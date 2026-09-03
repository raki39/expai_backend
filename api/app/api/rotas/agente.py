"""Rotas de **agente**.

O ciclo do cerebro lento: observa, propoe, registra a intencao, executa.
"""

from __future__ import annotations

import logging
from ...cerebro import avaliacao as cerebro_avaliacao
from ...cerebro import cache as cerebro_cache
from ...cerebro import ciclo as cerebro_ciclo
from ...cerebro import propostas as propostas_de_regra
from ...config import service as config_service
from ...config.service import (
    ConfigCongelada,
    SchemaDivergente,
    SemDeriva,
    SemMudanca,
    TetoExcedido,
)
from ...dataset import loader as dataset_loader
from ...ledger import livro
from ...maos_rapidas import baselines
from ...maos_rapidas import executor as maos_executor
from ...simulador import execucao as simulador
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from typing import Any
from ..modelos import PedidoAgente
from ..comum import _conn, _settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agente", tags=["agente"])


@router.get("")
def agente_estado(request: Request) -> dict[str, Any]:
    """O ultimo run do agente: caminho percorrido, propostas e gasto.

    Um run do agente e um run que tem `agent_event` - derivado, e nao uma
    coluna nova: quem tem evento cognitivo passou pelo cerebro, e quem nao
    tem, nao passou. Nao ha estado para ficar desatualizado.
    """
    conn = _conn(request)
    linha = conn.execute(
        "SELECT MAX(run_id) AS run_id FROM agent_event WHERE run_id IS NOT NULL"
    ).fetchone()
    run_id = linha["run_id"] if linha else None
    if run_id is None:
        return {"run_id": None, "caminho": [], "propostas": [], "gasto": None}

    carteira = livro.carteira(conn, run_id=int(run_id))
    return {
        "run_id": int(run_id),
        # O resultado economico do run, do ledger. Sem ele o relatorio diz
        # quantas operacoes houve e nao diz se sobrou dinheiro - que e a unica
        # pergunta que a comparacao com os baselines responde.
        "patrimonio_final_cents": simulador.caixa_cents(conn, int(run_id)),
        # A mesma definicao que o relatorio e a comparacao usam. Era
        # `COUNT(*) / 2` aqui e contagem de compras la: iguais enquanto toda
        # compra fecha, divergentes no run que termina comprado.
        "operacoes": maos_executor.idas_e_voltas(conn, int(run_id)),
        "custos_cents": {
            "execucao_total": carteira["simulado_usd"]["custo_execucao_minor"],
            "reflexao_total": carteira["simulado_usd"]["tesouraria_minor"],
            "posicao_aberta": carteira["simulado_usd"]["posicao_btc_minor"],
        },
        "config_version_id": conn.execute(
            "SELECT config_version_id FROM run WHERE id = ?", (int(run_id),)
        ).fetchone()["config_version_id"],
        # O controle do acaso casado com o giro DESTE run (D19). O B1 da
        # comparacao e casado com o B3 e tem outro giro - comparar o agente
        # com aquele mediria giro em vez de escolha de momento.
        "b1_casado": baselines.b1_do_agente(conn),
        # Onde o resultado caiu na distribuicao do acaso. Vem da api porque
        # classificar e decidir, e decidir sobre o experimento nao acontece no
        # painel (regra 19). Ele comparava com p5/p50/p95 na tela - e a mesma
        # regua ficava escrita em dois lugares, livre para divergir.
        "faixa": cerebro_avaliacao.faixa_contra_o_acaso(
            simulador.caixa_cents(conn, int(run_id)), baselines.b1_do_agente(conn)
        ),
        "caminho": cerebro_ciclo.caminho_percorrido(conn, int(run_id)),
        "propostas": propostas_de_regra.do_run(conn, int(run_id)),
        "regra_ativa": propostas_de_regra.regra_ativa(conn, int(run_id)),
        "gasto": livro.gasto_com_reflexao(conn, int(run_id)),
        "sobreposicao_amostral": propostas_de_regra.sobreposicao_amostral(
            conn, int(run_id)
        ),
        "condicoes_validade": simulador.condicoes_do_run(conn, int(run_id)),
        # Quantas vezes o cerebro falou neste run. O painel precisa disto
        # para nao confundir ausencia com zero: por D23, ZERO reflexoes
        # significa que o agente E o B3, que e uma afirmacao forte - e nao
        # pode ser o que a tela mostra so porque o campo nao veio.
        "reflexoes": conn.execute(
            "SELECT COUNT(*) AS n FROM agent_event"
            " WHERE run_id = ? AND provider IS NOT NULL",
            (int(run_id),),
        ).fetchone()["n"],
        "cache_de_respostas": cerebro_cache.tamanho(conn),
        "arredondamento_do_custo_ok": (
            livro.conferir_arredondamento_do_custo(conn) == []
        ),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def agente_rodar(
    request: Request, pedido: PedidoAgente = Body(...)
) -> dict[str, Any]:
    """Fecha o ciclo: observa, reflete, propoe regra, executa, contabiliza.

    E a unica rota do projeto que pode gastar dinheiro de verdade. Os tetos
    ficam dentro do ciclo, e nao aqui: uma trava no caminho HTTP protege
    contra o painel, nao contra o programa (secao 12.1).
    """
    conn = _conn(request)
    if config_service.run_ativo(conn) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="encerre o run ativo antes de rodar o agente",
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
            detail="ingira o dataset antes de rodar o agente",
        )

    log.info("agente.pedido", extra={"author": pedido.author})
    try:
        resultado = cerebro_ciclo.rodar(
            conn,
            dataset_id=meta.id,
            config=atual.config,
            config_version_id=atual.id,
            settings=_settings(request),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
    return resultado.como_dict()
