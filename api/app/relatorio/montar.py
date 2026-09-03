"""O relatorio de fechamento da 0A, montado A PARTIR DO BANCO.

O criterio 1 do incremento 7 e explicito sobre a forma: o relatorio afirma
"com dados do banco e **nao com prosa**". A diferenca nao e estilistica. Um
relatorio de fechamento escrito a mao e a maneira mais elegante que existe de
enganar o proprio autor: ele diria "o ciclo fecha" com a mesma confianca
tivesse fechado ou nao, e nada no texto acusaria a diferenca.

Por isso a **resposta da 0A e derivada**, nunca digitada. Cada uma das dez
condicoes abaixo e um booleano que sai de uma consulta; `fecha` e a conjuncao
delas; e quando alguma e falsa o relatorio diz **qual**. Se a resposta fosse
uma frase, ela sobreviveria a qualquer regressao futura sem mudar uma letra -
que e exatamente o padrao que este projeto ja corrigiu seis vezes.

O texto do que **nao** foi concluido, ao contrario, e fixo de proposito: a
secao 14 da 0A proibe conclusao estatistica e conhecimento promovido, e essa
proibicao nao depende de nenhum dado. Derivar a lista de limites dos dados
permitiria que um dia ela encolhesse sozinha.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ..cerebro import avaliacao, ciclo, propostas
from .. import fase as fase_mod
from ..config import service as config_service
from ..dataset import loader as dataset_loader
from ..ledger import livro
from ..maos_rapidas import baselines, executor
from ..simulador.execucao import condicoes_do_run
from . import reprodutibilidade, vinculo

# Fixo, e nao derivado: e o que a secao 14 proibe afirmar na 0A, e proibicao
# que se calcula dos dados e proibicao que pode desaparecer sozinha.
NAO_CONCLUIDO = [
    "Nenhuma conclusao estatistica. A 0A nao tem tamanho de amostra, nem"
    " correcao para multiplas comparacoes, nem teste de hipotese.",
    "Nenhum conhecimento promovido. Nada do que o agente 'aprendeu' vira"
    " contexto de um proximo run (regra 12).",
    "Fidelidade de simulacao nivel 1, declarada e propagada. Nenhuma"
    " afirmacao sobre fidelidade de book: sem spread real, sem fila, sem"
    " preenchimento maker (secao 8.4.1).",
    "Perfil `neutro@1`, presente e inerte. Nenhum ramo de decisao le o"
    " perfil (regra 18).",
    "Sem B4, sem walk-forward, sem holdout e sem Portao A ou B.",
    "Resultado EM AMOSTRA: o cerebro observou a mesma janela que executou"
    " (D22). A sobreposicao e numero calculado, nao estimativa.",
    "Um agente, um instrumento, um timeframe, uma janela. Nada aqui"
    " generaliza para outro mercado, outro periodo ou outro agente.",
]


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_do_agente(conn: sqlite3.Connection) -> int | None:
    """O run mais recente do agente. Baseline e prova nao contam."""
    linha = conn.execute(
        "SELECT id FROM run WHERE agent_id NOT LIKE 'baseline-%'"
        "   AND agent_id NOT LIKE 'prova-r12-%'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return int(linha["id"]) if linha else None


def montar(conn: sqlite3.Connection, run_id: int | None = None) -> dict:
    """O relatorio inteiro. `run_id` ausente usa o ultimo run do agente."""
    run_id = run_id if run_id is not None else _run_do_agente(conn)
    if run_id is None:
        return {
            "existe": False,
            "motivo": "nenhum run do agente foi executado ainda",
            "gerado_em": _agora(),
            "nao_concluido": NAO_CONCLUIDO,
        }

    run = conn.execute(
        "SELECT id, agent_id, state, config_version_id, created_at, updated_at"
        "  FROM run WHERE id = ?",
        (run_id,),
    ).fetchone()
    if run is None:
        return {"existe": False, "motivo": f"run {run_id} nao existe"}
    run = dict(run)

    # A config SOB A QUAL O RUN FOI ABERTO, nunca a vigente. Ler a vigente
    # faria o relatorio de um run antigo descrever parametros que nao o
    # produziram - o defeito exato que `condicoes_validade` teve.
    versao = config_service.versao_por_id(conn, int(run["config_version_id"]))
    config = versao.config if versao else None

    dataset = None
    meta = dataset_loader.dataset_vigente(conn)
    if meta is not None:
        dataset = dataset_loader.resumo(conn, meta.id)

    # -------------------------------------------------------------- observou
    sobreposicao = propostas.sobreposicao_amostral(conn, run_id)
    janela_observada = conn.execute(
        "SELECT MIN(observed_from_ms) AS de, MAX(observed_to_ms) AS ate"
        "  FROM rule_proposal WHERE run_id = ?",
        (run_id,),
    ).fetchone()

    observou = {
        "dataset_id": dataset["dataset_id"] if dataset else None,
        "dataset_sha256": dataset["sha256"] if dataset else None,
        "barras_disponiveis": dataset["barras_disponiveis"] if dataset else None,
        "barras_reservadas": dataset["barras_reservadas"] if dataset else None,
        "fidelity_level": dataset["fidelity_level"] if dataset else None,
        "janela_observada_de_ms": janela_observada["de"],
        "janela_observada_ate_ms": janela_observada["ate"],
        "sobreposicao_com_a_executada": sobreposicao,
    }

    # -------------------------------------------------------------- refletiu
    reflexoes = [
        dict(l)
        for l in conn.execute(
            "SELECT id, occurred_at, node, kind, tier, provider, model,"
            "       tokens_in, tokens_out, tokens_cache_read, tokens_cache_write,"
            "       cost_usd_minor, cost_usd_micro, price_table_version"
            "  FROM agent_event"
            " WHERE run_id = ? AND provider IS NOT NULL ORDER BY id",
            (run_id,),
        )
    ]
    refletiu = {
        "quantas": len(reflexoes),
        "reflexoes": reflexoes,
        "gasto": livro.gasto_com_reflexao(conn, run_id),
        # Com zero reflexoes o agente E o B3 (D23). Todo resultado precisa
        # informar isso: um run sem reflexao nao mede cerebro nenhum.
        "houve_cerebro": len(reflexoes) > 0,
    }

    # ---------------------------------------------------------------- propos
    ativa = propostas.regra_ativa(conn, run_id)
    todas = propostas.do_run(conn, run_id)
    regra_json = None
    if ativa and ativa.get("rule_id"):
        linha = conn.execute(
            "SELECT hash, kind, family, params_json, condicoes_validade_json,"
            "       created_at, frozen_at FROM rule WHERE id = ?",
            (ativa["rule_id"],),
        ).fetchone()
        if linha:
            regra_json = dict(linha)
            regra_json["params"] = json.loads(regra_json.pop("params_json"))
            regra_json["condicoes_validade"] = json.loads(
                regra_json.pop("condicoes_validade_json")
            )

    propos = {
        "regra_ativa": ativa,
        "regra": regra_json,
        "propostas": todas,
        "aceitas": sum(1 for p in todas if p.get("status") == "aceita"),
        "rejeitadas": sum(1 for p in todas if p.get("status") == "rejeitada"),
    }

    # -------------------------------------------------------------- executou
    executou = dict(
        conn.execute(
            "SELECT COUNT(*) AS ordens_executadas,"
            "       SUM(CASE WHEN side = 'compra' THEN 1 ELSE 0 END) AS compras,"
            "       SUM(CASE WHEN side = 'venda'  THEN 1 ELSE 0 END) AS vendas,"
            "       COALESCE(SUM(fee_cents), 0) AS taxa_cents,"
            "       COALESCE(SUM(spread_cents), 0) AS spread_cents,"
            "       COALESCE(SUM(slippage_cents), 0) AS slippage_cents,"
            "       COALESCE(SUM(penalty_cents), 0) AS penalidade_cents,"
            "       COALESCE(SUM(notional_ref_cents), 0) AS nocional_girado_cents,"
            "       MIN(decision_bar_ms) AS primeira_decisao_ms,"
            "       MAX(execution_bar_ms) AS ultima_execucao_ms"
            "  FROM execution WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    )
    executou["digest"] = executor.digest_do_run(conn, run_id)
    executou["condicoes_validade"] = condicoes_do_run(conn, run_id)
    # A UNIDADE em que a comparacao com os baselines e feita.
    #
    # `ordens_executadas` conta linhas de `execution`; uma ida e volta sao
    # duas. B1 reporta `operacoes_alvo`, que sao idas e voltas. Sem este campo
    # a tabela de comparacao poria 36 ordens ao lado de 18 idas e voltas sob o
    # mesmo rotulo, e o leitor concluiria que o controle girou metade -
    # exatamente o contrario do que a D19 existe para garantir.
    #
    # O nome era `execucoes`, e trocou porque `execucoes`/`operacoes` era o
    # par ambiguo: os dois liam como "quantas vezes operou". Hoje o
    # vocabulario e um so em todo resultado reportado - `idas_e_voltas` e
    # `ordens_executadas` -, e ha teste proibindo a volta de `operacoes`.
    #
    # Compras, e nao metade das execucoes: a D1 fixou long/flat, entao ha no
    # maximo uma posicao aberta e cada compra abre exatamente uma ida e volta.
    # Dividir por dois suporia que toda compra fechou, e a ultima pode nao ter.
    executou["idas_e_voltas"] = executor.idas_e_voltas(conn, run_id)

    # ---------------------------------------------------------------- custos
    carteira = livro.carteira(conn, run_id=run_id)
    custos = {
        "carteira_do_run": carteira,
        "patrimonio_final_cents": carteira["simulado_usd"]["caixa_minor"],
        "livro_simulado_usd": carteira["simulado_usd"],
        "livro_real_brl": carteira["real_brl"],
        # A ponte entre os dois livros: a taxa gravada em cada transacao,
        # nunca uma conversao feita na hora de ler (secao 4.2).
        "cambio_do_run": [
            dict(l)
            for l in conn.execute(
                "SELECT DISTINCT fx_rate_micro, fx_rate_date FROM ledger_transaction"
                " WHERE run_id = ? AND fx_rate_micro IS NOT NULL",
                (run_id,),
            )
        ],
    }

    # ------------------------------------------------------------- comparado
    comparado = baselines.resumo_comparacao(conn)
    # O controle LIGADO a este run (migracao 14). Era o ultimo B1 casado
    # gravado globalmente, e o relatorio de um run antigo tomava emprestado o
    # controle de um run novo sem nada dizer.
    b1_do_agente = baselines.b1_do_run(conn, run_id)
    patrimonio = custos["patrimonio_final_cents"]

    if b1_do_agente:
        comparado = {
            **comparado,
            "b1_casado_com_o_agente": b1_do_agente,
            # Regra 14: desempenho SEMPRE como excesso sobre baseline.
            "excesso_sobre_b1_p50_cents": patrimonio - b1_do_agente["p50"],
            "faixa": avaliacao.faixa_contra_o_acaso(patrimonio, b1_do_agente),
        }

    # --------------------------------------------------------------- avaliou
    avaliou = avaliacao.do_run(conn, run_id)

    # --------------------------------------------------------------- caminho
    caminho = ciclo.caminho_percorrido(conn, run_id)
    vinculo_conferido = vinculo.conferir_ida_e_volta(conn, run_id)

    # ------------------------------------------------------------ integridade
    partidas = livro.conferir_partidas_dobradas(conn)
    saldos = livro.reconciliar(conn)
    vinculo_inferencia = livro.conferir_vinculo_inferencia(conn)
    arredondamento = livro.conferir_arredondamento_do_custo(conn)
    hash_recalculado = config_service.conferir_hash(versao) if versao else None

    integridade = {
        "partidas_dobradas_violadas": partidas,
        "saldos_divergentes": saldos,
        "vinculo_inferencia": vinculo_inferencia,
        "arredondamento_do_custo_divergente": arredondamento,
        "config_hash_ainda_descreve": hash_recalculado is None,
        "config_hash_recalculado": hash_recalculado,
        "ok": (
            not partidas
            and not saldos
            and not any(vinculo_inferencia.values())
            and not arredondamento
            and hash_recalculado is None
        ),
    }

    prova = reprodutibilidade.ultima_prova(conn)

    # ------------------------------------------------- a resposta, DERIVADA
    condicoes = {
        "dataset_fixado_com_hash": bool(dataset and dataset.get("sha256")),
        "o_cerebro_falou": refletiu["houve_cerebro"],
        "custo_por_decisao_registrado": (
            refletiu["gasto"]["chamadas_com_custo"] > 0 if refletiu["houve_cerebro"]
            else None
        ),
        "regra_proposta_com_hash": bool(regra_json and regra_json.get("hash")),
        "regra_executada": (executou["ordens_executadas"] or 0) > 0,
        "custos_nos_dois_livros": bool(custos["cambio_do_run"]) if refletiu[
            "houve_cerebro"
        ] else None,
        "comparado_aos_tres_baselines": bool(
            comparado.get("existe") and b1_do_agente
        ),
        "integridade_contabil": integridade["ok"],
        "reprodutibilidade_provada": bool(prova and prova["provado"]),
        "caminho_reconstruido": len(caminho) > 0,
        "vinculo_fecha_nos_dois_sentidos": bool(vinculo_conferido.get("conferido")),
        "avaliacao_como_evento_filho": avaliou is not None,
    }
    # `None` significa "nao se aplica a este run" - com zero reflexoes nao ha
    # custo de decisao para registrar, e exigi-lo reprovaria o run pela
    # ausencia de algo que a D23 permite. Tratar `None` como falso seria a
    # confusao entre "nao sei" e "nao" que a secao 5.2 proibe no custo.
    faltando = [nome for nome, ok in condicoes.items() if ok is False]

    return {
        "existe": True,
        "gerado_em": _agora(),
        # LEGITIMO, e o unico lugar em que uma fase e escrita a mao: este
        # relatorio E o fechamento da 0A (incremento 7), e responde a pergunta
        # DELA. Nao e a fase corrente - por isso o nome do campo diz de qual
        # relatorio se trata, e nao "fase".
        #
        # A fase corrente vem de `app.fase`, e o campo ao lado deixa a
        # diferenca visivel: um relatorio da 0A lido durante a 0B continua
        # sendo da 0A.
        "fase_do_relatorio": "0A",
        "fase_corrente": fase_mod.FASE,
        "run": run,
        "config": {
            "version_id": versao.id if versao else None,
            "config_hash": versao.config_hash if versao else None,
            "material": versao.material if versao else None,
            "author": versao.author if versao else None,
            "created_at": versao.created_at if versao else None,
            "profile_id": "neutro@1",
            "valores": config.model_dump(mode="json") if config else None,
        },
        "observou": observou,
        "refletiu": refletiu,
        "propos": propos,
        "executou": executou,
        "custos": custos,
        "comparado": comparado,
        "avaliou": avaliou,
        "caminho": caminho,
        "vinculo": vinculo_conferido,
        "integridade": integridade,
        "reprodutibilidade": prova,
        "nao_concluido": NAO_CONCLUIDO,
        "resposta_da_0a": {
            "pergunta": "o ciclo basico fecha?",
            "condicoes": condicoes,
            "faltando": faltando,
            "fecha": not faltando,
            # Secao 14.5, textual. Nao e consolo: e o significado exato de um
            # "nao" aqui, e precisa estar escrito antes de o resultado sair.
            "se_nao_fecha": (
                "O ciclo basico nao fecha. Problema de engenharia, nao de tese."
            ),
        },
    }
