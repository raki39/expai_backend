"""Rotas de **a1a**: os controles negativos determinísticos de §14.4.

Domínio próprio, e não um parâmetro de `/api/b4`: B4 é o grupo de controle do
**agente** e mede busca de parâmetro contra reflexão; A1a é o grupo de controle
do **protocolo** e mede se o pipeline tem defeito. São perguntas diferentes com
tolerâncias diferentes — zero aqui, FDR pré-registrado lá — e juntá-las numa
rota só faria "controle promovido" perder o significado.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, status

from ...a1a import braco as a1a_braco
from ...config import service as config_service
from ...config.service import SchemaDivergente
from ...dataset import loader as dataset_loader
from ..comum import _conn, _settings
from ..modelos import PedidoA1a

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/a1a", tags=["a1a"])


@router.get("")
def a1a_estado(request: Request) -> dict[str, Any]:
    """Os controles sob a config vigente, derivados do banco."""
    conn = _conn(request)
    atual = config_service.versao_atual(conn)
    if atual is None:
        return {"existe": False, "motivo": "configuracao nao inicializada"}
    return {"existe": True, **a1a_braco.resumo(conn, atual.id)}


@router.post("", status_code=status.HTTP_201_CREATED)
def a1a_rodar(request: Request, pedido: PedidoA1a = Body(...)) -> dict[str, Any]:
    """Injeta os seis controles. **Não gasta dinheiro nenhum.**

    Mesma ordem de exigências de B4 — dataset separado, baselines rodados —, e
    cada uma recusa ANTES de conceder ou cobrar crédito.
    """
    conn = _conn(request)
    if config_service.run_ativo(conn) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="encerre o run ativo antes de rodar A1a",
        )
    try:
        config_service.exigir_hash_integro(conn)
    except SchemaDivergente as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    atual = config_service.versao_atual(conn)
    if atual is None:
        raise HTTPException(status_code=503, detail="configuracao nao inicializada")
    meta = dataset_loader.dataset_vigente(conn)
    if meta is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ingira o dataset antes de rodar A1a",
        )

    log.info("a1a.pedido", extra={"author": pedido.author})
    try:
        resultado = a1a_braco.rodar(
            conn,
            dataset_id=meta.id,
            config=atual.config,
            config_version_id=atual.id,
            settings=_settings(request),
        )
    except (a1a_braco.SeparacaoAusente, a1a_braco.BaselineAusente) as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
    return resultado.como_dict()
