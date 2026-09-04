"""Busca de klines FECHADAS na Binance. ADR 0029, incremento 16.

Roda em Singapura porque US East devolve **HTTP 451** - a Binance recusa por
jurisdicao. O ADR 0028 registra a descoberta e a generalizacao indevida que
ela corrigiu: o ADR 0012 mediu `data.binance.vision`, que e outro host.

## Por que REST e nao WebSocket

O que o forward precisa e **kline fechada**, e ela existe uma vez a cada 15
minutos. Um stream entregaria a barra em formacao a cada segundo, e todas
seriam descartadas menos a ultima - trocando simplicidade por nada.

E o pull torna o backfill trivial: pedir `[de, ate]` e a MESMA chamada que
pedir as ultimas. Com stream, backfill exigiria um segundo caminho de codigo,
e um segundo caminho e onde a divergencia mora.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal

log = logging.getLogger("rele")

BASE = "https://api.binance.com/api/v3/klines"
USER_AGENT = "ecossistema-agentes-economicos/0c-rele"
TIMEOUT_S = 20.0

# Teto da Binance por chamada, e o teto do lote da API sao numeros diferentes.
# O menor dos dois manda.
MAX_POR_CHAMADA = 1_000

# Escalas dos inteiros de precisao fixa. As MESMAS da ingestao historica
# (`app/dataset/binance.py`), e nao um numero escolhido aqui: elas entram no
# hash canonico do snapshot, e duas escalas diferentes para a mesma serie
# fariam dois snapshots do mesmo intervalo terem hashes diferentes.
PRICE_SCALE_EXP = 8
VOLUME_SCALE_EXP = 8


class ErroDeFonte(Exception):
    """Falha ao buscar. Transitorio ate prova em contrario."""


class BloqueioPorJurisdicao(ErroDeFonte):
    """HTTP 451. NAO e transitorio - repetir do mesmo lugar da o mesmo 451.

    Mesma familia do 400 que o incremento 11b separou dos transitorios. O
    coletor aprendeu isso no primeiro deploy, e o rele nasce sabendo.
    """


@dataclass(frozen=True)
class Kline:
    """Uma barra fechada, em inteiros. Os campos sao os da API do fluxo."""

    open_time_ms: int
    open: int
    high: int
    low: int
    close: int
    volume: int
    quote_volume: int
    trades: int


def _inteiro(texto: str, escala: int) -> int:
    """Decimal -> inteiro de precisao fixa, SEM ponto flutuante.

    `Decimal` e nao `float`: a regra 5 do projeto proibe ponto flutuante em
    valor monetario, e a razao aparece aqui - `float("0.1") * 10**8` nao da
    10000000 exato em toda plataforma, e o hash canonico do snapshot compara
    inteiros byte a byte.
    """
    return int(Decimal(texto) * (10 ** escala))


def _buscar(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 451:
            raise BloqueioPorJurisdicao(
                f"HTTP 451 em {url}: a Binance recusa este ambiente por "
                f"jurisdicao. NAO adianta repetir. Acao: conferir a regiao do "
                f"servico na Railway - Southeast Asia (Singapura) responde, "
                f"US East nao (ADR 0028)"
            ) from e
        raise ErroDeFonte(f"HTTP {e.code} em {url}: {e.reason}") from e
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise ErroDeFonte(f"falha de rede em {url}: {e}") from e


def buscar(
    symbol: str,
    timeframe: str,
    *,
    de_ms: int | None = None,
    limite: int = 100,
    agora_ms: int,
    interval_ms: int,
    buscar_bytes=_buscar,
) -> list[Kline]:
    """Klines FECHADAS. A barra em formacao e descartada aqui, e nao depois.

    **O corte e a razao de esta funcao existir.** A Binance devolve a barra em
    formacao junto com as fechadas, e ela muda a cada negocio. Se ela chegasse
    ao fluxo, o `stream_bar` receberia uma barra que depois mudaria - e a
    proxima tentativa levantaria `DivergenciaDeConteudo`, que e erro alto.

    Ou seja: sem este corte, o rele produziria um erro grave de forma rotineira,
    e a mensagem apontaria para corrupcao de dado em vez de para aqui.
    """
    params: dict[str, object] = {
        "symbol": symbol.upper(),
        "interval": timeframe,
        "limit": min(limite, MAX_POR_CHAMADA),
    }
    if de_ms is not None:
        params["startTime"] = de_ms

    url = f"{BASE}?{urllib.parse.urlencode(params)}"
    bruto = buscar_bytes(url, TIMEOUT_S)

    try:
        linhas = json.loads(bruto)
    except ValueError as e:
        raise ErroDeFonte(f"resposta nao e JSON: {bruto[:200]!r}") from e
    if not isinstance(linhas, list):
        raise ErroDeFonte(f"resposta inesperada: {bruto[:200]!r}")

    # A ultima barra FECHADA e a que abriu no penultimo passo da grade.
    ultima_fechada = (agora_ms // interval_ms) * interval_ms - interval_ms

    saida: list[Kline] = []
    for l in linhas:
        try:
            abertura = int(l[0])
            if abertura > ultima_fechada:
                continue          # em formacao: descartada AQUI
            saida.append(Kline(
                open_time_ms=abertura,
                open=_inteiro(l[1], PRICE_SCALE_EXP),
                high=_inteiro(l[2], PRICE_SCALE_EXP),
                low=_inteiro(l[3], PRICE_SCALE_EXP),
                close=_inteiro(l[4], PRICE_SCALE_EXP),
                volume=_inteiro(l[5], VOLUME_SCALE_EXP),
                quote_volume=_inteiro(l[7], VOLUME_SCALE_EXP),
                trades=int(l[8]),
            ))
        except (IndexError, TypeError, ValueError) as e:
            raise ErroDeFonte(f"kline malformada: {l!r}") from e

    saida.sort(key=lambda k: k.open_time_ms)
    return saida
