"""Criterios 6 e 12 do incremento 0: persistencia e nao vazamento de segredo.

Tambem cobre migracao idempotente e o formato dos logs.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.logging_setup import JsonFormatter, RedacaoFilter, configurar_logging
from app.settings import SECRET_FIELDS, get_settings
from app.store import conectar, migrar, versao_schema, volume_gravavel

from .conftest import ANTHROPIC, OPENAI, TOKEN

SEGREDOS = (TOKEN, ANTHROPIC, OPENAI)


# ----------------------------------------------- criterio 6: persistencia


def test_dado_sobrevive_a_reabertura_do_banco(client: TestClient, ambiente: Path) -> None:
    """Versao local do teste de redeploy.

    Na Railway o equivalente e: gravar, redeployar, conferir que sobreviveu.
    Aqui fechamos e reabrimos o arquivo, que e o mesmo mecanismo sem a
    plataforma no meio.
    """
    r = client.post("/api/sentinel", json={"label": "antes-do-redeploy"})
    assert r.status_code == 201
    sentinel_id = r.json()["id"]

    client.app.state.conn.close()

    conn = conectar(ambiente)
    try:
        linha = conn.execute(
            "SELECT label FROM sentinel WHERE id = ?", (sentinel_id,)
        ).fetchone()
        assert linha is not None
        assert linha["label"] == "antes-do-redeploy"
    finally:
        conn.close()


def test_arquivo_do_banco_existe_no_caminho_configurado(
    client: TestClient, ambiente: Path
) -> None:
    assert ambiente.exists(), "o banco nao foi criado onde DB_PATH aponta"


def test_volume_gravavel_detecta_diretorio_ruim(tmp_path: Path) -> None:
    assert volume_gravavel(tmp_path / "novo") is True
    arquivo = tmp_path / "arquivo"
    arquivo.write_text("x", encoding="utf-8")
    assert volume_gravavel(arquivo / "sub") is False


# --------------------------------------------------------------- migracao


def test_migracao_e_idempotente(ambiente: Path) -> None:
    conn = conectar(ambiente)
    try:
        v1 = migrar(conn)
        v2 = migrar(conn)
        assert v1 == v2 == versao_schema(conn)
        aplicadas = conn.execute("SELECT COUNT(*) c FROM schema_migration").fetchone()
        assert aplicadas["c"] == v1
    finally:
        conn.close()


def test_pragmas_aplicados(ambiente: Path) -> None:
    conn = conectar(ambiente)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_health_reporta_substrato(client: TestClient) -> None:
    corpo = client.get("/api/health").json()
    assert corpo["status"] == "ok"
    assert corpo["schema_version"] >= 1
    assert corpo["config_version"] == 1
    assert corpo["volume_gravavel"] is True
    assert corpo["run_ativo"] is None
    assert corpo["fase"] == "0A"


# -------------------------------------- criterio 12: segredo nao vaza


def _texto(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


@pytest.mark.parametrize("segredo", SEGREDOS)
def test_health_nao_expoe_segredo(client: TestClient, segredo: str) -> None:
    assert segredo not in _texto(client.get("/api/health").json())


@pytest.mark.parametrize("segredo", SEGREDOS)
def test_config_nao_expoe_segredo(client: TestClient, segredo: str) -> None:
    assert segredo not in _texto(client.get("/api/config").json())
    assert segredo not in _texto(client.get("/api/config/history").json())


def test_health_reporta_presenca_e_nao_valor(client: TestClient) -> None:
    """Secao 10.2.4: o painel mostra o ESTADO da credencial, nunca o valor."""
    creds = client.get("/api/health").json()["credenciais_configuradas"]
    assert creds == {"anthropic": True, "openai": True}


def test_settings_nao_serializa_segredo_em_texto(client: TestClient) -> None:
    despejo = _texto(get_settings().model_dump())
    for segredo in SEGREDOS:
        assert segredo not in despejo, "SecretStr deveria mascarar o valor"


def test_redacao_de_log_pega_segredo_solto(caplog: pytest.LogCaptureFixture) -> None:
    """Rede de seguranca: se um segredo escapar para o log, e redigido."""
    filtro = RedacaoFilter(list(SEGREDOS))
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg=f"vazou {ANTHROPIC} aqui", args=(), exc_info=None,
    )
    record.campo_extra = f"e {TOKEN} aqui tambem"
    assert filtro.filter(record) is True
    assert ANTHROPIC not in record.msg
    assert TOKEN not in record.campo_extra
    assert "***REDIGIDO***" in record.msg


def test_lista_de_segredos_cobre_todos_os_secretstr() -> None:
    """SECRET_FIELDS precisa cobrir todo campo SecretStr.

    Um campo novo que escape desta lista nao seria redigido no log.
    """
    settings = get_settings()
    from pydantic import SecretStr

    encontrados = {
        nome
        for nome in settings.model_dump().keys()
        if isinstance(getattr(settings, nome), SecretStr)
    }
    assert encontrados == set(SECRET_FIELDS)


# ------------------------------------------------------------------ logs


def test_log_sai_como_json_de_uma_linha(capsys: pytest.CaptureFixture) -> None:
    configurar_logging("INFO", segredos=[])
    logging.getLogger("t").info("evento.teste", extra={"run_id": "r_1", "n": 3})
    saida = capsys.readouterr().out.strip().splitlines()
    assert len(saida) == 1
    evento = json.loads(saida[0])
    assert evento["event"] == "evento.teste"
    assert evento["level"] == "INFO"
    assert evento["run_id"] == "r_1"
    assert evento["n"] == 3
    assert evento["ts"].endswith("Z")


def test_formatter_serializa_tipos_incomuns() -> None:
    from decimal import Decimal

    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg="evento", args=(), exc_info=None,
    )
    record.valor = Decimal("1.23")
    evento = json.loads(JsonFormatter().format(record))
    assert evento["valor"] == "1.23"


# -------------------------------------------------- sentinela e listagem


def test_sentinela_lista_o_que_gravou(client: TestClient) -> None:
    client.post("/api/sentinel", json={"label": "a"})
    client.post("/api/sentinel", json={"label": "b"})
    corpo = client.get("/api/sentinel").json()
    assert corpo["total"] == 2
    assert [i["label"] for i in corpo["items"]] == ["b", "a"]


def test_run_exige_config_version_existente(conn: sqlite3.Connection) -> None:
    """Chave estrangeira ligada: run orfao nao entra."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO run (agent_id, state, config_version_id, created_at,"
            " updated_at) VALUES ('a','executando',999,datetime('now'),"
            " datetime('now'))"
        )


# ------------------------------------------------- divisor de SQL da migracao


def test_divisor_preserva_corpo_de_trigger() -> None:
    """O ';' dentro de BEGIN...END nao pode virar ponto de corte.

    Dividir por ';' quebraria todo trigger de imutabilidade do projeto.
    """
    from app.store import dividir_statements

    sql = """
    CREATE TABLE t (id INTEGER PRIMARY KEY);
    CREATE TRIGGER t_sem_update
    BEFORE UPDATE ON t
    BEGIN
        SELECT RAISE(ABORT, 'imutavel');
    END;
    CREATE INDEX idx_t ON t(id);
    """
    statements = dividir_statements(sql)
    assert len(statements) == 3
    assert "RAISE(ABORT" in statements[1]
    assert statements[1].rstrip().endswith("END;")


def test_divisor_recusa_script_incompleto() -> None:
    from app.store import dividir_statements

    with pytest.raises(ValueError, match="incompleto"):
        dividir_statements("CREATE TABLE t (id INTEGER")


def test_divisor_aceita_comentarios() -> None:
    from app.store import dividir_statements

    sql = "-- comentario\nCREATE TABLE t (id INTEGER);\n-- outro\n"
    assert len(dividir_statements(sql)) == 1


def test_todas_as_migracoes_sao_divisiveis() -> None:
    """Guarda contra migracao futura que quebre o divisor."""
    from app.migrations import MIGRACOES
    from app.store import dividir_statements

    for versao, _, sql in MIGRACOES:
        assert dividir_statements(sql), f"migracao {versao} nao produziu statements"


# ------------------------------- rede de seguranca de ambiente de producao


def test_railway_com_app_env_local_falha_alto(
    ambiente: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A falha mais cara possivel: rodar na Railway gravando em disco efemero.

    Sem esta trava o servico sobe normalmente, grava em ./var e o banco some
    no redeploy seguinte - sem erro, sem aviso.
    """
    from app.settings import Settings

    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("APP_ENV", "local")
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="filesystem efemero"):
        Settings()


def test_railway_com_app_env_correto_passa(
    ambiente: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.settings import Settings

    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("APP_ENV", "railway")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "d" / "fase0a.sqlite3"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d" / "datasets"))
    get_settings.cache_clear()

    s = Settings()
    assert s.app_env == "railway"


def test_producao_exige_caminho_absoluto(
    ambiente: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.settings import Settings

    monkeypatch.setenv("APP_ENV", "railway")
    monkeypatch.setenv("DB_PATH", "./var/fase0a.sqlite3")
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="caminho absoluto"):
        Settings()


def test_producao_exige_token(
    ambiente: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.settings import Settings

    monkeypatch.setenv("APP_ENV", "railway")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "d.sqlite3"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "ds"))
    monkeypatch.setenv("API_SERVICE_TOKEN", "")
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="API_SERVICE_TOKEN"):
        Settings()


# ------------------------------------- deteccao de volume montado de verdade


def test_volume_montado_falso_quando_e_diretorio_comum(tmp_path: Path) -> None:
    """Escrever com sucesso NAO prova persistencia.

    O Dockerfile cria /data na imagem, entao o app grava normalmente mesmo
    sem volume - e perde tudo no redeploy. Foi o que aconteceu no primeiro
    deploy, e `volume_gravavel` sozinho nao pegou.
    """
    from app.store import volume_gravavel, volume_montado

    alvo = tmp_path / "data"
    alvo.mkdir()

    # Gravavel: sim. Persistente: nao - e o mesmo dispositivo da raiz.
    assert volume_gravavel(alvo) is True
    if os.name == "posix":
        assert volume_montado(alvo) is False
    else:
        assert volume_montado(alvo) is None


def test_volume_montado_none_fora_de_posix(monkeypatch: pytest.MonkeyPatch,
                                           tmp_path: Path) -> None:
    """"Nao sei" nao pode virar "nao esta montado"."""
    import app.store as store

    monkeypatch.setattr(store.os, "name", "nt")
    assert store.volume_montado(tmp_path) is None


def test_producao_recusa_subir_sem_volume(
    ambiente: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A trava que faltava: falhar alto em vez de perder dados em silencio."""
    import app.store as store
    from app.main import criar_app

    monkeypatch.setenv("APP_ENV", "railway")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "fase0a.sqlite3"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data" / "datasets"))
    get_settings.cache_clear()
    monkeypatch.setattr(store, "volume_montado", lambda _: False)
    monkeypatch.setattr("app.main.volume_montado", lambda _: False)

    with pytest.raises(RuntimeError, match="VOLUME AUSENTE"):
        with TestClient(criar_app()):
            pass


def test_producao_sobe_com_volume_montado(
    ambiente: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.main import criar_app

    monkeypatch.setenv("APP_ENV", "railway")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "fase0a.sqlite3"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data" / "datasets"))
    get_settings.cache_clear()
    monkeypatch.setattr("app.main.volume_montado", lambda _: True)

    with TestClient(criar_app()) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        assert c.get("/api/health").status_code == 200


def test_health_separa_gravavel_de_montado(client: TestClient) -> None:
    corpo = client.get("/api/health").json()
    assert "volume_gravavel" in corpo
    assert "volume_montado" in corpo
