"""Rotas de **a1b**: as nulas estocásticas em execuções repetidas (§14.4).

Domínio próprio, separado de `a1a`, porque as tolerâncias são diferentes: lá
uma promoção reprova a fase; aqui uma promoção ocasional é o comportamento
esperado de um procedimento com FDR positivo. §14.4 avisa que confundir os dois
"leva a consertar o que não está quebrado, ou pior, a apertar o critério até o
sistema nunca promover nada e parecer rigoroso por ser inútil".

O POST roda um **pedaço** e devolve o acumulado. São 400 execuções (D29) a
~0,85 s cada; uma requisição só levaria quase seis minutos, e a regra 1 proíbe
worker na Fase 0. Cada execução é reprodutível por `(semente, desenho,
indice)`, então o conjunto em pedaços é idêntico ao conjunto de uma vez.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, status

from ...a1b import braco as a1b_braco
from ...config import service as config_service
from ...dataset import loader as dataset_loader
from ..comum import _conn
from ..modelos import PedidoA1b

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/a1b", tags=["a1b"])


@router.get("")
def a1b_estado(request: Request) -> dict[str, Any]:
    """O calibre acumulado, derivado das execuções gravadas."""
    conn = _conn(request)
    atual = config_service.versao_atual(conn)
    if atual is None:
        return {"existe": False, "motivo": "configuracao nao inicializada"}
    meta = dataset_loader.dataset_vigente(conn)
    return {
        "existe": True,
        **a1b_braco.resumo(
            conn, atual.id, atual.config,
            dataset_id=meta.id if meta else None,
        ),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def a1b_rodar(request: Request, pedido: PedidoA1b = Body(...)) -> dict[str, Any]:
    """Roda o próximo pedaço de execuções. **Não gasta dinheiro nenhum.**

    Idempotente por índice: pedir de novo um índice já gravado não o regrava —
    o UNIQUE recusa, e contá-lo duas vezes é o defeito mais fácil de produzir
    num registro que cresce em pedaços.
    """
    conn = _conn(request)
    if config_service.run_ativo(conn) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="encerre o run ativo antes de rodar A1b",
        )
    atual = config_service.versao_atual(conn)
    if atual is None:
        raise HTTPException(status_code=503, detail="configuracao nao inicializada")
    meta = dataset_loader.dataset_vigente(conn)
    if meta is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ingira o dataset antes de rodar A1b",
        )

    log.info(
        "a1b.pedido",
        extra={"author": pedido.author, "quantas": pedido.quantas},
    )
    try:
        return a1b_braco.rodar(
            conn,
            dataset_id=meta.id,
            config=atual.config,
            config_version_id=atual.id,
            quantas=pedido.quantas,
            semente=pedido.semente,
        )
    except a1b_braco.SeparacaoAusente as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
