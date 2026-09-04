"""Envio assinado para a `api`. ADR 0029, incremento 16.

O rele **nao decide nada**: ele busca barra fechada e entrega. Quem valida,
quem grava e quem decide o que fazer com o dado e a `api`.

Isso nao e humildade de desenho - e a razao pela qual a alternativa A do
transporte foi escolhida sobre migrar a `api`: o volume com as 70.080 barras,
os runs e o ledger nao se move, e o unico artefato irrecuperavel do projeto
fica onde esta.

## A assinatura

`HMAC-SHA256` sobre `carimbo \\n nonce \\n corpo`, com o corpo **exatamente
como vai no fio**. Se este lado serializasse duas vezes - uma para assinar e
outra para enviar -, a menor diferenca de espacamento faria a verificacao
falhar parecendo credencial errada. Por isso os bytes sao produzidos UMA vez.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Sequence

from .binance import PRICE_SCALE_EXP, VOLUME_SCALE_EXP, Kline

log = logging.getLogger("rele")

TIMEOUT_S = 30.0
NONCE_BYTES = 16


class ErroDeEnvio(Exception):
    """Falha ao entregar. A mensagem carrega o corpo da resposta."""


class DivergenciaRecusada(ErroDeEnvio):
    """A `api` devolveu 409: a mesma barra ja existe com outro conteudo.

    NAO e transitorio, e nao se resolve reenviando. Ou a origem revisou o
    passado, ou algo corrompeu o dado no caminho - e escolher uma das versoes
    seria decidir qual passado vale.
    """


@dataclass(frozen=True)
class Destino:
    base_url: str
    token: str
    segredo: str


def _assinar(segredo: str, carimbo_ms: int, nonce: str, corpo: bytes) -> str:
    mensagem = (
        str(carimbo_ms).encode("ascii") + b"\n"
        + nonce.encode("ascii") + b"\n"
        + corpo
    )
    return hmac.new(segredo.encode("utf-8"), mensagem, hashlib.sha256).hexdigest()


def _pedir(req: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise ErroDeEnvio(f"falha de rede: {e}") from e


def ponto_de_retomada(
    destino: Destino, *, venue: str, symbol: str, timeframe: str,
    interval_ms: int, pedir=_pedir,
) -> int | None:
    """Pergunta a `api` de onde retomar.

    **Perguntar em vez de supor** e o que torna queda do rele um atraso
    recuperavel: o estado de verdade e o da `api`, e nao o que este processo
    lembra. Um rele que reiniciasse e comecasse do zero reenviaria tudo; um
    que guardasse o proprio ponto divergiria no primeiro desencontro.
    """
    q = urllib.parse.urlencode({
        "venue": venue, "symbol": symbol, "timeframe": timeframe,
        "interval_ms": interval_ms,
    })
    req = urllib.request.Request(
        f"{destino.base_url}/api/aovivo/ponto?{q}",
        headers={"Authorization": f"Bearer {destino.token}"},
    )
    status, corpo = pedir(req, TIMEOUT_S)
    if status != 200:
        raise ErroDeEnvio(f"HTTP {status} ao pedir o ponto: {corpo[:300]!r}")
    return json.loads(corpo).get("retomar_de_ms")


def enviar(
    destino: Destino,
    *,
    venue: str,
    symbol: str,
    timeframe: str,
    interval_ms: int,
    origem: str,
    klines: Sequence[Kline],
    agora_ms: int | None = None,
    pedir=_pedir,
) -> dict[str, Any]:
    """Entrega um lote assinado. Devolve o que a `api` respondeu."""
    corpo_dict = {
        "venue": venue, "symbol": symbol, "timeframe": timeframe,
        "interval_ms": interval_ms,
        "price_scale_exp": PRICE_SCALE_EXP,
        "volume_scale_exp": VOLUME_SCALE_EXP,
        "origem": origem,
        "barras": [
            {
                "open_time_ms": k.open_time_ms, "open": k.open, "high": k.high,
                "low": k.low, "close": k.close, "volume": k.volume,
                "quote_volume": k.quote_volume, "trades": k.trades,
            }
            for k in klines
        ],
    }
    # UMA serializacao, e os mesmos bytes assinados e enviados.
    corpo = json.dumps(corpo_dict, separators=(",", ":")).encode("utf-8")

    carimbo = int(time.time() * 1000) if agora_ms is None else agora_ms
    nonce = secrets.token_hex(NONCE_BYTES)

    req = urllib.request.Request(
        f"{destino.base_url}/api/aovivo/barras",
        data=corpo,
        method="POST",
        headers={
            "Authorization": f"Bearer {destino.token}",
            "Content-Type": "application/json",
            "X-Rele-Assinatura": _assinar(destino.segredo, carimbo, nonce, corpo),
            "X-Rele-Carimbo": str(carimbo),
            "X-Rele-Nonce": nonce,
        },
    )
    status, resposta = pedir(req, TIMEOUT_S)

    if status == 409:
        raise DivergenciaRecusada(
            f"a api recusou por divergencia de conteudo: {resposta[:400]!r}. "
            f"NAO reenviar - o fluxo e append-only, e reenviar dara o mesmo "
            f"409"
        )
    if status not in (200, 202):
        raise ErroDeEnvio(f"HTTP {status}: {resposta[:400]!r}")

    return json.loads(resposta)
