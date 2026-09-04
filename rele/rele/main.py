"""Ponto de entrada do servico `rele`. ADR 0029, incremento 16.

Busca kline fechada em Singapura, entrega assinada na `api` em US East. Nao
decide nada, nao guarda estado, e nao serve nada.

## O laco tem UMA regra que decide o resto

**Pergunta o ponto de retomada a cada volta.** O estado de verdade e o da
`api`, e nao o que este processo lembra - e e isso que torna queda do rele um
**atraso recuperavel** em vez de lacuna:

- rele reiniciado: pergunta e retoma de onde a `api` parou;
- rele fora por uma hora: pergunta, ve o atraso, e busca o intervalo inteiro;
- dois reles por engano: os dois enviam a mesma barra, e a chave idempotente
  absorve - `(venue, symbol, timeframe, open_time_ms)`.

Um rele que guardasse o proprio ponto divergiria no primeiro desencontro, e a
divergencia apareceria como lacuna que ninguem consegue explicar.

## Sem estado, e por isso sem volume

O rele nao tem nada para persistir. Nao tem volume na Railway, e o pre-voo nao
precisa conferir montagem - ao contrario do coletor e da `api`, que tem.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from typing import Any

from . import binance, envio

INTERVALO_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
                "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

SIMBOLO = os.environ.get("RELE_SIMBOLO", "BTCUSDT").upper()
TIMEFRAME = os.environ.get("RELE_TIMEFRAME", "15m")
VENUE = os.environ.get("RELE_VENUE", "binance")

# Quanto esperar entre voltas. Um TERCO do intervalo da barra: a barra fecha
# uma vez por intervalo, e checar tres vezes garante que ela e apanhada logo
# depois de fechar sem depender de o relogio estar alinhado.
FRACAO_DO_INTERVALO = 3

# Teto por lote. O menor entre o teto da Binance (1.000) e o da rota (500).
MAX_LOTE = 500

log = logging.getLogger("rele")


def configurar_log() -> None:
    class Json(logging.Formatter):
        def format(self, r: logging.LogRecord) -> str:
            base = {"ts": self.formatTime(r), "level": r.levelname,
                    "event": r.getMessage(), "logger": r.name}
            vazio = logging.LogRecord("", 0, "", 0, "", (), None).__dict__
            for k, v in r.__dict__.items():
                if k not in vazio:
                    base[k] = v
            return json.dumps(base, default=str)

    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(Json())
    logging.basicConfig(level=logging.INFO, handlers=[h], force=True)


def destino_do_ambiente() -> envio.Destino:
    faltando = [
        n for n in ("RELE_API_URL", "API_SERVICE_TOKEN", "RELE_HMAC_SECRET")
        if not os.environ.get(n)
    ]
    if faltando:
        raise SystemExit(
            f"rele: variaveis obrigatorias ausentes: {faltando}. "
            f"Falha FECHADO - um rele sem credencial nao deve subir e tentar"
        )
    return envio.Destino(
        base_url=os.environ["RELE_API_URL"].rstrip("/"),
        token=os.environ["API_SERVICE_TOKEN"],
        segredo=os.environ["RELE_HMAC_SECRET"],
    )


def uma_volta(destino: envio.Destino, *, agora_ms: int | None = None) -> dict[str, Any]:
    """Pergunta o ponto, busca o que falta, entrega. Devolve o que aconteceu."""
    interval_ms = INTERVALO_MS[TIMEFRAME]
    agora = int(time.time() * 1000) if agora_ms is None else agora_ms

    retomar = envio.ponto_de_retomada(
        destino, venue=VENUE, symbol=SIMBOLO, timeframe=TIMEFRAME,
        interval_ms=interval_ms,
    )

    klines = binance.buscar(
        SIMBOLO, TIMEFRAME, de_ms=retomar, limite=MAX_LOTE,
        agora_ms=agora, interval_ms=interval_ms,
    )
    if not klines:
        return {"enviadas": 0, "motivo": "nenhuma barra fechada nova"}

    # `backfill` quando estamos recuperando atraso, `ao_vivo` quando e a barra
    # que acabou de fechar. A distincao vai para o fluxo e permite depois
    # separar "chegou na hora" de "foi recuperado".
    ultima_fechada = (agora // interval_ms) * interval_ms - interval_ms
    atrasado = klines[0].open_time_ms < ultima_fechada
    origem = "backfill" if atrasado else "ao_vivo"

    resposta = envio.enviar(
        destino, venue=VENUE, symbol=SIMBOLO, timeframe=TIMEFRAME,
        interval_ms=interval_ms, origem=origem, klines=klines,
        agora_ms=agora,
    )
    return {
        "enviadas": len(klines), "origem": origem,
        "aceitas": resposta.get("aceitas"),
        "repetidas": resposta.get("repetidas"),
        "de_ms": klines[0].open_time_ms,
        "ate_ms": klines[-1].open_time_ms,
    }


def executar() -> int:
    configurar_log()
    destino = destino_do_ambiente()
    interval_ms = INTERVALO_MS[TIMEFRAME]
    espera_s = interval_ms / 1000 / FRACAO_DO_INTERVALO

    parar = {"agora": False}
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: parar.update(agora=True))

    log.info("rele.iniciado", extra={
        "api": destino.base_url, "simbolo": SIMBOLO,
        "timeframe": TIMEFRAME, "espera_s": espera_s,
    })

    while not parar["agora"]:
        try:
            r = uma_volta(destino)
            if r.get("enviadas"):
                log.info("rele.volta", extra=r)
        except binance.BloqueioPorJurisdicao as e:
            # NAO transitorio: repetir do mesmo lugar da o mesmo 451. Diz uma
            # vez com a acao e continua esperando - sair viraria laco de
            # reinicio sob restartPolicy ALWAYS.
            log.error("rele.bloqueio_por_jurisdicao", extra={
                "erro": str(e),
                "acao": "conferir a regiao na Railway: Southeast Asia responde",
            })
        except envio.DivergenciaRecusada as e:
            # ERRO ALTO, e nao se resolve reenviando.
            log.error("rele.divergencia", extra={"erro": str(e)})
        except (binance.ErroDeFonte, envio.ErroDeEnvio) as e:
            log.warning("rele.falha_transitoria", extra={"erro": str(e)})
        for _ in range(max(1, int(espera_s))):
            if parar["agora"]:
                break
            time.sleep(1.0)

    log.info("rele.encerrando")
    return 0


if __name__ == "__main__":
    raise SystemExit(executar())
