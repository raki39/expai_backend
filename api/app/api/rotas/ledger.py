"""Rotas de **ledger**.

Partidas dobradas, dois livros, e o ciclo de vida do run.
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
from ...ledger import livro
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from typing import Any
from ..modelos import PedidoRun
from ..comum import _conn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ledger", tags=["ledger"])


@router.get("")
def ledger_estado(request: Request) -> dict[str, Any]:
    """Saldos derivados e o resultado das conferencias.

    As conferencias vao junto do saldo de proposito: um numero de dinheiro sem
    a prova de que o livro fecha e so um numero.
    """
    conn = _conn(request)
    ativo = config_service.run_ativo(conn)
    violacoes = livro.conferir_partidas_dobradas(conn)
    divergencias = livro.reconciliar(conn)
    vinculos = livro.conferir_vinculo_inferencia(conn)

    return {
        "run_ativo": ativo,
        # Sem run ativo, o que vem e o LIVRO INTEIRO - a soma de todos os runs
        # que ja existiram. E um numero legitimo ("quanto ja passou por esta
        # conta"), mas responde outra pergunta que "quanto este run tem", e o
        # painel precisa dizer qual das duas esta mostrando. Rotulo errado
        # aqui faz somar duas comparacoes e ler o total como carteira.
        "escopo": "run" if ativo else "livro_inteiro",
        "runs_somados": (
            1 if ativo
            else conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"]
        ),
        "carteira": livro.carteira(conn, run_id=ativo),
        "contas": livro.saldos(conn, run_id=ativo) if ativo else livro.saldos(conn),
        "conferencias": {
            "partidas_dobradas_ok": not violacoes,
            "violacoes": violacoes,
            "saldo_reconciliado_ok": not divergencias,
            "divergencias": divergencias,
            "vinculos_ok": not any(vinculos.values()),
            "vinculos": vinculos,
            "sem_ponto_flutuante": livro.colunas_em_ponto_flutuante(conn) == [],
        },
        "eventos": conn.execute(
            "SELECT COUNT(*) AS n FROM agent_event"
        ).fetchone()["n"],
        "transacoes": conn.execute(
            "SELECT COUNT(*) AS n FROM ledger_transaction"
        ).fetchone()["n"],
    }


@router.get("/transacoes")
def ledger_transacoes(request: Request, limite: int = 50) -> dict[str, Any]:
    """Historico. Estorno e original aparecem os dois - nada e apagado."""
    linhas = _conn(request).execute(
        """
        SELECT t.id, t.kind, t.occurred_at, t.posted_at, t.run_id,
               t.fx_rate_micro, t.fx_rate_date, t.agent_event_id,
               t.reverses_transaction_id, t.memo,
               (SELECT COUNT(*) FROM ledger_entry WHERE transaction_id = t.id)
                   AS lancamentos
        FROM ledger_transaction t
        ORDER BY t.id DESC LIMIT ?
        """,
        (limite,),
    ).fetchall()
    return {"total": len(linhas), "items": [dict(l) for l in linhas]}


@router.post("/run", status_code=status.HTTP_201_CREATED)
def run_abrir(request: Request, pedido: PedidoRun = Body(...)) -> dict[str, Any]:
    """Abre um run e credita o capital semente como lancamento (criterio 7)."""
    conn = _conn(request)
    if config_service.run_ativo(conn) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ja existe run ativo; encerre antes de abrir outro",
        )
    atual = config_service.versao_atual(conn)
    if atual is None:
        raise HTTPException(status_code=503, detail="configuracao nao inicializada")

    # O hash e a IDENTIDADE da config do run. Se ele nao descreve mais a
    # configuracao, nenhum resultado produzido aqui pode ser comparado.
    try:
        config_service.exigir_hash_integro(conn)
    except SchemaDivergente as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    log.info("run.abertura_pedida", extra={"author": pedido.author})
    run_id, tx_id = livro.abrir_run(
        conn,
        config_version_id=atual.id,
        seed_capital_usd_cents=atual.config.seed_capital_usd_cents,
    )
    return {
        "run_id": run_id,
        "transaction_id": tx_id,
        "config_version_id": atual.id,
        "seed_capital_usd_cents": atual.config.seed_capital_usd_cents,
        "aviso": "a configuracao esta congelada enquanto o run estiver ativo",
    }


@router.post("/run/{run_id}/encerrar")
def run_encerrar(
    request: Request, run_id: int, estado: str = Body(embed=True)
) -> dict[str, Any]:
    try:
        livro.encerrar_run(_conn(request), run_id, estado)
    except livro.TransacaoInvalida as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
    return {"run_id": run_id, "state": estado}
