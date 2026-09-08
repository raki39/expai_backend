"""Conferencia de integridade, tudo derivado de consulta.

Existe porque "confira e registre" precisa produzir **numeros lidos do banco**,
e nao a minha palavra de que esta tudo bem. Cada campo aqui e o resultado de
uma consulta; nenhum e digitado.

As cinco perguntas que o usuario pediu, e onde cada uma e respondida:

    1. as migracoes aplicaram, com as tabelas e os gatilhos declarados
    2. os hashes e as identidades executaveis
    3. a integridade contabil
    4. o estado do rele e do coletor
    5. NENHUM run foi alterado retroativamente

**A quinta e a que mais importa depois de uma reancoragem**, porque a
reancoragem cria `config_version` nova - e a pergunta certa e se algum run
ANTIGO passou a apontar para ela. Ele nao pode: `run.config_version_id` nasce
com o run, e `config_version` e imutavel. Mas "nao pode" e uma afirmacao sobre
o desenho, e esta rota mede.
"""

from __future__ import annotations

import sqlite3
from typing import Any

# As tabelas e os gatilhos que os incrementos 18 e 19 acrescentaram. Lista
# EXPLICITA: uma tabela que deixasse de existir num deploy passaria calada se
# a conferencia apenas contasse quantas existem.
ESTRUTURA_ESPERADA = {
    19: ["bbo_contrato", "bbo_amostra", "janela_piloto"],
    20: [],  # ALTER TABLE, conferido por coluna abaixo
    21: ["calibracao_observacao"],
    22: ["shadow_ordem"],
    23: ["janela_revalidacao", "calibracao_versao", "revalidacao_uso",
         "calibracao_regime"],
    24: ["calibracao_perfil", "calibracao_perfil_regime"],
    25: [],  # ALTER TABLE
    26: ["abordagem", "quarentena_limiar"],
    27: ["quarentena_congelado"],
    28: ["monitor_limiar", "monitor_passo", "monitor_alarme",
         "monitor_reteste"],
    29: [],  # ALTER TABLE, conferido por coluna abaixo
}

COLUNAS_ESPERADAS = {
    ("bbo_amostra", "relogio_medido_em_ms"): 20,
    ("config_version", "calibracao_perfil_hash"): 24,
    ("run", "calibracao_perfil_hash"): 25,
    ("hypothesis", "regua_dimensionamento"): 29,
    ("hypothesis", "dimensionamento_json"): 29,
}

# Os gatilhos que impedem reescrita. Se um deles sumir, a tabela continua
# existindo e a garantia nao - e essa e a forma de falha que mais importa aqui.
GATILHOS_ESPERADOS = [
    "bbo_contrato_sem_update", "bbo_contrato_sem_delete",
    "bbo_amostra_sem_update", "bbo_amostra_sem_delete",
    "janela_piloto_sem_update", "janela_piloto_sem_delete",
    "calibracao_observacao_sem_update", "calibracao_observacao_sem_delete",
    "shadow_ordem_sem_update", "shadow_ordem_sem_delete",
    "janela_revalidacao_sem_update", "janela_revalidacao_sem_delete",
    "calibracao_versao_sem_update", "calibracao_versao_sem_delete",
    "revalidacao_uso_sem_update", "revalidacao_uso_sem_delete",
    "calibracao_regime_sem_update", "calibracao_regime_sem_delete",
    "calibracao_perfil_sem_update", "calibracao_perfil_sem_delete",
    "calibracao_perfil_regime_sem_update", "calibracao_perfil_regime_sem_delete",
    "abordagem_nao_reabre", "abordagem_sem_delete",
    "quarentena_limiar_sem_update", "quarentena_limiar_sem_delete",
    "quarentena_congelado_sem_update", "quarentena_congelado_sem_delete",
    "monitor_limiar_sem_update", "monitor_limiar_sem_delete",
    "monitor_passo_sem_update", "monitor_passo_sem_delete",
    "monitor_alarme_sem_update", "monitor_alarme_sem_delete",
    "monitor_critico_exige_alerta",
    "monitor_reteste_e_posterior", "monitor_reteste_sem_update",
    "hypothesis_dimensionamento_coerente",
]

# A migracao a partir da qual estas listas valem. Abaixo dela esta o substrato
# das fases 0A e 0B, ja coberto pelas suites daquelas fases.
#
# O numero existe para que `tests/test_relatorio.py` possa DERIVAR o que
# deveria estar aqui - aplicando as migracoes ate esta fronteira e depois todas,
# e comparando a diferenca com o que estas listas declaram.
#
# **Isto nasceu de uma falha real.** O incremento 20 acrescentou a migracao 28
# com quatro tabelas e nove gatilhos, e estas listas ficaram na 27. O relatorio
# de producao respondeu `integras: true` sobre uma estrutura que tinha deixado
# de descrever o sistema - no modulo cuja unica funcao e conferir integridade.
# Vigesima segunda ocorrencia do padrao.
PRIMEIRA_MIGRACAO_CONFERIDA = 19


def _nomes(conn: sqlite3.Connection, tipo: str) -> set[str]:
    return {
        str(r["name"])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = ?", (tipo,)
        )
    }


def montar(conn: sqlite3.Connection) -> dict[str, Any]:
    from ..calibracao import identidade
    from ..config import service as config_service
    from ..store import versao_schema

    schema = versao_schema(conn)
    tabelas = _nomes(conn, "table")
    gatilhos = _nomes(conn, "trigger")

    # -------------------------------------------- 1. estrutura das migracoes
    esperadas = [t for lista in ESTRUTURA_ESPERADA.values() for t in lista]
    faltando_tabelas = sorted(t for t in esperadas if t not in tabelas)
    faltando_gatilhos = sorted(g for g in GATILHOS_ESPERADOS if g not in gatilhos)

    faltando_colunas = []
    for (tabela, coluna), _mig in COLUNAS_ESPERADAS.items():
        if tabela not in tabelas:
            faltando_colunas.append(f"{tabela}.{coluna} (tabela ausente)")
            continue
        cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({tabela})")}
        if coluna not in cols:
            faltando_colunas.append(f"{tabela}.{coluna}")

    # ---------------------------------- 2. hashes e identidades executaveis
    versoes = [
        {
            "id": int(r["id"]),
            "config_hash": str(r["config_hash"])[:16],
            "hash_integro": config_service.conferir_hash(
                config_service._linha_para_versao(r)
            ) is None,
            "calibracao_perfil_hash": (
                None if r["calibracao_perfil_hash"] is None
                else str(r["calibracao_perfil_hash"])[:16]
            ),
            "material": bool(r["material"]),
            "author": str(r["author"]),
        }
        for r in conn.execute(
            "SELECT * FROM config_version ORDER BY id"
        )
    ]

    runs = [
        {
            "run_id": int(r["id"]),
            "agent_id": str(r["agent_id"]),
            "config_version_id": int(r["config_version_id"]),
            "identidade_executavel": identidade.do_run(conn, int(r["id"])),
            "perfil": (
                None if r["calibracao_perfil_hash"] is None
                else str(r["calibracao_perfil_hash"])[:16]
            ),
        }
        for r in conn.execute(
            "SELECT * FROM run ORDER BY id DESC LIMIT 20"
        )
    ]

    # ----------------------------------------------- 3. integridade contabil
    #
    # Equilibrio POR LIVRO, nunca no total: somar BRL com USD nao significa
    # nada (regra 7). E so transacao FECHADA entra - a aberta esta, por
    # definicao, no meio de receber lancamentos.
    livros = [
        {
            "book": str(r["book"]),
            "soma_minor": int(r["soma"] or 0),
            "equilibrado": int(r["soma"] or 0) == 0,
        }
        for r in conn.execute(
            "SELECT a.book AS book, SUM(e.amount_minor) AS soma"
            "  FROM ledger_entry e"
            "  JOIN ledger_transaction t ON t.id = e.transaction_id"
            "  JOIN account a ON a.id = e.account_id"
            " WHERE t.posted_at IS NOT NULL"
            " GROUP BY a.book"
        )
    ]
    abertas = int(conn.execute(
        "SELECT COUNT(*) AS n FROM ledger_transaction WHERE posted_at IS NULL"
    ).fetchone()["n"])

    # ------------------------------------------- 4. o rele e o coletor
    fluxo = conn.execute(
        "SELECT COUNT(*) AS barras, MIN(open_time_ms) AS de,"
        "       MAX(open_time_ms) AS ate FROM stream_bar"
    ).fetchone()
    bbo = conn.execute(
        "SELECT COUNT(*) AS total, SUM(disponivel) AS validas,"
        "       MIN(t_grid_ms) AS de, MAX(t_grid_ms) AS ate FROM bbo_amostra"
    ).fetchone()

    # ------------------------- 5. NENHUM run alterado retroativamente
    #
    # A reancoragem cria `config_version` NOVA. A pergunta certa nao e se ela
    # existe - e se algum run ANTIGO passou a apontar para ela.
    ultima = conn.execute(
        "SELECT MAX(id) AS id FROM config_version"
    ).fetchone()["id"]
    runs_na_ultima = int(conn.execute(
        "SELECT COUNT(*) AS n FROM run WHERE config_version_id = ?",
        (ultima,),
    ).fetchone()["n"])

    # E o teste mais forte: um run cujo `id` e ANTERIOR ao da config que ele
    # cita seria um run que trocou de config depois de nascer.
    anacronicos = [
        {"run_id": int(r["id"]), "config_version_id": int(r["cv"])}
        for r in conn.execute(
            "SELECT r.id AS id, r.config_version_id AS cv"
            "  FROM run r JOIN config_version c ON c.id = r.config_version_id"
            " WHERE c.created_at > r.created_at"
        )
    ]

    return {
        "schema_version": schema,
        "migracoes": {
            "tabelas_esperadas": len(esperadas),
            "tabelas_faltando": faltando_tabelas,
            "colunas_faltando": faltando_colunas,
            "gatilhos_esperados": len(GATILHOS_ESPERADOS),
            "gatilhos_faltando": faltando_gatilhos,
            "integras": not (
                faltando_tabelas or faltando_colunas or faltando_gatilhos
            ),
        },
        "identidades": {
            "config_versions": versoes,
            "todas_com_hash_integro": all(v["hash_integro"] for v in versoes),
            "runs_recentes": runs,
        },
        "contabilidade": {
            "por_livro": livros,
            "todos_equilibrados": all(l["equilibrado"] for l in livros),
            "transacoes_abertas": abertas,
        },
        "dado_ao_vivo": {
            "stream_bar": {
                "barras": int(fluxo["barras"] or 0),
                "de_ms": fluxo["de"], "ate_ms": fluxo["ate"],
            },
            "bbo_amostra": {
                "total": int(bbo["total"] or 0),
                "validas": int(bbo["validas"] or 0),
                "de_ms": bbo["de"], "ate_ms": bbo["ate"],
            },
        },
        "sem_alteracao_retroativa": {
            "config_version_mais_recente": ultima,
            "runs_apontando_para_ela": runs_na_ultima,
            "runs_anacronicos": anacronicos,
            "nenhum_run_trocou_de_config": not anacronicos,
        },
    }
