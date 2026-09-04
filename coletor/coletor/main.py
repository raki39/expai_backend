"""Ponto de entrada do servico `coletor`.

ADR 0028. Servico proprio, arquivo proprio, volume proprio - e nenhum caminho
de leitura a partir do agente ou do validador (R82).

Ele nao participa de nenhuma DECISAO da Fase 0. Participa da MEDICAO de
calibracao (ADR 0027), e essa leitura e do calibrador - nunca do agente. Essa
e a R83 reescrita, e a distincao entre os dois verbos e o ponto dela.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from . import fluxo
from .amostra import Estado
from .arquivo import Diario, conferir_destino, volume_montado

SIMBOLO = os.environ.get("COLETOR_SIMBOLO", "btcusdt").lower()
DESTINO = Path(os.environ.get("COLETOR_DIR", "/dados"))
APP_ENV = os.environ.get("APP_ENV", "local")


def configurar_log() -> None:
    """Uma linha JSON por evento, no mesmo formato do servico `api`."""

    class Json(logging.Formatter):
        def format(self, r: logging.LogRecord) -> str:
            base = {"ts": self.formatTime(r), "level": r.levelname,
                    "event": r.getMessage(), "logger": r.name}
            for k, v in r.__dict__.items():
                if k not in logging.LogRecord("", 0, "", 0, "", (), None).__dict__:
                    base[k] = v
            return json.dumps(base, default=str)

    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(Json())
    logging.basicConfig(level=logging.INFO, handlers=[h], force=True)


async def principal() -> int:
    log = logging.getLogger("coletor")

    # --------------------------------------------------------------- pre-voo
    conferir_destino(DESTINO, os.environ.get("DB_PATH"))
    DESTINO.mkdir(parents=True, exist_ok=True)

    montado = volume_montado(DESTINO)
    log.info("coletor.preflight", extra={
        "app_env": APP_ENV, "destino": str(DESTINO), "simbolo": SIMBOLO,
        "volume_montado": montado,
    })
    if APP_ENV == "railway" and not montado:
        # Mesma recusa da api, e pela mesma razao: escrever com sucesso nao e
        # persistir. Sem volume, o coletor grava no filesystem efemero e perde
        # tudo no redeploy - em silencio, que e a pior forma.
        log.error("coletor.sem_volume", extra={
            "acao": "Railway > Settings > Volumes: mount path exatamente "
                    f"{DESTINO}",
        })
        return 1

    estado = Estado()
    parar = asyncio.Event()

    laco = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            laco.add_signal_handler(s, parar.set)
        except NotImplementedError:
            # Windows nao implementa add_signal_handler no loop asyncio.
            signal.signal(s, lambda *_: parar.set())

    url = f"{fluxo.URL_BASE}/{SIMBOLO}@bookTicker"
    with Diario(DESTINO, f"bookticker-{SIMBOLO}") as diario:
        tarefas = [
            asyncio.create_task(fluxo.receber(url, estado, parar)),
            asyncio.create_task(fluxo.amostrar_em_1hz(estado, diario, parar)),
            asyncio.create_task(fluxo.sondar_relogio(diario, parar)),
        ]
        await parar.wait()
        log.info("coletor.encerrando", extra={
            "linhas": diario.linhas, "u_duplicadas": estado.duplicadas,
            "u_regressoes": estado.regressoes,
        })
        for t in tarefas:
            t.cancel()
        await asyncio.gather(*tarefas, return_exceptions=True)
        # O `with` fecha o gzip. Sem isso o final do arquivo do dia fica
        # ilegivel - gzip guarda estado interno, e processo morto sem fechar
        # perde o ultimo membro.
    return 0


def executar() -> int:
    configurar_log()
    try:
        return asyncio.run(principal())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(executar())
