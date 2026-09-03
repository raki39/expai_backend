"""Rotas de **baselines**.

B1, B2 e B3 - o grupo de controle da secao 14.3.
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
from ...dataset import loader as dataset_loader
from ...maos_rapidas import baselines
from ...maos_rapidas import curva as curva_de_patrimonio
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from typing import Any
from ..modelos import PedidoComparacao
from ..comum import _conn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/baselines", tags=["baselines"])


@router.get("")
def comparacao_atual(request: Request) -> dict[str, Any]:
    return baselines.resumo_comparacao(_conn(request))


@router.post("", status_code=status.HTTP_201_CREATED)
def comparacao_rodar(
    request: Request, pedido: PedidoComparacao = Body(...)
) -> dict[str, Any]:
    """Roda B1, B2 e B3 sobre a mesma janela, mesmo simulador, mesmo custo.

    Nenhum LLM e envolvido. Se o encanamento nao fecha sem o modelo, o
    problema nao e o modelo.
    """
    conn = _conn(request)
    if config_service.run_ativo(conn) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="encerre o run ativo antes de rodar a comparacao",
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
            detail="ingira o dataset antes de rodar a comparacao",
        )

    semente = pedido.semente if pedido.semente is not None else atual.config.default_seed
    log.info("comparacao.pedida",
             extra={"author": pedido.author, "semente": semente})
    try:
        return baselines.rodar_comparacao(
            conn, dataset_id=meta.id, config=atual.config,
            config_version_id=atual.id, semente=semente,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )


@router.get("/curva")
def curva(request: Request, pontos: int = curva_de_patrimonio.PONTOS_PADRAO) -> dict[str, Any]:
    """Curva de patrimonio do agente e dos baselines, na MESMA escala (§6.2).

    Derivada das execucoes gravadas. Nao ha tabela de curva: uma serie
    persistida ao lado do ledger seria segunda fonte de verdade sobre
    dinheiro (regra 16).

    B1 nao tem curva e nunca vai ter: sao mil repeticoes das quais so o
    resultado final e guardado. Ele entra como FAIXA do resultado final, e
    desenha-lo atravessando o tempo afirmaria que mil caminhos foram
    acompanhados - um grafico que mente sem que nenhum numero esteja errado.
    """
    conn = _conn(request)
    meta = dataset_loader.dataset_vigente(conn)
    if meta is None:
        return {"existe": False, "motivo": "dataset ainda nao ingerido"}

    runs: dict[str, int] = {}
    for marcador, chave in (("baseline-B2", "B2"), ("baseline-B3", "B3")):
        linha = conn.execute(
            "SELECT MAX(id) AS id FROM run WHERE agent_id = ?", (marcador,)
        ).fetchone()
        if linha and linha["id"]:
            runs[chave] = int(linha["id"])

    # O run do agente e derivado: e o ultimo que tem evento cognitivo. Nao ha
    # coluna "e do agente" para ficar desatualizada.
    agente = conn.execute(
        "SELECT MAX(run_id) AS id FROM agent_event WHERE run_id IS NOT NULL"
    ).fetchone()
    if agente and agente["id"]:
        runs["agente"] = int(agente["id"])

    if not runs:
        return {"existe": False, "motivo": "nenhum run para desenhar"}

    atual = config_service.versao_atual(conn)
    curvas = curva_de_patrimonio.curvas_da_comparacao(
        conn, dataset_id=meta.id, config=atual.config, runs=runs,
        pontos=max(50, min(pontos, 2_000)),
    )
    comparacao = baselines.resumo_comparacao(conn)

    finais = {
        nome: (c[-1]["patrimonio_cents"] if c else None)
        for nome, c in curvas.items()
    }
    # Regra 14: desempenho SEMPRE como excesso sobre baseline. O absoluto
    # responde "quanto sobrou", que nao e a pergunta do experimento.
    excessos = {}
    if finais.get("agente") is not None:
        for base in ("B2", "B3"):
            if finais.get(base) is not None:
                excessos[f"agente_sobre_{base}"] = (
                    curva_de_patrimonio.excesso_sobre_baseline_cents(
                        finais["agente"], finais[base]
                    )
                )
        # Contra o B1 CASADO com o giro do agente, e nao contra o da
        # comparacao, que e casado com o B3 (D19).
        casado = baselines.b1_do_agente(conn)
        if casado:
            excessos["agente_sobre_B1_casado_p50"] = (
                curva_de_patrimonio.excesso_sobre_baseline_cents(
                    finais["agente"], casado["p50"]
                )
            )

    return {
        "existe": True,
        "curvas": curvas,
        "finais_cents": finais,
        "excesso_cents": excessos,
        # A faixa de B1 e do RESULTADO FINAL, e nao um caminho. A do agente
        # e a casada com o giro dele; a da comparacao e casada com o B3.
        "b1_faixa_final": baselines.b1_do_agente(conn) or comparacao.get("B1"),
        "b1_faixa_da_comparacao": comparacao.get("B1"),
        "runs": runs,
        "config_version_vigente": comparacao.get("config_version_vigente"),
        "sob_a_config_vigente": comparacao.get("sob_a_config_vigente"),
        "aviso": (
            "Entre uma compra e a venda seguinte a curva marca a posicao ao"
            " fechamento da barra: e otimista no meio e exata nas pontas,"
            " onde a posicao esta zerada. Vender pagaria custos que a"
            " marcacao nao desconta."
        ),
    }
