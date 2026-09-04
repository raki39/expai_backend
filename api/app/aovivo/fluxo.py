"""O fluxo aberto de barras ao vivo. ADR 0029.

Append-only, **sem hash**, e nunca citado por resultado nenhum. A ausencia da
coluna de hash e o desenho: e o que impede alguem citar o fluxo como se fosse
reproduzivel.

**Este modulo nao e alcancavel pelo caminho do agente**, e nao por permissao:
`stream_bar` nao esta em `dataset_split`, logo nao aparece em
`bar_por_finalidade`, que e a unica porta do agente. Ha guarda de importacao.

## O que "lacuna" significa aqui, e por que difere do coletor

**Kline e recuperavel; snapshot de BBO nao e.**

Se o rele cair por uma hora, as barras daquela hora **continuam existindo na
Binance** e podem ser buscadas depois. Queda do rele produz **atraso
recuperavel**, e nao lacuna - por isso `origem` distingue `ao_vivo` de
`backfill`, e por isso `ultima_confirmada` existe.

Lacuna de verdade e o que nem o backfill traz: indisponibilidade da propria
exchange. Ela e declarada no manifesto do snapshot, nunca interpolada.

No coletor (ADR 0028) e o contrario: um segundo de BBO perdido esta perdido
para sempre, porque nao existe historico de topo de livro a 1 Hz para buscar.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal

log = logging.getLogger(__name__)

Origem = Literal["ao_vivo", "backfill"]


class DivergenciaDeConteudo(Exception):
    """A mesma barra chegou com conteudo DIFERENTE. Erro alto, sempre.

    Reenvio identico e o caminho normal - retry do rele, e o que a chave
    idempotente absorve em silencio. Reenvio com conteudo diferente e outra
    coisa: ou a origem revisou o passado, ou algo no caminho corrompeu o dado,
    ou dois remetentes discordam.

    Nenhuma dessas tres pode ser resolvida escolhendo uma das versoes, e por
    isso isto nao e aviso. E o mesmo raciocinio do `DivergenciaNaReingestao`
    da ingestao historica: "um dataset fixado que pode ser trocado por baixo
    nao esta fixado".
    """


class BarraInvalida(Exception):
    """Validacao integral falhou. A API nao confia no rele."""


@dataclass(frozen=True)
class Barra:
    """Uma kline FECHADA. Inteiros de precisao fixa (regra 5)."""

    open_time_ms: int
    open: int
    high: int
    low: int
    close: int
    volume: int
    quote_volume: int
    trades: int


@dataclass(frozen=True)
class Serie:
    """A identidade da serie. Entra no hash canonico do snapshot."""

    venue: str
    symbol: str
    timeframe: str
    interval_ms: int
    price_scale_exp: int
    volume_scale_exp: int


@dataclass(frozen=True)
class Recebimento:
    """O que aconteceu com um lote."""

    aceitas: int
    repetidas: int          # identicas: idempotencia funcionando
    primeira_ms: int | None
    ultima_ms: int | None


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validar(b: Barra, serie: Serie) -> None:
    """Validacao INTEGRAL, feita pela API e nao pelo rele.

    O rele e codigo nosso, mas roda noutro lugar e fala pela rede. Confiar na
    validacao dele seria mover a fronteira de confianca para fora do processo
    que grava - e a regra e a mesma que vale para o modelo: o que chega de
    fora e conferido aqui.
    """
    if b.open_time_ms <= 0:
        raise BarraInvalida(f"open_time_ms invalido: {b.open_time_ms}")
    if b.open_time_ms % serie.interval_ms != 0:
        raise BarraInvalida(
            f"open_time_ms {b.open_time_ms} nao cai na grade de "
            f"{serie.interval_ms} ms - barra desalinhada nao e barra"
        )
    if min(b.open, b.high, b.low, b.close) <= 0:
        raise BarraInvalida("preco nao positivo")
    if b.high < b.low:
        raise BarraInvalida(f"high {b.high} < low {b.low}")
    if not (b.low <= b.open <= b.high and b.low <= b.close <= b.high):
        raise BarraInvalida(
            f"abertura/fechamento fora de [low, high]: "
            f"o={b.open} h={b.high} l={b.low} c={b.close}"
        )
    if min(b.volume, b.quote_volume, b.trades) < 0:
        raise BarraInvalida("volume ou negocios negativos")


def ultima_confirmada(
    conn: sqlite3.Connection, serie: Serie
) -> int | None:
    """`open_time_ms` da ultima barra que ESTE lado tem.

    E daqui que o backfill parte. O rele pergunta, e retoma do ponto - em vez
    de reenviar tudo ou de supor onde paramos.
    """
    linha = conn.execute(
        "SELECT MAX(open_time_ms) AS m FROM stream_bar"
        " WHERE venue = ? AND symbol = ? AND timeframe = ?",
        (serie.venue, serie.symbol, serie.timeframe),
    ).fetchone()
    return None if linha is None or linha["m"] is None else int(linha["m"])


def receber(
    conn: sqlite3.Connection,
    serie: Serie,
    barras: Iterable[Barra],
    *,
    origem: Origem,
) -> Recebimento:
    """Grava um lote, idempotente pela chave `(venue, symbol, timeframe, ms)`.

    Tres caminhos, e a diferenca entre o segundo e o terceiro e o ponto:

      nova         grava
      IDENTICA     nao grava, e conta como repetida. Retry normal do rele
      DIFERENTE    `DivergenciaDeConteudo`. Erro alto, e nada e gravado

    A transacao e do chamador: um lote e atomico, e um lote com divergencia
    nao entra pela metade.
    """
    aceitas = repetidas = 0
    marcos: list[int] = []
    agora = _agora()

    for b in barras:
        validar(b, serie)
        existente = conn.execute(
            "SELECT open, high, low, close, volume, quote_volume, trades"
            "  FROM stream_bar"
            " WHERE venue = ? AND symbol = ? AND timeframe = ?"
            "   AND open_time_ms = ?",
            (serie.venue, serie.symbol, serie.timeframe, b.open_time_ms),
        ).fetchone()

        if existente is not None:
            atual = (
                int(existente["open"]), int(existente["high"]),
                int(existente["low"]), int(existente["close"]),
                int(existente["volume"]), int(existente["quote_volume"]),
                int(existente["trades"]),
            )
            chegando = (b.open, b.high, b.low, b.close,
                        b.volume, b.quote_volume, b.trades)
            if atual != chegando:
                raise DivergenciaDeConteudo(
                    f"{serie.symbol} {serie.timeframe} em {b.open_time_ms}: "
                    f"gravado {atual}, chegando {chegando}. O fluxo e "
                    f"append-only e a barra ja existe com outro conteudo - "
                    f"escolher uma das versoes seria decidir qual passado vale"
                )
            repetidas += 1
            continue

        conn.execute(
            "INSERT INTO stream_bar ("
            " venue, symbol, timeframe, open_time_ms,"
            " open, high, low, close, volume, quote_volume, trades,"
            " interval_ms, price_scale_exp, volume_scale_exp,"
            " recebido_em, origem) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                serie.venue, serie.symbol, serie.timeframe, b.open_time_ms,
                b.open, b.high, b.low, b.close, b.volume, b.quote_volume,
                b.trades, serie.interval_ms, serie.price_scale_exp,
                serie.volume_scale_exp, agora, origem,
            ),
        )
        aceitas += 1
        marcos.append(b.open_time_ms)

    return Recebimento(
        aceitas=aceitas,
        repetidas=repetidas,
        primeira_ms=min(marcos) if marcos else None,
        ultima_ms=max(marcos) if marcos else None,
    )


def barras_em(
    conn: sqlite3.Connection, serie: Serie, de_ms: int, ate_ms_exclusive: int
) -> list[Barra]:
    """Barras do fluxo no intervalo semiaberto `[de, ate)`, ordenadas."""
    return [
        Barra(
            open_time_ms=int(l["open_time_ms"]),
            open=int(l["open"]), high=int(l["high"]),
            low=int(l["low"]), close=int(l["close"]),
            volume=int(l["volume"]), quote_volume=int(l["quote_volume"]),
            trades=int(l["trades"]),
        )
        for l in conn.execute(
            "SELECT open_time_ms, open, high, low, close,"
            "       volume, quote_volume, trades"
            "  FROM stream_bar"
            " WHERE venue = ? AND symbol = ? AND timeframe = ?"
            "   AND open_time_ms >= ? AND open_time_ms < ?"
            " ORDER BY open_time_ms",
            (serie.venue, serie.symbol, serie.timeframe,
             de_ms, ate_ms_exclusive),
        )
    ]


def atraso_ms(
    conn: sqlite3.Connection, serie: Serie, agora_ms: int
) -> int | None:
    """Quanto tempo desde a ultima barra que deveria ter fechado.

    **Atraso NAO e lacuna.** Kline e recuperavel: enquanto o backfill nao
    correr, o que existe e atraso. Chamar isso de lacuna cedo demais
    declararia perdido um dado que a Binance ainda tem.
    """
    ultima = ultima_confirmada(conn, serie)
    if ultima is None:
        return None
    # A ultima barra que JA FECHOU: a grade anterior ao instante atual.
    ultima_fechada = (agora_ms // serie.interval_ms) * serie.interval_ms - serie.interval_ms
    return max(0, ultima_fechada - ultima)
