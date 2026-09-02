"""Aplicacao FastAPI do servico `api`.

Um agente, um processo. O painel e um servico separado (`web`) que fala com
este por rede privada; este servico nao tem dominio publico.

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
from .settings import SECRET_FIELDS, get_settings
from .store import conectar, migrar, volume_gravavel

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

    app.include_router(router)
    return app


# Log estruturado desde a importacao do modulo, antes de o uvicorn falar.
_configurar_log_do_processo()

# Instancia que o uvicorn carrega: `uvicorn app.main:app`.
app = criar_app()
