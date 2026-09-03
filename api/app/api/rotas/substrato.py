"""Rotas de **substrato**.

Prontidao do processo e do volume. E o que o painel le primeiro.
"""

from __future__ import annotations

import logging
from ... import fase as fase_mod
from ...config import service as config_service
from ...store import (
    conexao_do_thread,
    versao_schema,
    volume_gravavel,
    volume_montado,
)
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from typing import Any
from ..comum import _conn, _settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/substrato", tags=["substrato"])


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    """Estado do substrato. Sem segredo, nem parcial."""
    settings = _settings(request)
    conn = _conn(request)
    atual = config_service.versao_atual(conn)

    return {
        "status": "ok",
        "app_env": settings.app_env,
        "build": request.app.state.build_id,
        "db_path": str(settings.db_path),
        "db_path_absoluto": settings.db_path.is_absolute(),
        # Escrever com sucesso NAO prova persistencia (o /data pode ser da
        # propria imagem). As duas linhas dizem coisas diferentes.
        "volume_gravavel": volume_gravavel(settings.db_path.parent),
        "volume_montado": volume_montado(settings.db_path.parent),
        "data_dir": str(settings.data_dir),
        "schema_version": versao_schema(conn),
        "config_version": atual.id if atual else None,
        "config_hash": atual.config_hash if atual else None,
        # O hash gravado ainda descreve esta configuracao? Se nao, ele parou
        # de identificar o que diz identificar - e o painel precisa gritar.
        "config_hash_confere": (
            config_service.conferir_hash(atual) is None if atual else None
        ),
        "run_ativo": config_service.run_ativo(conn),
        # Nao e segredo: e configuracao publica de politica de origem.
        "cors_allowed_origins": settings.cors_origins,
        # Presenca das credenciais, nunca o valor (secao 10.2.4).
        "credenciais_configuradas": {
            "anthropic": bool(settings.anthropic_api_key.get_secret_value()),
            "openai": bool(settings.openai_api_key.get_secret_value()),
        },
        # De `app.fase`. O comentario que estava aqui afirmava "a fase vem
        # daqui e de nenhum outro lugar" - e era falso: havia mais dois
        # lugares, e os tres discordavam. Afirmar unicidade nao a produz.
        "fase": fase_mod.FASE,
        "aviso": fase_mod.AVISO,
    }
