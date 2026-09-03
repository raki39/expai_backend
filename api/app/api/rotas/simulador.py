"""Rotas de **simulador**.

Execucao pessimista e o que ela produziu (secao 8.4.1).
"""

from __future__ import annotations

import logging
from ...config import service as config_service
from ...simulador import execucao as simulador
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from typing import Any
from ..comum import _conn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/simulador", tags=["simulador"])


@router.get("")
def simulador_estado(request: Request) -> dict[str, Any]:
    """Agregado das execucoes do run ativo.

    A fidelidade e as condicoes de validade vao SEMPRE junto do numero
    (criterio 5, secao 8.4.1.1). Um custo agregado sem o nivel de fidelidade
    que o produziu convida a conclusao que a Fase 0A nao pode sustentar.
    """
    conn = _conn(request)
    ativo = config_service.run_ativo(conn)
    # Sem run ativo, mostra o ULTIMO que teve execucao. A tela ficava zerada
    # com milhares de execucoes gravadas, porque a comparacao encerra os runs
    # que ela abre - e "nao ha run ativo" nao e a mesma coisa que "nao houve
    # execucao nenhuma".
    alvo = ativo
    if alvo is None:
        linha = conn.execute(
            "SELECT MAX(run_id) AS run_id FROM execution"
        ).fetchone()
        alvo = int(linha["run_id"]) if linha and linha["run_id"] else None
    if alvo is None:
        atual = config_service.versao_atual(conn)
        return {
            "run_ativo": None,
            "run_exibido": None,
            "condicoes_validade": (
                simulador.condicoes_de_validade(atual.config) if atual else ""
            ),
        }
    return {
        "run_ativo": ativo,
        "run_exibido": alvo,
        "encerrado": ativo is None,
        **simulador.resumo(conn, alvo),
    }


@router.get("/execucoes")
def execucoes_listar(request: Request, limite: int = 50) -> dict[str, Any]:
    linhas = _conn(request).execute(
        "SELECT id, run_id, side, decision_bar_ms, execution_bar_ms,"
        " quantity_sats, price_ref, price_exec, notional_ref_cents, fee_cents,"
        " spread_cents, slippage_cents, penalty_cents, fidelity_level,"
        " ledger_transaction_id FROM execution ORDER BY id DESC LIMIT ?",
        (limite,),
    ).fetchall()
    atual = config_service.versao_atual(_conn(request))
    return {
        "total": len(linhas),
        "items": [dict(l) for l in linhas],
        "condicoes_validade": (
            simulador.condicoes_de_validade(atual.config) if atual else ""
        ),
    }
