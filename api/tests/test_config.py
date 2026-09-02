"""Criterios 8, 9 e 10 do incremento 0, e as travas do ADR 0008.

A secao 10.2.3 exige alteracao de configuracao "versionada no ledger, com
autor, data, valor anterior e novo". Estes testes verificam exatamente isso.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config.schema import ExperimentConfig
from app.config.service import (
    ConfigCongelada,
    SemMudanca,
    TetoExcedido,
    criar_versao,
    versao_atual,
)
from app.settings import get_settings

# --------------------------------------------- criterio 8: versao 1 e historico


def test_bootstrap_cria_versao_1(client: TestClient) -> None:
    r = client.get("/api/config")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["version_id"] == 1
    assert corpo["author"] == "bootstrap"
    assert corpo["parent_version_id"] is None
    assert len(corpo["config_hash"]) == 64  # sha256 hex
    assert corpo["config"]["market_symbol"] == "BTCUSDT"
    assert corpo["config"]["timeframe"] == "15m"
    assert corpo["config"]["fidelity_level"] == 1
    assert corpo["config"]["b3_fast"] == 20
    assert corpo["config"]["b3_slow"] == 50
    assert corpo["config"]["b1_repetitions"] == 1000


def test_post_cria_versao_2_com_autor_data_antes_e_depois(client: TestClient) -> None:
    r = client.post(
        "/api/config",
        json={
            "author": "tiago",
            "changes": {"spread_bps": "3"},
            "note": "spread mais pessimista",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["version_id"] == 2
    assert r.json()["material"] is True
    assert "invalida comparacao" in r.json()["aviso"]

    hist = client.get("/api/config/history").json()["versions"]
    assert [v["version_id"] for v in hist] == [2, 1]

    v2 = hist[0]
    assert v2["author"] == "tiago"
    assert v2["created_at"]
    assert v2["parent_version_id"] == 1
    assert v2["note"] == "spread mais pessimista"

    mudanca = next(c for c in v2["changes"] if c["field"] == "spread_bps")
    assert mudanca["old_value"] == "1"
    assert mudanca["new_value"] == "3"
    assert mudanca["material"] is True


def test_config_hash_muda_com_alteracao_material(client: TestClient) -> None:
    antes = client.get("/api/config").json()["config_hash"]
    client.post(
        "/api/config", json={"author": "t", "changes": {"taker_fee_bps": "12"}}
    )
    assert client.get("/api/config").json()["config_hash"] != antes


def test_semente_nao_muda_config_hash(client: TestClient) -> None:
    """Semente diferente, mesmo config_hash (secao 14.4.1).

    Trocar a semente e reexecucao legitima, nao outro experimento.
    """
    antes = client.get("/api/config").json()["config_hash"]
    r = client.post(
        "/api/config", json={"author": "t", "changes": {"default_seed": 4242}}
    )
    assert r.status_code == 201
    assert r.json()["material"] is False
    assert r.json()["aviso"] == ""
    assert client.get("/api/config").json()["config_hash"] == antes


def test_alteracao_vazia_recusada(client: TestClient) -> None:
    atual = client.get("/api/config").json()["config"]
    r = client.post(
        "/api/config",
        json={"author": "t", "changes": {"spread_bps": atual["spread_bps"]}},
    )
    assert r.status_code == 400


def test_valor_incoerente_recusado(client: TestClient) -> None:
    """b3_fast precisa ser menor que b3_slow."""
    r = client.post("/api/config", json={"author": "t", "changes": {"b3_fast": 99}})
    assert r.status_code == 422


def test_campo_desconhecido_recusado(client: TestClient) -> None:
    r = client.post(
        "/api/config", json={"author": "t", "changes": {"campo_inventado": 1}}
    )
    assert r.status_code == 422


# ------------------------------------------------ criterio 10: teto absoluto


def test_teto_do_banco_nao_excede_o_do_ambiente(client: TestClient) -> None:
    """Secao 12.1: o limite mora fora do codigo.

    LLM_MAX_USD_ABSOLUTE = 5.00 -> 500 centavos.
    """
    r = client.post(
        "/api/config",
        json={"author": "t", "changes": {"max_llm_usd_per_run_cents": 501}},
    )
    assert r.status_code == 422
    assert "LLM_MAX_USD_ABSOLUTE" in r.json()["detail"]

    r = client.post(
        "/api/config",
        json={"author": "t", "changes": {"max_llm_usd_per_run_cents": 500}},
    )
    assert r.status_code == 201


# ----------------------------------------------- trava 1: run ativo congela


def test_config_congela_durante_run_ativo(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    conn.execute(
        "INSERT INTO run (agent_id, state, config_version_id, created_at,"
        " updated_at) VALUES ('agent-0001','executando',1,datetime('now'),"
        " datetime('now'))"
    )
    assert client.get("/api/config").json()["congelada"] is True

    r = client.post("/api/config", json={"author": "t", "changes": {"spread_bps": "9"}})
    assert r.status_code == 409
    assert "reprodutibilidade" in r.json()["detail"]


def test_run_concluido_nao_congela(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    conn.execute(
        "INSERT INTO run (agent_id, state, config_version_id, created_at,"
        " updated_at) VALUES ('agent-0001','concluido',1,datetime('now'),"
        " datetime('now'))"
    )
    r = client.post("/api/config", json={"author": "t", "changes": {"spread_bps": "9"}})
    assert r.status_code == 201


# ------------------------------------------------- imutabilidade no banco


def test_config_version_nao_aceita_update(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="imutavel"):
        conn.execute("UPDATE config_version SET author = 'outro' WHERE id = 1")


def test_config_version_nao_aceita_delete(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM config_version WHERE id = 1")


def test_config_change_nao_aceita_update(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="imutavel"):
        conn.execute("UPDATE config_change SET field = 'x' WHERE id = 1")


# ------------------------------------- criterio 9: precedencia env vs banco


def test_env_nao_sobrepoe_parametro_do_experimento(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Depois da versao 1, o ambiente e IGNORADO nos parametros do experimento.

    Configuracao com duas fontes e classe conhecida de bug. Aqui a regra de
    precedencia e testada, nao presumida.
    """
    assert client.get("/api/config").json()["config"]["timeframe"] == "15m"

    # Mesmo que alguem defina isso no ambiente, nao tem efeito.
    monkeypatch.setenv("TIMEFRAME", "1m")
    monkeypatch.setenv("MARKET_SYMBOL", "ETHUSDT")
    get_settings.cache_clear()

    corpo = client.get("/api/config").json()["config"]
    assert corpo["timeframe"] == "15m"
    assert corpo["market_symbol"] == "BTCUSDT"


def test_settings_nao_tem_campo_de_experimento() -> None:
    """A fronteira entre as camadas e estrutural, nao convencao."""
    campos_env = set(get_settings().model_dump().keys())
    campos_experimento = set(ExperimentConfig().model_dump().keys())
    assert campos_env & campos_experimento == set()


# ---------------------------------------------------- servico, nivel unitario


def test_criar_versao_levanta_excecoes_tipadas(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    settings = get_settings()

    with pytest.raises(SemMudanca):
        criar_versao(conn, settings, alteracoes={}, author="t")

    with pytest.raises(TetoExcedido):
        criar_versao(
            conn, settings, alteracoes={"max_llm_usd_per_run_cents": 99_999}, author="t"
        )

    conn.execute(
        "INSERT INTO run (agent_id, state, config_version_id, created_at,"
        " updated_at) VALUES ('agent-0001','pausado',1,datetime('now'),"
        " datetime('now'))"
    )
    with pytest.raises(ConfigCongelada):
        criar_versao(conn, settings, alteracoes={"default_seed": 1}, author="t")


def test_versao_atual_reflete_a_ultima(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    criar_versao(conn, get_settings(), alteracoes={"default_seed": 7}, author="t")
    atual = versao_atual(conn)
    assert atual is not None
    assert atual.id == 2
    assert atual.config.default_seed == 7


# ============================================================================
# Deriva de schema: o hash gravado ainda descreve a config que ele identifica?
#
# `config_hash` e a identidade da configuracao de um run. Se `ExperimentConfig`
# ganhar um campo, o `payload_json` gravado continua o mesmo mas reconstrui-lo
# passa a produzir outro objeto - e outro hash. Sem esta conferencia, dois runs
# reportariam o mesmo hash tendo rodado com configuracoes diferentes, e a
# comparacao entre eles mentiria sem nada acusar.
# ============================================================================


def test_hash_confere_no_estado_normal(conn) -> None:
    from app.config import service as cs

    atual = cs.versao_atual(conn)
    assert cs.conferir_hash(atual) is None


def test_deriva_de_schema_e_detectada(conn) -> None:
    """Simula o payload de uma versao gravada antes de um campo existir."""
    import json

    from app.config import service as cs

    atual = cs.versao_atual(conn)
    payload = json.loads(atual.config.model_dump_json())
    payload.pop("note", None)  # como se o campo tivesse sido acrescentado depois
    conn.execute(
        "INSERT INTO config_version (created_at, author, parent_version_id,"
        " payload_json, config_hash, material, note)"
        " VALUES ('x','teste',?,?,'hash-de-outra-epoca',1,'')",
        (atual.id, json.dumps(payload)),
    )
    nova = cs.versao_atual(conn)
    assert cs.conferir_hash(nova) is not None


def test_run_e_recusado_sob_hash_divergente(client) -> None:
    """Nao se produz resultado sob um hash que identifica outra coisa."""
    import json

    from app.config import service as cs

    conn = client.app.state.conn
    atual = cs.versao_atual(conn)
    conn.execute(
        "INSERT INTO config_version (created_at, author, parent_version_id,"
        " payload_json, config_hash, material, note)"
        " VALUES ('x','teste',?,?,'hash-que-nao-descreve-mais',1,'')",
        (atual.id, atual.config.model_dump_json()),
    )
    resposta = client.post("/api/run", json={"author": "teste"})
    assert resposta.status_code == 409
    assert "schema da configuracao mudou" in resposta.json()["detail"]
    assert client.get("/api/health").json()["config_hash_confere"] is False
