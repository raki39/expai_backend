"""Rotas de **agente**.

O ciclo do cerebro lento: observa, propoe, registra a intencao, executa.
"""

from __future__ import annotations

import logging
from ...cerebro import avaliacao as cerebro_avaliacao
from ...cerebro import cache as cerebro_cache
from ...cerebro import ciclo as cerebro_ciclo
from ...cerebro import paradas
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
from ...hipotese import registro as hipotese_registro
from ...ledger import livro
from ...maos_rapidas import baselines
from ...maos_rapidas import executor as maos_executor
from ...simulador import execucao as simulador
from ...validador import promocao as validador_promocao
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
    # O ultimo run DO AGENTE, e nao o ultimo run com evento.
    #
    # Ate o incremento 12 os dois eram a mesma coisa: so o cerebro emitia
    # `agent_event`. B4 tambem emite - evento nao cognitivo, provedor nulo -,
    # e no primeiro B4 rodado em producao esta rota passou a mostrar o run 52,
    # do controle, com o pre-registro dele e a atribuicao dele.
    #
    # O `braco.AGENT_ID = "b4-0001"` foi escrito COM UM COMENTARIO dizendo que
    # impedia exatamente isso. Nao impedia: o filtro daqui era sobre
    # `agent_event`, e nao sobre o dono do run. Comentario afirmando protecao
    # que nao existe e o padrao que este projeto ja registrou treze vezes, e
    # esta foi escrita por mim no mesmo incremento.
    #
    # O filtro e POSITIVO - `= AGENT_ID_PADRAO` - e nao a exclusao de B4: um
    # terceiro braco que emita evento nao voltaria a aparecer aqui por
    # esquecimento.
    linha = conn.execute(
        "SELECT MAX(e.run_id) AS run_id FROM agent_event e"
        " JOIN run r ON r.id = e.run_id"
        " WHERE r.agent_id = ?",
        (livro.AGENT_ID_PADRAO,),
    ).fetchone()
    run_id = linha["run_id"] if linha else None
    if run_id is None:
        return {"run_id": None, "caminho": [], "propostas": [], "gasto": None}

    carteira = livro.carteira(conn, run_id=int(run_id))

    # Quem pode chamar isto de "resultado do agente" - derivado do que ficou
    # gravado, nunca de um campo do run.
    #
    # Este bloco existe porque a alternativa ja produziu um numero errado em
    # producao: o run 27 apareceu aqui com `faixa: "entre_p50_e_p95"` sobre
    # 244 idas e voltas que NENHUMA decisao cognitiva escolheu - o cerebro
    # havia parado em `propor_regra` e a regra padrao rodou por baixo.
    # `regra_veio_do_cerebro` era calculado no ciclo, ia no corpo do POST e
    # sumia aqui.
    parada = cerebro_ciclo.parada_do_run(conn, int(run_id))
    ativa = propostas_de_regra.regra_ativa(conn, int(run_id))
    idas_e_voltas = maos_executor.idas_e_voltas(conn, int(run_id))
    ordens = maos_executor.ordens_executadas(conn, int(run_id))
    atribuicao = paradas.atribuicao(
        veio_do_cerebro=ativa is not None,
        categoria=(parada or {}).get("categoria"),
        executou=ordens > 0,
    )
    do_agente = bool(atribuicao["atribuivel_ao_agente"])
    b1 = baselines.b1_do_agente(conn)
    hypothesis_id = conn.execute(
        "SELECT MAX(id) AS id FROM hypothesis WHERE run_id = ?", (int(run_id),)
    ).fetchone()["id"]

    return {
        "run_id": int(run_id),
        "parada": parada,
        "atribuicao": atribuicao,
        # O resultado economico do run, do ledger. Sem ele o relatorio diz
        # quantas operacoes houve e nao diz se sobrou dinheiro - que e a unica
        # pergunta que a comparacao com os baselines responde.
        "patrimonio_final_cents": simulador.caixa_cents(conn, int(run_id)),
        # DUAS unidades, e nomeadas. `operacoes` sozinho era ambiguo: valia
        # 244 aqui (idas e voltas) ao lado de `operacoes_alvo` do B1, que e a
        # mesma coisa, e ao lado de `execucoes` do simulador, que e o dobro.
        # O CLAUDE.md ja registra esse exato erro de unidade uma vez, na
        # tabela de comparacao do incremento 7.
        #
        # A mesma definicao que o relatorio e a comparacao usam. Era
        # `COUNT(*) / 2` aqui e contagem de compras la: iguais enquanto toda
        # compra fecha, divergentes no run que termina comprado.
        "idas_e_voltas": idas_e_voltas,
        "ordens_executadas": ordens,
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
        "b1_casado": b1,
        # A D19 CONFERIDA, e nao suposta.
        #
        # `b1_do_agente` devolve o ultimo B1 casado gravado, globalmente - nao
        # ha ligacao entre o run do B1 e o run que ele casa. Enquanto so o
        # agente produzia B1 casado, o ultimo era sempre o do ultimo run dele.
        #
        # Em producao a tela mostrou 37 idas e voltas do run ao lado de um
        # controle de 70, porque o run exibido era outro. A D19 existe
        # exatamente para impedir comparar giros diferentes, e o defeito
        # aparecia como uma tabela plausivel.
        #
        # Enquanto a ligacao nao existe (incremento 13), o campo DIZ quando
        # nao casa, em vez de deixar a tabela mentir.
        "b1_casado_confere": (
            None if not b1 else {
                "casa": int(b1.get("operacoes_alvo") or -1) == idas_e_voltas,
                "operacoes_alvo": b1.get("operacoes_alvo"),
                "idas_e_voltas_do_run": idas_e_voltas,
                "por_que_importa": (
                    "cada ida e volta paga um pedagio fixo, entao comparar"
                    " giros diferentes mede giro e nao escolha de momento"
                    " (D19, secao 14.3)"
                ),
            }
        ),
        # Onde o resultado caiu na distribuicao do acaso. Vem da api porque
        # classificar e decidir, e decidir sobre o experimento nao acontece no
        # painel (regra 19). Ele comparava com p5/p50/p95 na tela - e a mesma
        # regua ficava escrita em dois lugares, livre para divergir.
        # `None` quando o resultado nao e do agente. A faixa nao e um dado
        # neutro: "entre_p50_e_p95" e uma afirmacao sobre a competencia dele,
        # e sobre um run em que ele nao decidiu nada e uma afirmacao falsa.
        # Zerar o campo perde informacao; deixa-lo mente. O `atribuicao`
        # ao lado diz por que ele esta vazio.
        "faixa": (
            cerebro_avaliacao.faixa_contra_o_acaso(
                simulador.caixa_cents(conn, int(run_id)), b1
            )
            if do_agente
            else None
        ),
        "caminho": cerebro_ciclo.caminho_percorrido(conn, int(run_id)),
        "propostas": propostas_de_regra.do_run(conn, int(run_id)),
        "regra_ativa": ativa,
        "gasto": livro.gasto_com_reflexao(conn, int(run_id)),
        # O parecer INDEPENDENTE do validador (§8.1), ao lado da
        # autoavaliacao do agente. Vinha so no corpo do POST do ciclo: o
        # painel mostrava a avaliacao do agente sobre si mesmo e **nao** a do
        # validador - que e justamente a separacao de que a 0B trata. Terceiro
        # campo desta rota com o mesmo defeito, depois de `motivo` e
        # `regra_veio_do_cerebro`.
        # O PRE-REGISTRO de §8.2, estruturado. Ele existe no banco desde o
        # incremento 8 e nao chegava a tela nenhuma: o painel mostrava a
        # `expectation` em prosa, que era o campo da 0A. A metade que torna o
        # veredito uma conta - `efeito_minimo`, `n_minimo`,
        # `condicoes_falseamento` - ficava invisivel.
        #
        # Vem montado daqui, e nao parseado do `raw_response_json` na tela: o
        # painel nao contem logica de negocio (regra 19).
        "pre_registro": (
            hipotese_registro.por_id(conn, int(hypothesis_id))
            if hypothesis_id is not None
            else None
        ),
        "parecer_do_validador": (
            validador_promocao.parecer_derivado(conn, int(hypothesis_id))
            if hypothesis_id is not None
            else None
        ),
        "hypothesis_id": hypothesis_id,
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
