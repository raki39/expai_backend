"""Vinculo completo, navegavel nos DOIS sentidos (R25.2).

    decisao -> custo de inferencia -> regra -> execucoes -> resultado

A exigencia da R25.2 nao e que os campos existam: e que exista **consulta que
parte de uma execucao e chega ao evento cognitivo que a autorizou, e
vice-versa**. Campo de chave estrangeira que ninguem percorre e promessa, nao
vinculo - e este projeto ja gastou cinco correcoes descobrindo a diferenca
entre as duas coisas.

## Onde as duas pontas se ligam

`execution.rule_id` diz qual regra autorizou a ordem (R25.5). `rule_proposal`
liga uma regra ao evento que a propos. Juntas, fecham o caminho:

    execution.rule_id -> rule_proposal.rule_id -> rule_proposal.agent_event_id
                      -> agent_event (e dali sobe por parent_event_id)

O elo do meio e o que pode faltar legitimamente: quando o cerebro nao produziu
regra valida - ou o teto o calou - as maos rapidas rodam com a regra padrao
(D23), e **nao existe evento cognitivo que a autorizou**. Isso e resposta, nao
lacuna, e a funcao devolve o motivo em vez de uma lista vazia que o leitor
interpretaria como defeito.
"""

from __future__ import annotations

import json
import sqlite3


def _um(conn: sqlite3.Connection, sql: str, args: tuple) -> dict | None:
    linha = conn.execute(sql, args).fetchone()
    return dict(linha) if linha else None


def ancestrais(conn: sqlite3.Connection, event_id: int) -> list[dict]:
    """A cadeia do evento ate a raiz, do mais recente ao primeiro.

    Sobe por `parent_event_id`. O limite de profundidade nao e paranoia
    barata: `parent_event_id` e uma auto-referencia sem restricao de aciclia
    no SQLite, e um laco aqui travaria a rota do relatorio em vez de acusar.
    """
    cadeia: list[dict] = []
    atual: int | None = event_id
    vistos: set[int] = set()
    while atual is not None and atual not in vistos and len(cadeia) < 64:
        vistos.add(atual)
        no = _um(
            conn,
            "SELECT id, run_id, parent_event_id, occurred_at, node, kind,"
            "       provider, model, tier, cost_usd_minor, cost_usd_micro,"
            "       expectation, confidence_ppm, outputs_digest, profile_id"
            "  FROM agent_event WHERE id = ?",
            (atual,),
        )
        if no is None:
            break
        cadeia.append(no)
        atual = no["parent_event_id"]
    return cadeia


def da_execucao_ao_evento(conn: sqlite3.Connection, execution_id: int) -> dict:
    """Sentido REVERSO: de uma execucao qualquer ao evento que a autorizou.

    E a consulta que a R25.2 nomeia explicitamente como prova.
    """
    execucao = _um(
        conn,
        "SELECT id, run_id, dataset_id, rule_id, decision_bar_ms,"
        "       execution_bar_ms, side, quantity_sats, price_ref, price_exec,"
        "       notional_ref_cents, fee_cents, spread_cents, slippage_cents,"
        "       penalty_cents, fidelity_level, ledger_transaction_id"
        "  FROM execution WHERE id = ?",
        (execution_id,),
    )
    if execucao is None:
        return {"existe": False, "motivo": f"execucao {execution_id} nao existe"}

    regra = _um(
        conn,
        "SELECT id, kind, family, params_json, hash AS regra_hash,"
        "       condicoes_validade_json, frozen_at FROM rule WHERE id = ?",
        (execucao["rule_id"],),
    )

    proposta = _um(
        conn,
        "SELECT id, run_id, agent_event_id, proposed_at, status, expectation,"
        "       confidence_ppm, observed_from_ms, observed_to_ms"
        "  FROM rule_proposal"
        " WHERE rule_id = ? AND status = 'aceita'"
        " ORDER BY id DESC LIMIT 1",
        (execucao["rule_id"],),
    )

    if proposta is None:
        # Nao e lacuna: e a D23 acontecendo. Dizer isso e diferente de
        # devolver lista vazia, que o leitor leria como vinculo quebrado.
        return {
            "existe": True,
            "execucao": execucao,
            "regra": regra,
            "cadeia_cognitiva": [],
            "autorizada_por": None,
            "motivo": (
                "regra padrao (D23): o cerebro nao declarou intencao neste run,"
                " entao nao existe evento cognitivo que a tenha autorizado"
            ),
        }

    cadeia = ancestrais(conn, int(proposta["agent_event_id"]))

    # A decisao propriamente dita e a intencao, que e FILHA da proposta - e
    # portanto nao aparece subindo a cadeia. Buscada a parte, de proposito:
    # e ela que carrega expectativa e confianca declaradas antes da execucao.
    intencao = _um(
        conn,
        "SELECT id, occurred_at, expectation, confidence_ppm, outputs_digest"
        "  FROM agent_event"
        " WHERE run_id = ? AND kind = 'intencao' ORDER BY id DESC LIMIT 1",
        (int(proposta["run_id"]),),
    )

    return {
        "existe": True,
        "execucao": execucao,
        "regra": regra,
        "proposta": proposta,
        "autorizada_por": int(proposta["agent_event_id"]),
        "intencao": intencao,
        "cadeia_cognitiva": cadeia,
        "motivo": None,
    }


def do_evento_ao_resultado(conn: sqlite3.Connection, event_id: int) -> dict:
    """Sentido DIRETO: da decisao ao custo, a regra, as execucoes e ao resultado."""
    evento = _um(
        conn,
        "SELECT id, run_id, parent_event_id, occurred_at, node, kind, provider,"
        "       model, tier, tokens_in, tokens_out, tokens_cache_read,"
        "       tokens_cache_write, cost_usd_minor, cost_usd_micro,"
        "       price_table_version, expectation, confidence_ppm,"
        "       outputs_digest, ledger_transaction_id, evaluation_json"
        "  FROM agent_event WHERE id = ?",
        (event_id,),
    )
    if evento is None:
        return {"existe": False, "motivo": f"evento {event_id} nao existe"}

    # O custo: o lancamento no ledger, e nao o campo do evento. Os dois
    # existem e devem concordar; ler o do ledger e ler a autoridade (regra 16).
    custo = _um(
        conn,
        "SELECT t.id AS transaction_id, t.kind, t.fx_rate_micro, t.fx_rate_date,"
        "       SUM(CASE WHEN a.code = 'sim.tesouraria' THEN e.amount_minor END)"
        "           AS custo_simulado_minor,"
        "       SUM(CASE WHEN a.code = 'real.despesa.inferencia' THEN e.amount_minor END)"
        "           AS custo_real_brl_minor"
        "  FROM ledger_transaction t"
        "  JOIN ledger_entry e ON e.transaction_id = t.id"
        "  JOIN account a ON a.id = e.account_id"
        " WHERE t.id = ? GROUP BY t.id",
        (evento["ledger_transaction_id"],),
    ) if evento["ledger_transaction_id"] else None

    proposta = _um(
        conn,
        "SELECT id, status, rule_id, rejection_reason, expectation,"
        "       confidence_ppm, observed_from_ms, observed_to_ms"
        "  FROM rule_proposal WHERE agent_event_id = ?",
        (event_id,),
    )

    regra = None
    execucoes: dict | None = None
    if proposta and proposta["rule_id"]:
        regra = _um(
            conn,
            "SELECT id, kind, family, params_json, hash AS regra_hash,"
            "       condicoes_validade_json, frozen_at FROM rule WHERE id = ?",
            (proposta["rule_id"],),
        )
        execucoes = _um(
            conn,
            "SELECT COUNT(*) AS quantas, MIN(id) AS primeira, MAX(id) AS ultima,"
            "       SUM(fee_cents + spread_cents + slippage_cents + penalty_cents)"
            "           AS custo_total_cents"
            "  FROM execution WHERE rule_id = ? AND run_id = ?",
            (proposta["rule_id"], evento["run_id"]),
        )

    avaliacao = _um(
        conn,
        "SELECT id, occurred_at, evaluation_json FROM agent_event"
        " WHERE parent_event_id = ? AND kind = 'avaliacao'"
        " ORDER BY id DESC LIMIT 1",
        (event_id,),
    )
    if avaliacao and avaliacao["evaluation_json"]:
        avaliacao["comparacao"] = json.loads(avaliacao.pop("evaluation_json"))

    return {
        "existe": True,
        "evento": evento,
        "custo": custo,
        "proposta": proposta,
        "regra": regra,
        "execucoes": execucoes,
        "avaliacao": avaliacao,
    }


def conferir_ida_e_volta(conn: sqlite3.Connection, run_id: int) -> dict:
    """Percorre os dois sentidos no mesmo run e confere que fecham.

    Pega uma execucao qualquer, sobe ate o evento cognitivo, desce de volta e
    exige chegar na mesma execucao. Um vinculo que so funciona num sentido
    passaria nas duas consultas isoladas e falharia aqui - que e o unico lugar
    onde a palavra "navegavel nos dois sentidos" quer dizer alguma coisa.
    """
    alguma = conn.execute(
        "SELECT id FROM execution WHERE run_id = ? ORDER BY id LIMIT 1", (run_id,)
    ).fetchone()
    if alguma is None:
        return {"conferido": False, "motivo": "o run nao tem execucao nenhuma"}

    execution_id = int(alguma["id"])
    volta = da_execucao_ao_evento(conn, execution_id)
    if volta.get("autorizada_por") is None:
        return {
            "conferido": False,
            "execution_id": execution_id,
            "motivo": volta.get("motivo"),
        }

    ida = do_evento_ao_resultado(conn, int(volta["autorizada_por"]))
    quantas = (ida.get("execucoes") or {}).get("quantas") or 0
    primeira = (ida.get("execucoes") or {}).get("primeira")

    return {
        "conferido": bool(quantas) and primeira == execution_id,
        "execution_id": execution_id,
        "evento_cognitivo": int(volta["autorizada_por"]),
        "no": volta["cadeia_cognitiva"][0]["node"] if volta["cadeia_cognitiva"] else None,
        "profundidade_da_cadeia": len(volta["cadeia_cognitiva"]),
        "execucoes_autorizadas": quantas,
        "primeira_execucao_da_regra": primeira,
        "regra_hash": (volta.get("regra") or {}).get("regra_hash"),
    }
