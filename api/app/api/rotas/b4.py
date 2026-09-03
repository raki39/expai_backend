"""Rotas de **b4**.

O controle nao cognitivo da secao 14.3: busca aleatoria e varredura de
parametro sobre o mesmo catalogo, pelo mesmo validador e com o mesmo
orcamento de creditos do agente.

Dominio proprio, e nao um parametro de `/api/agente`: sao dois bracos do
experimento, e a comparacao entre eles e o produto da fase. Pendurar B4 numa
flag da rota do agente faria o painel e o Swagger apresentarem como variacao
de um agente o que e o grupo de controle dele.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, status

from ...b4 import braco as b4_braco
from ...config import service as config_service
from ...config.service import SchemaDivergente
from ...dataset import loader as dataset_loader
from ..comum import _conn, _settings
from ..modelos import PedidoB4

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/b4", tags=["b4"])


@router.get("")
def b4_estado(request: Request) -> dict[str, Any]:
    """O braco B4 sob a config vigente, derivado do banco.

    Nada guardado: as hipoteses de B4 sao linhas de `hypothesis` com
    `agente_origem` proprio, e o resumo e consulta sobre elas.
    """
    conn = _conn(request)
    atual = config_service.versao_atual(conn)
    if atual is None:
        return {"existe": False, "motivo": "configuracao nao inicializada"}
    return {"existe": True, **b4_braco.resumo(conn, atual.id)}


@router.post("", status_code=status.HTTP_201_CREATED)
def b4_rodar(request: Request, pedido: PedidoB4 = Body(...)) -> dict[str, Any]:
    """Roda as 16 hipoteses de B4. **Nao gasta dinheiro nenhum.**

    E a diferenca mais visivel entre esta rota e a do agente: B4 nao consome
    tokens (secao 14.3), so CPU. Os tetos de gasto nem entram no caminho
    porque nao ha gasto a limitar.

    A ordem que ela exige e a mesma do painel - dataset separado, baselines
    rodados, e so entao o braco - e cada exigencia recusa ANTES de cobrar
    credito: recusar depois de gastar seria cobrar pelo que nao se mediu.
    """
    conn = _conn(request)
    if config_service.run_ativo(conn) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="encerre o run ativo antes de rodar B4",
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
            detail="ingira o dataset antes de rodar B4",
        )

    log.info("b4.pedido", extra={"author": pedido.author})
    try:
        resultado = b4_braco.rodar(
            conn,
            dataset_id=meta.id,
            config=atual.config,
            config_version_id=atual.id,
            settings=_settings(request),
            semente=pedido.semente,
        )
    except (b4_braco.SeparacaoAusente, b4_braco.BaselineAusente) as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except sqlite3.IntegrityError as e:
        # A familia fechada recusando a hipotese excedente (R38). Nao e erro
        # do servidor: e o teto funcionando, e o texto do gatilho diz qual.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
    return resultado.como_dict()
