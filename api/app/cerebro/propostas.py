"""Propostas de regra: as aceitas e, principalmente, as recusadas.

Criterio 2: "resposta fora do schema e rejeitada, registrada como rejeicao e
a regra ativa anterior permanece". As tres coisas sao verificaveis aqui.

A regra ativa **nao e um campo**. E derivada: a ultima proposta aceita do run.
Guardar "regra ativa" numa coluna criaria uma segunda fonte de verdade que
poderia divergir do historico de propostas - e uma proposta rejeitada que
esquecesse de nao atualizar essa coluna trocaria a regra em silencio, que e
exatamente o que o criterio 2 proibe. Derivando, a garantia e estrutural:
uma rejeicao nao tem `rule_id`, entao nao ha o que ela possa passar a
apontar.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

from ..regra.schema import Regra

log = logging.getLogger(__name__)


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def registrar_aceita(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    agent_event_id: int,
    rule_id: int,
    regra: Regra,
    resposta_crua: str,
    expectativa: str,
    confianca_ppm: int,
    observado_de_ms: int,
    observado_ate_ms: int,
) -> int:
    """Grava a proposta aceita, com a expectativa declarada ANTES da execucao.

    A ordem e o criterio 10 (R25.3): esta linha existe antes de qualquer
    chamada das maos rapidas, e `rule_proposal` recusa `UPDATE` por trigger.
    Reavaliar depois e proposta NOVA, nunca edicao desta.
    """
    cur = conn.execute(
        "INSERT INTO rule_proposal (run_id, agent_event_id, proposed_at, status,"
        " raw_response_json, rule_id, expectation, confidence_ppm,"
        " observed_from_ms, observed_to_ms)"
        " VALUES (?,?,?, 'aceita', ?,?,?,?,?,?)",
        (
            run_id, agent_event_id, _agora(), resposta_crua, rule_id,
            expectativa, confianca_ppm, observado_de_ms, observado_ate_ms,
        ),
    )
    proposta_id = int(cur.lastrowid)
    log.info(
        "cerebro.proposta_aceita",
        extra={
            "proposal_id": proposta_id,
            "rule_id": rule_id,
            "regra_hash": regra.hash(),
            "familia": regra.familia,
            "confianca_ppm": confianca_ppm,
        },
    )
    return proposta_id


def registrar_rejeitada(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    agent_event_id: int,
    resposta_crua: str,
    motivo: str,
    observado_de_ms: int | None = None,
    observado_ate_ms: int | None = None,
) -> int:
    """Grava a rejeicao COM a resposta que a causou.

    Guardar so o que deu certo transformaria o historico num relatorio de
    sucesso. Sem a resposta crua nao ha como diagnosticar um modelo que
    comecou a responder fora do schema.
    """
    cur = conn.execute(
        "INSERT INTO rule_proposal (run_id, agent_event_id, proposed_at, status,"
        " raw_response_json, rejection_reason, observed_from_ms, observed_to_ms)"
        " VALUES (?,?,?, 'rejeitada', ?,?,?,?)",
        (
            run_id, agent_event_id, _agora(), resposta_crua, motivo,
            observado_de_ms, observado_ate_ms,
        ),
    )
    proposta_id = int(cur.lastrowid)
    log.warning(
        "cerebro.proposta_rejeitada",
        extra={"proposal_id": proposta_id, "motivo": motivo[:200]},
    )
    return proposta_id


def regra_ativa(conn: sqlite3.Connection, run_id: int) -> dict | None:
    """A ultima proposta ACEITA do run. Derivada, nunca guardada.

    `None` significa que o cerebro ainda nao produziu regra valida neste run -
    e nesse caso quem executa e a regra padrao, que vem da configuracao.
    """
    linha = conn.execute(
        "SELECT p.id AS proposal_id, p.rule_id, p.expectation, p.confidence_ppm,"
        "       p.proposed_at, p.agent_event_id, p.observed_from_ms,"
        "       p.observed_to_ms, r.hash AS regra_hash, r.family, r.params_json"
        " FROM rule_proposal p JOIN rule r ON r.id = p.rule_id"
        " WHERE p.run_id = ? AND p.status = 'aceita'"
        " ORDER BY p.id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    return dict(linha) if linha else None


def do_run(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """Todas as propostas do run, na ordem, aceitas e rejeitadas."""
    return [
        dict(l)
        for l in conn.execute(
            "SELECT id, agent_event_id, proposed_at, status, rejection_reason,"
            " rule_id, expectation, confidence_ppm, observed_from_ms,"
            " observed_to_ms, raw_response_json"
            " FROM rule_proposal WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
    ]


def sobreposicao_amostral(conn: sqlite3.Connection, run_id: int) -> dict:
    """Quanto da janela executada o cerebro tinha visto ao propor a regra.

    E declarado como NUMERO, calculado do que ficou gravado, e nao como frase
    num texto de condicoes de validade. A frase envelhece quando o desenho
    muda; a conta nao. Na Fase 0A a resposta esperada e 100%: o cerebro
    observa a mesma janela em que a regra sera executada, o que torna o
    resultado do agente uma medida EM AMOSTRA - suficiente para responder "o
    ciclo fecha?", e insuficiente para qualquer afirmacao de desempenho.
    """
    proposta = conn.execute(
        "SELECT observed_from_ms, observed_to_ms FROM rule_proposal"
        " WHERE run_id = ? AND status = 'aceita'"
        " ORDER BY id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    executado = conn.execute(
        "SELECT MIN(decision_bar_ms) AS de, MAX(decision_bar_ms) AS ate,"
        "       COUNT(*) AS execucoes"
        " FROM execution WHERE run_id = ?",
        (run_id,),
    ).fetchone()

    if proposta is None or executado is None or executado["de"] is None:
        return {
            "observado_de_ms": None,
            "observado_ate_ms": None,
            "executado_de_ms": None,
            "executado_ate_ms": None,
            "sobreposicao_bps": None,
            "em_amostra": None,
        }

    de = max(int(proposta["observed_from_ms"]), int(executado["de"]))
    ate = min(int(proposta["observed_to_ms"]), int(executado["ate"]))
    largura = int(executado["ate"]) - int(executado["de"])
    sobreposto = max(ate - de, 0)
    bps = 10_000 if largura == 0 else sobreposto * 10_000 // largura

    return {
        "observado_de_ms": int(proposta["observed_from_ms"]),
        "observado_ate_ms": int(proposta["observed_to_ms"]),
        "executado_de_ms": int(executado["de"]),
        "executado_ate_ms": int(executado["ate"]),
        "sobreposicao_bps": bps,
        "em_amostra": bps > 0,
    }
