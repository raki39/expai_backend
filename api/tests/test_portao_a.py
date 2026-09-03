"""Incremento 13 — Portão A: o produto da fase (§14.4).

> "Reprovar no Portão A **não é resultado ruim; é o resultado mais informativo
> possível a esse custo**, porque significa que o mecanismo central do projeto
> ainda não existe." — §14.4

Este arquivo começa pelo bloqueio que o incremento 12 declarou e que o 13
obriga a encarar: **o controle do acaso não estava ligado ao run que ele
casa**. Sem essa ligação o critério 3 do Portão B — "acima do p95 de B1" — não
é computável, e a métrica que isola escolha de momento era inavaliável pelo
validador.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.config.schema import ExperimentConfig


# ===========================================================================
# A ligação do controle com o run que ele casa (migração 14)
# ===========================================================================


def _abrir(conn: sqlite3.Connection, **kwargs) -> int:
    from app.ledger import livro

    return livro.abrir_run(
        conn,
        config_version_id=1,
        seed_capital_usd_cents=ExperimentConfig().seed_capital_usd_cents,
        **kwargs,
    )[0]


def test_um_run_so_pode_ter_um_controle_ligado(conn: sqlite3.Connection) -> None:
    """Dois controles reivindicando o mesmo alvo não é ambiguidade: é palpite.

    Sem o índice único, `b1_do_run` faria `SELECT id FROM run WHERE
    casa_run_id = ?` e escolheria uma das linhas por ordem de id — uma escolha
    arbitrária com cara de consulta, que é exatamente a forma do defeito que a
    ligação existe para fechar.
    """
    alvo = _abrir(conn)
    _abrir(conn, agent_id="baseline-B1-agente", casa_run_id=alvo)
    with pytest.raises(sqlite3.IntegrityError):
        _abrir(conn, agent_id="baseline-B1-agente", casa_run_id=alvo)


def test_a_ligacao_e_imutavel(conn: sqlite3.Connection) -> None:
    """Trocar o alvo depois de medir é a régua trocada depois do resultado.

    E a coluna está exposta a um UPDATE que já existe e roda em todo run:
    `encerrar_run` faz `UPDATE run SET state = ...`. Sem o gatilho, bastaria
    acrescentar uma coluna àquele UPDATE.
    """
    alvo, outro = _abrir(conn), _abrir(conn)
    controle = _abrir(conn, agent_id="baseline-B1-agente", casa_run_id=alvo)
    with pytest.raises(sqlite3.IntegrityError, match="imutavel"):
        conn.execute(
            "UPDATE run SET casa_run_id = ? WHERE id = ?", (outro, controle)
        )
    # E o UPDATE que o sistema faz de verdade continua passando.
    from app.ledger import livro

    livro.encerrar_run(conn, controle, "concluido")


def test_controle_de_controle_e_recusado(conn: sqlite3.Connection) -> None:
    """Comparar um sorteio com outro sorteio não mede escolha de momento.

    Alcançável de fato: basta passar o run de um B1 como alvo do casamento.
    """
    alvo = _abrir(conn)
    controle = _abrir(conn, agent_id="baseline-B1-agente", casa_run_id=alvo)
    with pytest.raises(sqlite3.IntegrityError, match="controle de controle"):
        _abrir(conn, agent_id="baseline-B1-agente", casa_run_id=controle)


def test_run_sem_ligacao_devolve_none_e_nao_o_controle_de_outro(
    conn: sqlite3.Connection,
) -> None:
    """`None` é a resposta de todo run anterior à migração 14.

    A alternativa que existia — "o último B1 casado gravado, globalmente" —
    devolvia um controle plausível de OUTRO experimento. Em produção isso saiu
    na tela como 37 idas e voltas ao lado de um controle de 70.
    """
    from app.maos_rapidas import baselines

    sem_ligacao = _abrir(conn)
    alvo = _abrir(conn)
    _abrir(conn, agent_id="baseline-B1-agente", casa_run_id=alvo)

    assert baselines.b1_do_run(conn, sem_ligacao) is None


def test_a_guarda_nao_e_vazia(conn: sqlite3.Connection) -> None:
    """A coluna existe, e o teste acima poderia passar por ela não existir.

    Um `SELECT ... WHERE casa_run_id = ?` sobre uma coluna ausente levantaria
    `OperationalError`, e não `None` — mas um teste que só afirma `None`
    passaria igual se a função inteira fosse `return None`.
    """
    colunas = {l["name"] for l in conn.execute("PRAGMA table_info(run)")}
    assert "casa_run_id" in colunas
