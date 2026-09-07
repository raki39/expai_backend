"""Entrega assinada da amostra alinhada. ADR 0032, incremento 18.

Mesmo protocolo do rele - HMAC sobre `carimbo \\n nonce \\n corpo`, janela
temporal, nonce, chave idempotente, ponto de retomada perguntado a cada volta.
O que nao se compartilha e o **segredo**: `COLETOR_HMAC_SECRET` e proprio, pelo
mesmo argumento que separou o do rele do token de servico - dois processos
diferentes, e o comprometimento de um nao pode dar o outro.

## Uma diferenca de postura em relacao ao rele, e ela e deliberada

O rele **falha FECHADO** sem credencial: um rele que nao entrega nao faz nada,
entao subir e tentar so esconde o problema.

O coletor **continua coletando** e reclama alto. A entrega e derivada e
refazivel a qualquer momento a partir do arquivo bruto; a COLETA nao e - um
segundo de BBO nao gravado esta perdido para sempre. Recusar a subir por falta
de credencial de envio trocaria uma perda irrecuperavel por uma recuperavel.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Sequence

from .extracao import PRICE_SCALE_EXP, VOLUME_SCALE_EXP, Observacao

log = logging.getLogger("coletor")

TIMEOUT_S = 30.0
NONCE_BYTES = 16


class ErroDeEnvio(Exception):
    """Falha ao entregar. A mensagem carrega o corpo da resposta."""


class DivergenciaRecusada(ErroDeEnvio):
    """A `api` devolveu 409: a mesma chave ja existe com outro conteudo.

    NAO e transitorio. A extracao e deterministica sobre o arquivo bruto,
    entao divergir significa que o bruto mudou ou que a regra mudou sem
    trocar de contrato - e escolher uma das versoes seria decidir qual passado
    vale.
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
    destino: Destino, *, venue: str, symbol: str, contrato: str,
    pedir=_pedir,
) -> dict[str, Any]:
    """Pergunta a `api` de que instante da grade retomar.

    Perguntar em vez de supor e o que torna queda do envio um ATRASO
    recuperavel: o estado de verdade e o da `api`, e o coletor nao guarda
    quanto ja entregou.
    """
    q = urllib.parse.urlencode(
        {"venue": venue, "symbol": symbol, "contrato": contrato}
    )
    req = urllib.request.Request(
        f"{destino.base_url}/api/aovivo/bbo/ponto?{q}",
        headers={"Authorization": f"Bearer {destino.token}"},
    )
    status, corpo = pedir(req, TIMEOUT_S)
    if status != 200:
        raise ErroDeEnvio(f"HTTP {status} ao pedir o ponto: {corpo[:300]!r}")
    return json.loads(corpo)


def enviar(
    destino: Destino,
    *,
    venue: str,
    symbol: str,
    contrato: str,
    observacoes: Sequence[Observacao],
    agora_ms: int | None = None,
    pedir=_pedir,
) -> dict[str, Any]:
    """Entrega um lote assinado. Devolve o que a `api` respondeu."""
    import time

    corpo_dict = {
        "venue": venue, "symbol": symbol, "contrato": contrato,
        "price_scale_exp": PRICE_SCALE_EXP,
        "volume_scale_exp": VOLUME_SCALE_EXP,
        "amostras": [o.como_corpo() for o in observacoes],
    }
    # UMA serializacao, e os mesmos bytes assinados e enviados. Serializar
    # duas vezes faria a menor diferenca de espacamento quebrar a verificacao,
    # PARECENDO CREDENCIAL ERRADA.
    corpo = json.dumps(corpo_dict, separators=(",", ":")).encode("utf-8")

    carimbo = int(time.time() * 1000) if agora_ms is None else agora_ms
    nonce = secrets.token_hex(NONCE_BYTES)

    req = urllib.request.Request(
        f"{destino.base_url}/api/aovivo/bbo",
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
            f"NAO reenviar - a extracao e deterministica sobre o arquivo "
            f"bruto, entao ou o bruto mudou ou a regra mudou sem trocar de "
            f"contrato"
        )
    if status not in (200, 202):
        raise ErroDeEnvio(f"HTTP {status}: {resposta[:400]!r}")

    return json.loads(resposta)
