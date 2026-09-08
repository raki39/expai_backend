"""Rotas de **diagnostico**.

So para PROVAR o substrato, nunca para o experimento. A sentinela existe desde o incremento 0 para demonstrar que o volume persiste entre deploys - ela nao participa de run nenhum, e nada do experimento a le.
"""

from __future__ import annotations

import logging
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from typing import Any
from ..comum import _conn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/diagnostico", tags=["diagnostico"])


@router.post("/sentinela", status_code=status.HTTP_201_CREATED)
def sentinela_criar(request: Request, label: str = Body(embed=True)) -> dict[str, Any]:
    """Grava um marcador para provar que o volume persiste entre deploys."""
    conn = _conn(request)
    cur = conn.execute(
        "INSERT INTO sentinel (label, created_at) VALUES (?, datetime('now'))",
        (label,),
    )
    log.info("sentinel.created", extra={"sentinel_id": cur.lastrowid})
    return {"id": int(cur.lastrowid), "label": label}


@router.get("/sentinela")
def sentinela_listar(request: Request) -> dict[str, Any]:
    linhas = _conn(request).execute(
        "SELECT id, label, created_at FROM sentinel ORDER BY id DESC LIMIT 50"
    ).fetchall()
    return {
        "total": len(linhas),
        "items": [dict(linha) for linha in linhas],
    }


@router.get("/integridade")
def integridade(request: Request) -> dict[str, Any]:
    """As cinco conferencias, todas DERIVADAS de consulta.

    Pedida depois de uma reancoragem, e o motivo e a quinta: a reancoragem
    cria `config_version` NOVA, e a pergunta certa nao e se ela existe - e se
    algum run ANTIGO passou a apontar para ela.

    Ele nao pode: `run.config_version_id` nasce com o run e `config_version` e
    imutavel por gatilho. Mas "nao pode" e afirmacao sobre o desenho, e esta
    rota MEDE - que e a diferenca que este projeto inteiro persegue.

    Fica em `diagnostico` porque nao participa de run nenhum e nada do
    experimento a le. Ela prova o substrato, como a sentinela.
    """
    from ...relatorio import integridade as mod

    return mod.montar(_conn(request))
