"""O laco: recebe sem parar, amostra a 1 Hz, sonda o relogio a cada 5 min.

O desenho tem uma propriedade que decide tudo: **o receptor nunca enfileira e
nunca escreve em disco.** Ele substitui um slot com a ultima cotacao e volta a
esperar. O tique de 1 Hz le o slot e grava.

Se o receptor gravasse, uma latencia de disco viraria latencia de rede: as
mensagens se acumulariam e a "ultima cotacao" ficaria velha sem que nada
apontasse. Com o slot, atraso de disco atrasa a AMOSTRA - que carrega
`idade_ms` e sai `defasada` -, e nunca a leitura do stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import websockets

from . import relogio
from .amostra import Estado, amostrar, da_mensagem
from .arquivo import Diario

log = logging.getLogger("coletor")

URL_BASE = "wss://stream.binance.com:9443/ws"
INTERVALO_AMOSTRA_S = 1.0

# Backoff da reconexao. Nao e exponencial sem teto de proposito: um coletor que
# desiste por minutos perde mais dado do que a rede custou. O teto e baixo.
BACKOFF_INICIAL_S = 1.0
BACKOFF_MAXIMO_S = 30.0

# Bloqueio por jurisdicao (451) nao e transitorio: a mesma requisicao do mesmo
# lugar devolve a mesma resposta. Repetir a cada 30 s so enche o log e esconde
# a causa - o primeiro deploy na Railway produziu exatamente esse ruido.
#
# O servico NAO sai: com restartPolicyType ALWAYS isso viraria laco de
# reinicio, e uma troca de regiao so tem efeito no redeploy seguinte de todo
# modo. Ele espera longo e diz UMA VEZ o que esta acontecendo.
BACKOFF_BLOQUEIO_S = 300.0


def _status_http(e: BaseException) -> int | None:
    """Codigo HTTP de uma falha de handshake do websocket, se houver."""
    resposta = getattr(e, "response", None)
    return getattr(resposta, "status_code", None)


async def receber(url: str, estado: Estado, parar: asyncio.Event) -> None:
    """Mantem a conexao viva e o slot atualizado. Reconecta sozinho.

    `estado.conectado` e a fonte de verdade do amostrador sobre a conexao - e
    ele vai a falso ANTES de qualquer tentativa de reconexao, para que as
    amostras do intervalo saiam `desconectado` em vez de repetirem uma cotacao
    de antes da queda.
    """
    espera = BACKOFF_INICIAL_S
    bloqueado = False
    while not parar.is_set():
        try:
            # ping_interval do cliente: o servidor da Binance manda ping e
            # espera pong. A biblioteca responde sozinha; o nosso ping serve
            # para detectar conexao morta que nao fechou (half-open).
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                estado.conectado = True
                espera = BACKOFF_INICIAL_S
                bloqueado = False
                log.info("coletor.conectado", extra={"url": url})
                async for bruto in ws:
                    if parar.is_set():
                        break
                    recebido_ns = time.time_ns()
                    try:
                        payload: dict[str, Any] = json.loads(bruto)
                        estado.ultima = da_mensagem(payload, recebido_ns)
                    except (ValueError, KeyError, TypeError):
                        # Mensagem que nao e uma cotacao (resposta de
                        # assinatura, por exemplo). Nao derruba o laco, e
                        # tambem nao vira cotacao.
                        log.debug("coletor.mensagem_ignorada", extra={"bruto": str(bruto)[:200]})
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - qualquer falha de rede reconecta
            if _status_http(e) == 451:
                # Diz UMA vez, com a acao junto, e depois cala.
                if not bloqueado:
                    bloqueado = True
                    log.error("coletor.bloqueio_por_jurisdicao", extra={
                        "url": url,
                        "diagnostico": "a Binance recusa este ambiente por "
                                       "jurisdicao (HTTP 451). Repetir nao "
                                       "conserta.",
                        "acao": "trocar a regiao do servico na Railway "
                                "(Settings > Regions), ou hospedar o coletor "
                                "fora dela. O volume vazio migra de graca; "
                                "depois de semanas de coleta, nao.",
                        "nota": "o ADR 0012 mediu data.binance.vision, que e "
                                "outro host - o bloqueio e de api/stream.",
                    })
                espera = BACKOFF_BLOQUEIO_S
            else:
                bloqueado = False
                log.warning("coletor.queda", extra={"erro": repr(e), "espera_s": espera})
        finally:
            estado.conectado = False
        if parar.is_set():
            break
        await asyncio.sleep(espera)
        if espera != BACKOFF_BLOQUEIO_S:
            espera = min(espera * 2, BACKOFF_MAXIMO_S)


async def amostrar_em_1hz(estado: Estado, diario: Diario, parar: asyncio.Event) -> None:
    """Um tique por segundo, alinhado ao relogio de parede.

    O alinhamento existe para que `sampled_at` caia perto do segundo cheio, e
    nao derive com o tempo de execucao do laco. Sem ele, mil tiques acumulariam
    o custo de mil escritas e a amostragem deixaria de ser 1 Hz sem avisar.
    """
    while not parar.is_set():
        agora = time.time()
        await asyncio.sleep(max(0.0, (1.0 - agora % 1.0)))
        if parar.is_set():
            break
        ns = time.time_ns()
        a = amostrar(estado, ns)
        diario.escrever(a.como_linha(), ns=ns)


async def sondar_relogio(diario: Diario, parar: asyncio.Event,
                         *, intervalo_s: float = relogio.INTERVALO_S) -> None:
    """Mede offset e RTT contra a Binance, e grava como telemetria.

    Falha de sonda vira linha de telemetria com o motivo. Nao inventa valor, e
    nao derruba a coleta: um relogio nao medido e um relogio nao medido.
    """
    while not parar.is_set():
        ns = time.time_ns()
        try:
            m = await asyncio.to_thread(relogio.medir)
            diario.escrever(m.como_linha(), ns=ns)
            log.info("coletor.relogio", extra={
                "offset_ms": round(m.offset_ms, 3),
                "rtt_ms": round(m.rtt_ms, 3),
                "incerteza_bruta_ms": round(m.incerteza_bruta_ms(), 3),
                "incerteza_residual_ms": round(m.incerteza_residual_ms(), 3),
            })
        except relogio.FalhaNaSonda as e:
            diario.escrever(
                {"tipo": "relogio", "medido_em_ns": ns, "falha": str(e)[:300]}, ns=ns
            )
            log.warning("coletor.relogio_falhou", extra={"erro": str(e)[:300]})
        # Espera fatiada para que o SIGTERM nao precise aguardar 5 minutos.
        for _ in range(int(intervalo_s)):
            if parar.is_set():
                return
            await asyncio.sleep(1.0)
