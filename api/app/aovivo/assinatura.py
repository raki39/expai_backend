"""HMAC do rele, com protecao contra replay. ADR 0029, incremento 16.

## Por que HMAC, se ja existe o token de servico

O `API_SERVICE_TOKEN` autentica **quem chama**; ele nao amarra a credencial ao
**pedido**. Capturado, um Bearer vale para sempre e para qualquer corpo.

Esta rota e a primeira do projeto que **recebe dado que vai para o banco**, e
essa e a superficie nova que a alternativa A do transporte trouxe. O HMAC
amarra tres coisas ao mesmo instante: o segredo, o corpo exato, e uma janela de
tempo.

Sao credenciais SEPARADAS de proposito. Reusar o token de servico faria o
comprometimento de um dar o outro, e os dois tem alcances diferentes: um deixa
ler o painel, o outro deixa **escrever no fluxo**.

## As duas metades da protecao contra replay, e nenhuma basta sozinha

**A janela de tempo** limita por quanto tempo um pedido capturado serve. Sem
ela, um pedido gravado hoje seria aceito no ano que vem.

**O nonce** impede reenvio DENTRO da janela. Sem ele, capturar e reenviar em
dez segundos passaria - e a janela nao pode ser curta o bastante para tornar
isso impossivel, porque relogios divergem (o coletor mediu offset de 2,4 s numa
maquina real).

Juntas: a janela limita o alcance, o nonce mata a repeticao dentro dele. E o
armazem de nonce so precisa guardar a janela, porque fora dela o carimbo ja
recusa.

## O segredo vive so no env

Regra 15: "Chaves de LLM e tokens vivem so em env, nunca no banco." O mesmo
vale aqui, e o `/api/health` nao o publica.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Tolerancia do carimbo, em milissegundos.
#
# 300 s e generoso de proposito: relogio de maquina gerenciada deriva, e o
# coletor MEDIU offset de 2.450 ms contra a Binance numa maquina real. Uma
# janela apertada trocaria uma protecao por recusa de pedido legitimo - e quem
# mata a repeticao dentro da janela e o nonce, nao o aperto dela.
TOLERANCIA_MS = 300_000

CABECALHO_ASSINATURA = "x-rele-assinatura"
CABECALHO_CARIMBO = "x-rele-carimbo"
CABECALHO_NONCE = "x-rele-nonce"

NONCE_MIN = 16
NONCE_MAX = 128


class AssinaturaInvalida(Exception):
    """Nao bate, esta fora da janela, ou o nonce repetiu."""


@dataclass(frozen=True)
class Pedido:
    """O que se assina. A ordem e fixa e entra na mensagem."""

    carimbo_ms: int
    nonce: str
    corpo: bytes


def mensagem(p: Pedido) -> bytes:
    """`carimbo \\n nonce \\n corpo`, e o corpo entra CRU.

    Cru, e nao re-serializado: assinar o JSON reconstruido faria a assinatura
    depender de como cada lado ordena chaves e espaca virgulas - e a primeira
    divergencia de biblioteca quebraria tudo, parecendo credencial errada.
    """
    return (
        str(p.carimbo_ms).encode("ascii")
        + b"\n"
        + p.nonce.encode("ascii")
        + b"\n"
        + p.corpo
    )


def assinar(p: Pedido, segredo: str) -> str:
    return hmac.new(
        segredo.encode("utf-8"), mensagem(p), hashlib.sha256
    ).hexdigest()


def _agora_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def conferir(
    conn: sqlite3.Connection,
    p: Pedido,
    assinatura: str,
    segredo: str,
    *,
    agora_ms: int | None = None,
    tolerancia_ms: int = TOLERANCIA_MS,
) -> None:
    """Confere assinatura, janela e nonce. Levanta ou passa em silencio.

    A ordem das checagens e deliberada: **assinatura primeiro**. Conferir a
    janela antes diria a um atacante sem segredo se o carimbo dele estava bom,
    e gravar o nonce antes validaria a assinatura de quem nao a tem - enchendo
    a tabela de nonce com lixo de terceiros.
    """
    if not segredo:
        # Falha FECHADO, como `exigir_token_de_servico` ja faz.
        raise AssinaturaInvalida("RELE_HMAC_SECRET nao configurado no servidor")

    if not (NONCE_MIN <= len(p.nonce) <= NONCE_MAX) or not p.nonce.isalnum():
        raise AssinaturaInvalida(
            f"nonce fora do formato: {NONCE_MIN}-{NONCE_MAX} caracteres"
            f" alfanumericos"
        )

    esperada = assinar(p, segredo)
    if not hmac.compare_digest(esperada, assinatura.strip().lower()):
        # `compare_digest` e nao `==`: comparacao byte a byte com saida
        # antecipada vaza o prefixo correto pelo tempo de resposta.
        raise AssinaturaInvalida("assinatura nao bate")

    agora = _agora_ms() if agora_ms is None else agora_ms
    if abs(agora - p.carimbo_ms) > tolerancia_ms:
        raise AssinaturaInvalida(
            f"carimbo fora da janela de {tolerancia_ms} ms "
            f"(diferenca de {agora - p.carimbo_ms} ms). Se isto repete, o "
            f"relogio de um dos lados derivou - o coletor mede offset e a "
            f"telemetria dele diz de quanto"
        )

    # O nonce so e gravado DEPOIS de a assinatura passar. Antes disso,
    # qualquer um encheria esta tabela.
    try:
        conn.execute(
            "INSERT INTO rele_nonce (nonce, carimbo_ms, visto_em) VALUES (?,?,?)",
            (p.nonce, p.carimbo_ms,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
    except sqlite3.IntegrityError as e:
        raise AssinaturaInvalida(
            f"nonce {p.nonce[:12]}... ja foi usado: pedido repetido dentro da "
            f"janela. A janela sozinha nao pega isto, e e por isso que o nonce "
            f"existe"
        ) from e


def podar(conn: sqlite3.Connection, *, agora_ms: int | None = None,
          tolerancia_ms: int = TOLERANCIA_MS) -> int:
    """Descarta nonce mais velho que a janela. Devolve quantos saíram.

    Pode apagar porque, fora da janela, **o carimbo ja recusa** - guardar o
    nonce por mais tempo nao acrescentaria protecao, e a tabela cresceria sem
    limite.

    Esta e a unica tabela do projeto que pode ser apagada, e a excecao esta
    escrita aqui: ela nao e registro de experimento, e cache de protecao.
    """
    agora = _agora_ms() if agora_ms is None else agora_ms
    cur = conn.execute(
        "DELETE FROM rele_nonce WHERE carimbo_ms < ?", (agora - tolerancia_ms,)
    )
    return cur.rowcount or 0
