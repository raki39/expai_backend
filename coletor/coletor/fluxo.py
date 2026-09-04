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
import math
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

# Silencio maximo antes de considerar a conexao morta e reconectar.
#
# O bookTicker de BTC/USDT manda varias mensagens por SEGUNDO, entao 30 s de
# silencio nao e mercado calmo - e socket morto que nao fechou (half-open).
#
# Este e o sinal de vivacidade CERTO, e substitui o ping do cliente. A
# documentacao da Binance diz que o SERVIDOR manda ping a cada 20 s e espera
# pong em 1 minuto (a biblioteca responde sozinha), mas NAO diz que ele
# responde aos nossos. Com `ping_interval=20, ping_timeout=20` nos derrubavamos
# conexao saudavel sempre que ela demorava a responder um ping que talvez ela
# nem responda - e era essa a causa dos `coletor.queda` esporadicos no primeiro
# deploy em Singapura.
SILENCIO_MAXIMO_S = 30.0


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
            # `ping_interval=None`: NAO mandamos ping. A biblioteca continua
            # respondendo aos pings do servidor sozinha, que e o que a Binance
            # exige. A vivacidade e detectada pelo SILENCIO do dado, abaixo.
            async with websockets.connect(url, ping_interval=None) as ws:
                estado.conectado = True
                espera = BACKOFF_INICIAL_S
                bloqueado = False
                log.info("coletor.conectado", extra={"url": url})
                while not parar.is_set():
                    try:
                        bruto = await asyncio.wait_for(
                            ws.recv(), timeout=SILENCIO_MAXIMO_S
                        )
                    except asyncio.TimeoutError:
                        log.warning("coletor.silencio", extra={
                            "segundos": SILENCIO_MAXIMO_S,
                            "diagnostico": "nenhuma mensagem no periodo. O "
                                           "bookTicker manda varias por "
                                           "segundo, entao isto e socket morto "
                                           "e nao mercado calmo.",
                        })
                        break
                    recebido_ns = time.time_ns()
                    try:
                        payload: dict[str, Any] = json.loads(bruto)
                        estado.ultima = da_mensagem(payload, recebido_ns)
                    except (ValueError, KeyError, TypeError):
                        # Mensagem que nao e uma cotacao (resposta de
                        # assinatura, ou o `serverShutdown` que a Binance manda
                        # antes de manutencao). Nao derruba o laco, e tambem
                        # nao vira cotacao.
                        log.debug("coletor.mensagem_ignorada", extra={"bruto": str(bruto)[:200]})
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosedOK:
            # Fechamento LIMPO. A documentacao da Binance e explicita: "a single
            # connection to the API is only valid for 24 hours; expect to be
            # disconnected after the 24-hour mark". Isso e rotina, nao falha:
            # sai como INFO e reconecta na hora, sem backoff. Tratar como erro
            # produziria um WARNING por dia dizendo que algo esperado ocorreu.
            log.info("coletor.reconectando", extra={
                "motivo": "fechamento limpo do servidor (24 h ou manutencao)",
            })
            espera = BACKOFF_INICIAL_S
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
    """UM tique por segundo, num alvo que avanca de exatamente 1 s.

    A primeira versao calculava `1.0 - time.time() % 1.0` a cada volta, e
    amostrava a **1,98 Hz** - medido, nao suspeitado. O motivo: depois de
    gravar, o resto da divisao fica logo ABAIXO de 1 (0,984, digamos), a espera
    sai 16 ms, e o laco amostra outra vez no MESMO segundo. Quase todo segundo
    saia duas vezes.

    Isso e o defeito de sempre num lugar novo: o ADR 0028 diz "amostrado a
    1 Hz" e o codigo fazia 2 Hz - o documento parou de descrever o codigo, e
    nada acusava. De quebra dobrava o volume, contra os 21,5 MB/mes medidos.

    O alvo monotonico resolve: ele avanca de 1,0 s e nao depende de quando o
    laco acordou.

    **Atraso longo PULA em vez de correr atras.** Se o processo travar (GC,
    disco, agendador), recuperar os tiques perdidos gravaria um punhado de
    amostras com `sampled_at` colado - dado falso, com cara de dado. Pular
    deixa um buraco visivel na sequencia de `sampled_at`, que e a verdade.
    """
    proximo = math.ceil(time.time())
    while not parar.is_set():
        await asyncio.sleep(max(0.0, proximo - time.time()))
        if parar.is_set():
            break
        ns = time.time_ns()
        a = amostrar(estado, ns)
        diario.escrever(a.como_linha(), ns=ns)
        proximo += 1.0
        if proximo <= time.time():
            # Ficamos para tras. Realinha ao proximo segundo FUTURO.
            proximo = math.ceil(time.time())


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
