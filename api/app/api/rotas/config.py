"""Rotas de **config**.

Configuracao versionada do experimento (secao 10.2.3).
"""

from __future__ import annotations

import logging
from ...config import service as config_service
from ...config.service import (
    ConfigCongelada,
    SchemaDivergente,
    SemDeriva,
    SemMudanca,
    TetoExcedido,
)
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from typing import Any
from ..modelos import AlteracaoConfig, PedidoReancoragem
from ..comum import _conn, _settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
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
        # Campos de catalogo em que o banco discorda do catalogo verificado.
        # Nao e "o banco esta errado": o banco e a autoridade (regra 20). E a
        # constatacao de que existe diferenca, para que ela seja resolvida por
        # ato humano registrado em vez de passar despercebida.
        "catalogo_desatualizado": config_service.catalogo_desatualizado(
            _conn(request)
        ),
    }


@router.get("/historico")
def config_historico(request: Request, limite: int = 50) -> dict[str, Any]:
    """Autor, data, valor anterior e valor novo (secao 10.2.3)."""
    return {"versions": config_service.historico(_conn(request), limite=limite)}


@router.post("", status_code=status.HTTP_201_CREATED)
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


@router.post("/catalogo", status_code=status.HTTP_201_CREATED)
def config_adotar_catalogo(
    request: Request, pedido: PedidoReancoragem = Body(...)
) -> dict[str, Any]:
    """Adota o catalogo de provedores verificado: tiers e tabela de precos.

    Toca APENAS esses campos, e cria uma `config_version` como qualquer outra
    alteracao, com autor, data, valor anterior e novo. E material: preco
    alimenta o teto, e o teto decide quantas reflexoes cabem num run.
    """
    try:
        nova = config_service.adotar_catalogo_de_provedores(
            _conn(request), _settings(request),
            author=pedido.author, note=pedido.note,
        )
    except ConfigCongelada as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except SemMudanca as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except TetoExcedido as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
    return {
        "version_id": nova.id,
        "config_hash": nova.config_hash,
        "material": nova.material,
        "aviso": (
            "Alteracao material: invalida comparacao com runs anteriores."
            if nova.material else ""
        ),
    }


@router.post("/reancorar", status_code=status.HTTP_201_CREATED)
def config_reancorar(
    request: Request, pedido: PedidoReancoragem = Body(...)
) -> dict[str, Any]:
    """Regrava a config vigente sob o hash correto, apos mudanca de schema.

    Nao altera valor nenhum. Existe porque `criar_versao` devolveria
    `SemMudanca` neste caso - a configuracao efetiva nao mudou, so o hash.
    """
    try:
        nova = config_service.reancorar(
            _conn(request), _settings(request),
            author=pedido.author, note=pedido.note,
        )
    except SemDeriva as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ConfigCongelada as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except TetoExcedido as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
    return {
        "version_id": nova.id,
        "config_hash": nova.config_hash,
        "material": nova.material,
        "aviso": (
            "Configuracao reancorada. O schema mudou: toda comparacao que "
            "atravesse esta versao e invalida."
        ),
    }
