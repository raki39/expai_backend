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
    # A fase corrente, e nao uma constante que sobreviveu a virada. Este
    # assert existe para o campo nao poder mudar sem alguem decidir: ele
    # acompanha um aviso sobre o que pode ser afirmado.
    assert corpo["fase"] == "0B"


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


# ===========================================================================
# Concorrencia: o painel faz catorze chamadas em paralelo
# ===========================================================================


def test_cada_thread_tem_a_propria_conexao(ambiente) -> None:
    """Uma conexao unica de processo era usada por varias threads ao mesmo
    tempo, e `sqlite3.Connection` nao suporta isso.

    Medido antes do conserto: 3 falhas em 208 requisicoes concorrentes -
    `sqlite3.InterfaceError` virando `500` em `/api/curva` e `503` em
    `/api/config`, cerca de 1,4%.

    A suite nunca viu porque `TestClient` chama uma rota de cada vez. O
    defeito so existe quando duas chamadas se cruzam - mais uma vez, o que a
    suite nao consegue observar ela nao protege.
    """
    import threading

    from app.store import conectar, conexao_do_thread, fechar_conexao_do_thread

    caminho = ambiente
    # O banco nasce ANTES da corrida. Deixar oito threads criarem o arquivo
    # ao mesmo tempo mede outra coisa - primeiro toque concorrente - e nao a
    # propriedade que interessa aqui.
    conectar(caminho).close()

    vistas: list[int] = []
    trava = threading.Lock()

    def usar() -> None:
        conn = conexao_do_thread(caminho)
        # Usa de verdade: obter o objeto nao prova que ele funciona.
        conn.execute("SELECT 1").fetchone()
        with trava:
            vistas.append(id(conn))
        fechar_conexao_do_thread()

    threads = [threading.Thread(target=usar) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(vistas) == 8
    assert len(set(vistas)) == 8, "duas threads compartilharam a mesma conexao"


def test_a_mesma_thread_reaproveita_a_conexao(ambiente) -> None:
    """Abrir uma conexao por consulta funcionaria e desperdicaria; o contrato
    e uma por thread."""
    from app.store import conexao_do_thread, fechar_conexao_do_thread

    try:
        primeira = conexao_do_thread(ambiente)
        assert conexao_do_thread(ambiente) is primeira
    finally:
        fechar_conexao_do_thread()


def test_trocar_de_banco_troca_a_conexao(ambiente, tmp_path) -> None:
    """Guardar so a conexao, sem o caminho, faria a thread continuar lendo o
    banco anterior - defeito silencioso e do tipo que este projeto coleciona.
    """
    from app.store import conexao_do_thread, fechar_conexao_do_thread

    try:
        primeira = conexao_do_thread(ambiente)
        outro = tmp_path / "outro.sqlite3"
        segunda = conexao_do_thread(outro)
        assert segunda is not primeira
        assert conexao_do_thread(outro) is segunda
    finally:
        fechar_conexao_do_thread()


def test_o_painel_inteiro_em_paralelo_nao_derruba_rota_nenhuma(
    client, ambiente
) -> None:
    """A forma exata do que o painel faz: todas as telas de uma vez.

    E o teste que teria pego o defeito. Repetido, porque uma falha de
    interleaving nao acontece na primeira tentativa.
    """
    import concurrent.futures as cf

    rotas = [
        "/api/health", "/api/config", "/api/dataset", "/api/ledger",
        "/api/ledger/transacoes", "/api/sentinel", "/api/simulador",
        "/api/execucoes", "/api/comparacao", "/api/agente", "/api/curva",
        "/api/relatorio", "/api/exportar",
    ]

    def bater(rota: str) -> tuple[str, int]:
        return rota, client.get(rota).status_code

    ruins: list[tuple[str, int]] = []
    with cf.ThreadPoolExecutor(max_workers=len(rotas)) as ex:
        for _ in range(6):
            for rota, status in ex.map(bater, rotas):
                if status != 200:
                    ruins.append((rota, status))

    assert not ruins, ruins


# ===========================================================================
# Docs interativos: interruptor, e nao decisao permanente
# ===========================================================================


def test_docs_desligados_por_padrao(client: TestClient) -> None:
    """Sem `HABILITAR_DOCS`, a superficie nao e revelada.

    O Swagger UI busca `openapi.json` SEM autenticacao - e assim que ele
    funciona -, entao deixa-lo ligado publicaria a lista de rotas de um
    servico que exige token em todas elas. Nao vaza dado; revela superficie, e
    revelar superficie por conveniencia e o tipo de decisao que se toma sem
    perceber.
    """
    for rota in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(rota).status_code == 404, rota


def test_docs_ligam_com_a_env_var(monkeypatch, ambiente) -> None:
    """Liga, usa, desliga. E `/redoc` continua fora: um caminho basta."""
    from fastapi.testclient import TestClient as TC

    from app.main import criar_app
    from app.settings import get_settings

    monkeypatch.setenv("HABILITAR_DOCS", "1")
    get_settings.cache_clear()
    try:
        with TC(criar_app()) as c:
            assert c.get("/docs").status_code == 200
            assert c.get("/openapi.json").status_code == 200
            assert c.get("/redoc").status_code == 404
    finally:
        get_settings.cache_clear()


def test_a_pagina_de_docs_nao_carrega_o_token(monkeypatch, ambiente) -> None:
    """Regra 15: segredo nunca aparece em pagina. Nem para dar conveniencia.

    Embutir o `API_SERVICE_TOKEN` no HTML do Swagger tornaria o `Authorize`
    automatico - e poria a credencial de servico numa pagina, que e
    exatamente o que a regra proibe sem excecao. Quem investiga cola o token
    a mao.
    """
    from fastapi.testclient import TestClient as TC

    from app.main import criar_app
    from app.settings import get_settings

    monkeypatch.setenv("HABILITAR_DOCS", "1")
    monkeypatch.setenv("API_SERVICE_TOKEN", "segredo-que-nao-pode-vazar")
    get_settings.cache_clear()
    try:
        with TC(criar_app()) as c:
            for rota in ("/docs", "/openapi.json"):
                assert "segredo-que-nao-pode-vazar" not in c.get(rota).text
    finally:
        get_settings.cache_clear()


def test_as_rotas_continuam_exigindo_token_com_docs_ligados(
    monkeypatch, ambiente
) -> None:
    """Ligar docs revela a lista de rotas, e SO isso.

    Se ligar os docs afrouxasse a autenticacao, o interruptor deixaria de ser
    sobre superficie e passaria a ser sobre acesso - coisa completamente
    diferente da que foi decidida.
    """
    from fastapi.testclient import TestClient as TC

    from app.main import criar_app
    from app.settings import get_settings

    monkeypatch.setenv("HABILITAR_DOCS", "1")
    get_settings.cache_clear()
    try:
        with TC(criar_app()) as c:
            assert c.get("/api/health").status_code == 401
            assert c.get("/api/separacao").status_code == 401
    finally:
        get_settings.cache_clear()


def test_habilitar_docs_vazio_nao_derruba_o_boot(monkeypatch, ambiente) -> None:
    """`HABILITAR_DOCS=` era um `ValidationError` fatal - e estava no exemplo.

    O `.env.example` shipou o campo VAZIO. Quem copiasse o arquivo nao subiria
    o servico, e a mensagem falaria de booleano invalido em vez de dizer que
    ele copiou o exemplo.

    Variavel de ambiente vazia significa "nao defini". Derrubar o servico
    inteiro por isso e a forma mais cara de ser rigoroso.
    """
    from app.settings import Settings, get_settings

    get_settings.cache_clear()
    try:
        for vazio in ("", "   "):
            monkeypatch.setenv("HABILITAR_DOCS", vazio)
            assert Settings().habilitar_docs is False
    finally:
        monkeypatch.delenv("HABILITAR_DOCS", raising=False)
        get_settings.cache_clear()


def test_habilitar_docs_aceita_sim_e_nao(monkeypatch, ambiente) -> None:
    """O projeto e escrito em portugues; `sim` e o que se digita antes de ler."""
    from app.settings import Settings, get_settings

    get_settings.cache_clear()
    try:
        for texto, esperado in (
            ("sim", True), ("SIM", True), ("nao", False), ("não", False),
            ("1", True), ("true", True), ("0", False),
        ):
            monkeypatch.setenv("HABILITAR_DOCS", texto)
            assert Settings().habilitar_docs is esperado, texto
    finally:
        monkeypatch.delenv("HABILITAR_DOCS", raising=False)
        get_settings.cache_clear()


def test_valor_sem_sentido_continua_sendo_recusado(monkeypatch, ambiente) -> None:
    """Tolerar vazio nao e tolerar qualquer coisa.

    `HABILITAR_DOCS=talvez` e um engano de quem escreveu, e falhar alto ali e
    o comportamento certo - diferente de vazio, que significa "nao defini".
    """
    import pytest as _pytest

    from app.settings import Settings, get_settings

    get_settings.cache_clear()
    try:
        monkeypatch.setenv("HABILITAR_DOCS", "talvez")
        with _pytest.raises(Exception):
            Settings()
    finally:
        monkeypatch.delenv("HABILITAR_DOCS", raising=False)
        get_settings.cache_clear()
