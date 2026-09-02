"""Aplicacao FastAPI do servico `api`.

Um agente, um processo. O painel e um servico separado, hospedado na Vercel,
que fala com este pela internet - por isso a api tem dominio publico e todo
endpoint exige token (ADR 0010). A unica excecao e a rota de liveness `/`,
que nao expoe dado nenhum e existe para diagnosticar deploy.

Sequencia de boot:
  1. carrega o ambiente (segredos + bootstrap)
  2. configura log estruturado, com redacao de segredo
  3. abre o SQLite no volume e aplica migracoes  -- no boot, nunca no build
  4. garante a config_version 1 a partir dos defaults
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router
from .config import service as config_service
from .logging_setup import configurar_logging
from .sonda_rede import router as router_sonda
from .settings import SECRET_FIELDS, get_settings
from .store import (
    conectar,
    devices_do_caminho,
    migrar,
    volume_gravavel,
    volume_montado,
)

log = logging.getLogger(__name__)


def _configurar_log_do_processo() -> None:
    """Aplica o log estruturado o mais cedo possivel.

    Chamado na importacao do modulo, e nao so no lifespan, porque o uvicorn
    emite as primeiras linhas ("Started server process", "Waiting for
    application startup") antes de o lifespan rodar. Sem isso, todo boot
    comeca com duas linhas de texto livre no meio do JSON.
    """
    settings = get_settings()
    configurar_logging(
        settings.log_level,
        segredos=[
            getattr(settings, campo).get_secret_value() for campo in SECRET_FIELDS
        ],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # Idempotente: o ambiente pode ter mudado entre a importacao e o boot
    # (e nos testes muda a cada caso).
    _configurar_log_do_processo()

    if not volume_gravavel(settings.db_path.parent):
        # Falhar aqui e melhor que descobrir depois de perder um run.
        raise RuntimeError(
            f"diretorio do banco nao e gravavel: {settings.db_path.parent}. "
            "Na Railway, confira se o volume esta montado em /data."
        )

    # Escrever com sucesso nao prova persistencia. Em producao, exigimos que
    # o diretorio do banco esteja num volume DE VERDADE - senao o servico
    # sobe, grava, e perde tudo no proximo deploy sem avisar.
    montado = volume_montado(settings.db_path.parent)
    dev_dados, dev_raiz = devices_do_caminho(settings.db_path.parent)

    # A evidencia vai para o log ANTES da decisao, sempre. Se algum dia esta
    # checagem der falso positivo, o log ja tera o que precisamos para saber.
    log.info(
        "volume.check",
        extra={
            "db_dir": str(settings.db_path.parent),
            "device_db_dir": dev_dados,
            "device_raiz": dev_raiz,
            "montado": montado,
            "gravavel": True,
        },
    )

    if settings.app_env == "railway" and montado is False:
        raise RuntimeError(
            f"VOLUME AUSENTE: {settings.db_path.parent} esta no MESMO "
            f"dispositivo que a raiz (device={dev_dados}). Isso significa que "
            "e um diretorio da propria imagem, e nao um volume - o banco "
            "seria gravado normalmente e PERDIDO no proximo deploy. "
            "Correcao: Railway > servico api > Settings > Volumes > "
            "Add Volume, com mount path exatamente /data. "
            "  Depois disso, device_db_dir e device_raiz devem ser diferentes "
            "no log volume.check."
        )

    conn = conectar(settings.db_path)
    schema_version = migrar(conn)
    versao_config = config_service.bootstrap(conn, settings)

    app.state.settings = settings
    app.state.conn = conn
    app.state.build_id = os.getenv("RAILWAY_GIT_COMMIT_SHA", "local")

    log.info(
        "app.started",
        extra={
            "app_env": settings.app_env,
            "db_path": str(settings.db_path),
            "schema_version": schema_version,
            "volume_montado": montado,
            "config_version": versao_config.id,
            "config_hash": versao_config.config_hash,
            "build": app.state.build_id,
        },
    )

    try:
        yield
    finally:
        conn.close()
        log.info("app.stopped")


def criar_app() -> FastAPI:
    """Monta a aplicacao a partir da configuracao de ambiente vigente.

    E fabrica, e nao objeto de modulo, porque a lista de origens do CORS e
    fixada no momento da construcao. Sem a fabrica, o middleware congelaria
    no ambiente da primeira importacao e nao haveria como testa-lo.
    """
    settings = get_settings()

    app = FastAPI(
        title="Fase 0A - api",
        description=(
            "Servico do experimento da Fase 0A. Backend do agente, do "
            "simulador, da carteira e do ledger."
        ),
        version="0.1.0",
        lifespan=lifespan,
        # Sem docs interativos: a superficie e consumida pelo painel,
        # e um endpoint a menos e uma coisa a menos para proteger.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # CORS so entra em jogo se o NAVEGADOR chamar a api direto. Com o proxy
    # no servidor Next.js isso nao acontece: requisicao servidor-para-servidor
    # nao passa por CORS. Fica aqui para destravar chamada direta do browser
    # sem mudanca de codigo.
    #
    # Allowlist explicita, JAMAIS "*": a api exige credencial em toda rota, e
    # curinga com credencial e invalido no protocolo.
    origens = settings.cors_origins
    if origens:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origens,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=600,
        )
        log.info("cors.enabled", extra={"origins": origens})

    # ------------------------------------------------------------------
    # Liveness: a UNICA rota sem autenticacao, e de proposito.
    #
    # Sem ela e impossivel distinguir "container morto" de "autenticacao
    # funcionando": as duas situacoes dao erro no navegador. Isso torna o
    # diagnostico de deploy adivinhacao.
    #
    # Nao expoe NADA: nem versao de schema, nem config, nem presenca de
    # credencial, nem caminho de banco. So diz que o processo respondeu.
    # Todo dado real continua atras do token, em /api/health.
    # ------------------------------------------------------------------
    @app.get("/")
    def liveness() -> dict[str, str]:
        return {"status": "alive", "service": "fase0a-api", "fase": "0A"}

    app.include_router(router)

    # TEMPORARIO - sai junto com app/sonda_rede.py quando a D18 for registrada.
    app.include_router(router_sonda)

    return app


# Log estruturado desde a importacao do modulo, antes de o uvicorn falar.
_configurar_log_do_processo()

# Instancia que o uvicorn carrega: `uvicorn app.main:app`.
app = criar_app()
