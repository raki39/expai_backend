"""Rotas HTTP do incremento 0.

Sem logica de negocio (secao 10.2.1). As rotas leem o banco, delegam as
travas ao `config.service` e devolvem JSON.

Nenhuma rota expoe segredo, nem parcialmente (secao 10.2.4).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..config import service as config_service
from ..config.service import ConfigCongelada, SemMudanca, TetoExcedido
from ..security import exigir_token_de_servico
from ..settings import Settings
from ..store import versao_schema, volume_gravavel

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(exigir_token_de_servico)])


def _conn(request: Request) -> sqlite3.Connection:
    return request.app.state.conn


def _settings(request: Request) -> Settings:
    return request.app.state.settings


# ------------------------------------------------------------------ health


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
        "volume_gravavel": volume_gravavel(settings.db_path.parent),
        "data_dir": str(settings.data_dir),
        "schema_version": versao_schema(conn),
        "config_version": atual.id if atual else None,
        "config_hash": atual.config_hash if atual else None,
        "run_ativo": config_service.run_ativo(conn),
        # Nao e segredo: e configuracao publica de politica de origem.
        "cors_allowed_origins": settings.cors_origins,
        # Presenca das credenciais, nunca o valor (secao 10.2.4).
        "credenciais_configuradas": {
            "anthropic": bool(settings.anthropic_api_key.get_secret_value()),
            "openai": bool(settings.openai_api_key.get_secret_value()),
        },
        "fase": "0A",
        "aviso": (
            "Fase 0A. Nenhuma conclusao estatistica. Nenhum conhecimento "
            "promovido."
        ),
    }


# ------------------------------------------------------------ configuracao


class AlteracaoConfig(BaseModel):
    """Pedido de nova versao de configuracao."""

    author: str = Field(min_length=1, max_length=120)
    changes: dict[str, Any] = Field(min_length=1)
    note: str = Field(default="", max_length=500)


@router.get("/config")
def config_atual(request: Request) -> dict[str, Any]:
    atual = config_service.versao_atual(_conn(request))
    if atual is None:
        raise HTTPException(status_code=503, detail="configuracao nao inicializada")
    return {
        "version_id": atual.id,
        "created_at": atual.created_at,
        "author": atual.author,
        "parent_version_id": atual.parent_version_id,
        "config_hash": atual.config_hash,
        "material": atual.material,
        "note": atual.note,
        "config": atual.config.model_dump(mode="json"),
        "congelada": config_service.run_ativo(_conn(request)) is not None,
    }


@router.get("/config/history")
def config_historico(request: Request, limite: int = 50) -> dict[str, Any]:
    """Autor, data, valor anterior e valor novo (secao 10.2.3)."""
    return {"versions": config_service.historico(_conn(request), limite=limite)}


@router.post("/config", status_code=status.HTTP_201_CREATED)
def config_nova_versao(
    request: Request, pedido: AlteracaoConfig = Body(...)
) -> dict[str, Any]:
    try:
        nova = config_service.criar_versao(
            _conn(request),
            _settings(request),
            alteracoes=pedido.changes,
            author=pedido.author,
            note=pedido.note,
        )
    except ConfigCongelada as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except TetoExcedido as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e))
    except SemMudanca as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e))

    return {
        "version_id": nova.id,
        "config_hash": nova.config_hash,
        "material": nova.material,
        # O painel usa isto para avisar alto (secao 10.2.3).
        "aviso": (
            "Alteracao material: invalida comparacao com runs anteriores."
            if nova.material
            else ""
        ),
    }


# --------------------------------------------------------------- sentinela


@router.post("/sentinel", status_code=status.HTTP_201_CREATED)
def sentinela_criar(request: Request, label: str = Body(embed=True)) -> dict[str, Any]:
    """Grava um marcador para provar que o volume persiste entre deploys."""
    conn = _conn(request)
    cur = conn.execute(
        "INSERT INTO sentinel (label, created_at) VALUES (?, datetime('now'))",
        (label,),
    )
    log.info("sentinel.created", extra={"sentinel_id": cur.lastrowid})
    return {"id": int(cur.lastrowid), "label": label}


@router.get("/sentinel")
def sentinela_listar(request: Request) -> dict[str, Any]:
    linhas = _conn(request).execute(
        "SELECT id, label, created_at FROM sentinel ORDER BY id DESC LIMIT 50"
    ).fetchall()
    return {
        "total": len(linhas),
        "items": [dict(linha) for linha in linhas],
    }
