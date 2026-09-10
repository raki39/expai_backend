"""O A1a PARCELADO: as onze garantias, cada uma com teste. OP-1.

> *"alvo e plano congelados antes da primeira etapa; uma única cópia
> temporária identificada e selada para a execução; etapas com índice e estado
> persistidos; no máximo uma etapa em andamento; retry idempotente; nenhum
> resultado concluído sobrescrito; perda da cópia temporária aborta
> explicitamente a execução — nunca reconstrói silenciosamente sobre outro
> estado; manifesto somente depois de todas as etapas e casos; conteúdo
> canônico no certificado, nunca IDs temporários; descarte da cópia após
> conclusão ou aborto; banco oficial permanece sem hipóteses, créditos,
> tentativas ou holdout novos."* — o usuário, 2026-09-10
"""

from __future__ import annotations

import inspect
import json
import pathlib
import re
import sqlite3

import pytest

from app.certificacao import canonico, escopos, laboratorio, parcelado
from tests.test_certificacao import cenario_cru  # noqa: F401
from tests.test_portao_a import cenario  # noqa: F401


def _foto(conn: sqlite3.Connection) -> dict:
    """As nove tabelas que a certificação não pode tocar, e o orçamento."""
    return laboratorio.fotografia(conn) | {
        "test_credit_budget": int(
            conn.execute("SELECT COUNT(*) FROM test_credit_budget").fetchone()[0]
        )
    }


def _iniciar(conn, cenario, **extra):
    dataset_id, cfg = cenario
    return parcelado.iniciar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        dataset_hash="a" * 64, build_do_backend="deadbeef", **extra,
    )


def _caminho(conn, execucao_id: int) -> pathlib.Path:
    return pathlib.Path(
        conn.execute(
            "SELECT caminho FROM certificacao_copia WHERE execucao_id = ?",
            (execucao_id,),
        ).fetchone()[0]
    )


_FORMA_DE_ID = re.compile(r"(^|_)ids?$|_original$")


def _ids_no(valor, caminho=""):
    """Todo id de linha que sobrou num documento — a garantia 9 por inspeção."""
    if isinstance(valor, dict):
        for k, x in valor.items():
            if (
                _FORMA_DE_ID.search(k)
                and k not in canonico.PERSISTENTES
                and isinstance(x, int)
                and not isinstance(x, bool)
            ):
                yield f"{caminho}.{k}={x}"
            yield from _ids_no(x, f"{caminho}.{k}")
    elif isinstance(valor, list):
        for i, x in enumerate(valor):
            yield from _ids_no(x, f"{caminho}[{i}]")


# ---------------------------------------------------------------------------
# O plano saiu da MEDICAO
# ---------------------------------------------------------------------------


def test_o_plano_sai_da_MEDICAO_e_do_catalogo():
    """Uma etapa por unidade indivisível, e o catálogo gera as seis últimas."""
    from app.a1a import catalogo

    assert escopos.PLANO_A1A[:2] == ("preparo.baselines", "preparo.b4")
    assert escopos.PLANO_A1A[2:] == tuple(
        f"a1a.{f.chave}" for f in catalogo.FAMILIAS
    )
    assert escopos.BLOCOS[escopos.A1A] == len(escopos.PLANO_A1A) == 8
    fonte = inspect.getsource(escopos)
    assert "Medido em 2026-09-10" in fonte
    assert "unidade indivisível" in fonte


def test_os_pragmas_da_copia_sao_so_de_DESEMPENHO():
    """Um pragma que mudasse semântica faria a cópia deixar de ser o caminho real.

    `foreign_keys=OFF` desligaria a guarda que A1a existe para exercitar; e a
    medição que escolheu `temp_store` exigiu resultado idêntico byte a byte.
    """
    permitidos = {"temp_store", "mmap_size", "cache_size"}
    for pragma in laboratorio.PRAGMAS_DA_COPIA:
        nome = re.match(r"PRAGMA\s+(\w+)", pragma).group(1)
        assert nome in permitidos, pragma


def test_abrir_copia_NUNCA_cria_arquivo(tmp_path):
    """`sqlite3.connect` criaria um banco VAZIO com o mesmo nome."""
    caminho = tmp_path / "sumiu.sqlite3"
    with pytest.raises(FileNotFoundError):
        laboratorio.abrir_copia(caminho)
    assert not caminho.exists()


# ---------------------------------------------------------------------------
# 1 e 2: alvo e plano congelados, uma copia selada
# ---------------------------------------------------------------------------


def test_iniciar_CONGELA_alvo_e_plano_e_SELA_a_copia(conn, cenario):
    antes = _foto(conn)
    e = _iniciar(conn, cenario)
    eid = e["execucao_id"]
    try:
        assert e["concluidas"] == 0 and e["proxima"] == 0
        assert e["plano"] == list(escopos.PLANO_A1A)
        assert e["insumos_congelados"]["plano"] == list(escopos.PLANO_A1A)
        assert len(e["alvo_de_certificacao_hash"]) == 64
        caminho = _caminho(conn, eid)
        assert caminho.exists()
        assert e["copia"]["impressao_do_selo"] == laboratorio.impressao(caminho)
        # Nenhuma etapa rodou ainda: congelar vem ANTES.
        assert conn.execute(
            "SELECT COUNT(*) FROM certificacao_bloco WHERE execucao_id = ?",
            (eid,),
        ).fetchone()[0] == 0
        # Idempotente: iniciar de novo devolve a MESMA execucao.
        assert _iniciar(conn, cenario)["execucao_id"] == eid
        assert _foto(conn) == antes
    finally:
        parcelado.abortar(conn, execucao_id=eid, motivo="fim do teste")


# ---------------------------------------------------------------------------
# O caminho inteiro: oito etapas, selo, descarte, oficial intocado
# ---------------------------------------------------------------------------


def test_as_OITO_etapas_certificam_e_o_experimento_fica_INTOCADO(conn, cenario_cru):
    """O estado da `cv9`: sem B3 e sem hipótese — o preparo roda NA CÓPIA."""
    dataset_id, cfg = cenario_cru
    antes = _foto(conn)

    cert = parcelado.de_uma_vez(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        dataset_hash="a" * 64, build_do_backend="deadbeef",
    )

    assert cert.passa is True
    e = parcelado.estado(conn, cert.execucao_id)
    assert [x["estado"] for x in e["etapas"]] == ["concluida"] * 8
    assert e["selado"]["passa"] is True
    assert len(cert.manifesto["casos"]) == escopos.CASOS_ESPERADOS["a1a"]
    assert cert.manifesto["suite"]["blocos"] == 8
    preparo = cert.manifesto["preparo_do_laboratorio"]
    assert "rodados na copia" in preparo["baselines"]
    assert "rodado na copia" in preparo["b4"]
    assert set(preparo["micros_por_etapa"]) == set(escopos.PLANO_A1A)

    # Garantia 11: o banco OFICIAL sem hipotese, credito, tentativa ou holdout.
    assert _foto(conn) == antes

    # Garantia 10: a copia foi DESCARTADA, e o descarte registrado.
    assert e["copia"]["descartada"]["motivo"] == "selada"
    assert e["copia"]["descartada"]["residuo"] is False
    assert not _caminho(conn, cert.execucao_id).exists()

    # Garantia 9: CONTEUDO, nunca id temporario - nem o token da copia.
    assert list(_ids_no(cert.manifesto)) == []
    removidos = cert.manifesto["ids_temporarios_removidos"]["caminhos"]
    assert any("duplicata.hypothesis_original" in c for c in removidos), (
        "a duplicacao citava a hipotese da COPIA e o selo nao a removeu"
    )
    assert e["copia"]["token"] not in json.dumps(cert.manifesto)

    # Selar de novo devolve o MESMO certificado.
    de_novo = parcelado.selar(conn, execucao_id=cert.execucao_id, config=cfg)
    assert de_novo.manifesto == cert.manifesto


def test_suite_executar_a1a_passa_pelo_MESMO_caminho_parcelado(conn, cenario):
    """Não há um segundo caminho monolítico: ele divergiria no primeiro campo novo."""
    from app.certificacao import por_escopo, suite

    assert escopos.A1A not in por_escopo.DE_UMA_VEZ
    assert not hasattr(por_escopo, "rodar_a1a")
    dataset_id, cfg = cenario
    cert = suite.executar(
        conn, escopo="a1a", dataset_id=dataset_id, config=cfg,
        config_version_id=1, dataset_hash="a" * 64, build_do_backend="x",
    )
    assert parcelado.estado(conn, cert.execucao_id)["concluidas"] == 8


# ---------------------------------------------------------------------------
# 3, 5 e 6: estado persistido, retry idempotente, nada sobrescrito
# ---------------------------------------------------------------------------


def test_retry_e_IDEMPOTENTE_e_nada_concluido_e_SOBRESCRITO(conn, cenario):
    e = _iniciar(conn, cenario)
    eid, cfg = e["execucao_id"], cenario[1]
    try:
        r0 = parcelado.rodar_etapa(conn, execucao_id=eid, config=cfg)
        assert r0["rodou_agora"] and r0["etapa"] == "preparo.baselines"
        # O pedido seguinte NAO roda a etapa 0 de novo: roda a proxima.
        r1 = parcelado.rodar_etapa(conn, execucao_id=eid, config=cfg)
        assert r1["etapa"] == "preparo.b4"
        assert [x["estado"] for x in r1["etapas"][:3]] == [
            "concluida", "concluida", "pendente"
        ]
        imp = json.loads(
            conn.execute(
                "SELECT conteudo_json FROM certificacao_bloco"
                " WHERE execucao_id = ? AND indice_bloco = 0", (eid,)
            ).fetchone()[0]
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO certificacao_bloco (execucao_id, escopo,"
                " indice_bloco, estado, quantas, conteudo_json, micros,"
                " criado_em) VALUES (?, 'a1a', 0, 'concluido', 0, ?, 0, 'x')",
                (eid, json.dumps({"impressao_antes": imp["impressao_antes"],
                                  "impressao_depois": imp["impressao_depois"]})),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE certificacao_bloco SET micros = 1 WHERE execucao_id = ?",
                (eid,),
            )
    finally:
        parcelado.abortar(conn, execucao_id=eid, motivo="fim do teste")


def test_etapa_INTERROMPIDA_sem_escrita_reroda_com_NOVA_tentativa(conn, cenario):
    """O processo morreu depois de registrar a tentativa e antes de escrever.

    A cópia está na impressão de antes, então rodar de novo é idempotente — e a
    tentativa nova fica registrada ao lado da interrompida.
    """
    e = _iniciar(conn, cenario)
    eid = e["execucao_id"]
    try:
        conn.execute(
            "INSERT INTO certificacao_etapa_tentativa (execucao_id,"
            " indice_etapa, tentativa, impressao_antes, iniciada_em)"
            " VALUES (?, 0, 1, ?, 'x')",
            (eid, e["copia"]["impressao_do_selo"]),
        )
        assert parcelado.estado(conn, eid)["etapas"][0]["estado"] == "interrompida"

        r = parcelado.rodar_etapa(conn, execucao_id=eid, config=cenario[1])
        assert r["etapa"] == "preparo.baselines"
        conteudo = json.loads(
            conn.execute(
                "SELECT conteudo_json FROM certificacao_bloco"
                " WHERE execucao_id = ? AND indice_bloco = 0", (eid,)
            ).fetchone()[0]
        )
        assert conteudo["tentativa"] == 2
        assert parcelado.estado(conn, eid)["etapas"][0]["tentativas"] == 2
    finally:
        parcelado.abortar(conn, execucao_id=eid, motivo="fim do teste")


# ---------------------------------------------------------------------------
# 4: no maximo UMA em andamento
# ---------------------------------------------------------------------------


def test_no_maximo_UMA_etapa_em_andamento(conn, cenario):
    e = _iniciar(conn, cenario)
    eid, cfg = e["execucao_id"], cenario[1]
    trava = parcelado._trava(eid)
    trava.acquire()
    try:
        with pytest.raises(parcelado.EtapaEmAndamento):
            parcelado.rodar_etapa(conn, execucao_id=eid, config=cfg)
        with pytest.raises(parcelado.EtapaEmAndamento):
            parcelado.selar(conn, execucao_id=eid, config=cfg)
        assert parcelado.estado(conn, eid)["em_andamento"] is True
    finally:
        trava.release()
    # E nada rodou enquanto a outra estava em andamento.
    assert conn.execute(
        "SELECT COUNT(*) FROM certificacao_etapa_tentativa WHERE execucao_id = ?",
        (eid,),
    ).fetchone()[0] == 0
    parcelado.abortar(conn, execucao_id=eid, motivo="fim do teste")


# ---------------------------------------------------------------------------
# 7: perda da copia ABORTA - e nunca reconstroi
# ---------------------------------------------------------------------------


def test_PERDA_da_copia_ABORTA_e_nunca_RECONSTROI(conn, cenario):
    e = _iniciar(conn, cenario)
    eid, cfg = e["execucao_id"], cenario[1]
    parcelado.rodar_etapa(conn, execucao_id=eid, config=cfg)
    caminho = _caminho(conn, eid)
    laboratorio._apagar(caminho.parent)
    assert not caminho.exists()

    with pytest.raises(parcelado.ExecucaoAbortada) as erro:
        parcelado.rodar_etapa(conn, execucao_id=eid, config=cfg)
    assert "SUMIU" in str(erro.value)
    # NADA foi criado no lugar dela.
    assert not caminho.exists()

    st = parcelado.estado(conn, eid)
    assert "SUMIU" in st["abortada"]["motivo"]
    assert st["copia"]["descartada"]["motivo"] == "abortada"
    assert st["proxima"] is None
    with pytest.raises(parcelado.ExecucaoAbortada):
        parcelado.rodar_etapa(conn, execucao_id=eid, config=cfg)

    # Recomecar e decisao EXPLICITA - e e uma execucao NOVA.
    with pytest.raises(parcelado.ExecucaoAbortada) as erro:
        _iniciar(conn, cenario)
    assert "nova_execucao" in str(erro.value)
    nova = _iniciar(conn, cenario, nova_execucao=True)
    assert nova["execucao_id"] != eid and nova["concluidas"] == 0
    parcelado.abortar(conn, execucao_id=nova["execucao_id"], motivo="fim do teste")


def test_escrita_FORA_da_etapa_muda_a_impressao_e_ABORTA(conn, cenario):
    """Uma etapa interrompida NO MEIO deixa a cópia assim — e ela não reroda."""
    e = _iniciar(conn, cenario)
    eid, cfg = e["execucao_id"], cenario[1]
    parcelado.rodar_etapa(conn, execucao_id=eid, config=cfg)
    caminho = _caminho(conn, eid)
    c = laboratorio.abrir_copia(caminho)
    c.execute("CREATE TABLE intrusa (x INTEGER)")
    c.close()

    with pytest.raises(parcelado.ExecucaoAbortada) as erro:
        parcelado.rodar_etapa(conn, execucao_id=eid, config=cfg)
    assert "nao esta no estado" in str(erro.value)
    assert not caminho.parent.exists()


def test_etapa_que_FALHA_fica_no_registro_e_ABORTA(conn, cenario, monkeypatch):
    e = _iniciar(conn, cenario)
    eid, cfg = e["execucao_id"], cenario[1]
    original = parcelado._executar

    def com_defeito(conn_, nome, congelado, config):
        if nome == "preparo.b4":
            raise RuntimeError("defeito plantado")
        return original(conn_, nome, congelado, config)

    monkeypatch.setattr(parcelado, "_executar", com_defeito)
    parcelado.rodar_etapa(conn, execucao_id=eid, config=cfg)
    with pytest.raises(parcelado.ExecucaoAbortada) as erro:
        parcelado.rodar_etapa(conn, execucao_id=eid, config=cfg)
    assert "defeito plantado" in str(erro.value)

    linha = conn.execute(
        "SELECT estado, conteudo_json FROM certificacao_bloco"
        " WHERE execucao_id = ? AND indice_bloco = 1", (eid,)
    ).fetchone()
    assert linha["estado"] == "falhou"
    assert "defeito plantado" in linha["conteudo_json"]
    st = parcelado.estado(conn, eid)
    assert st["etapas"][1]["estado"] == "falhou"
    assert st["copia"]["descartada"]["motivo"] == "abortada"


def test_ALVO_mudado_entre_etapas_ABORTA(conn, cenario, monkeypatch):
    e = _iniciar(conn, cenario)
    eid = e["execucao_id"]
    real = parcelado.alvo_mod.montar

    def outro_laboratorio(*a, **k):
        return {**real(*a, **k), "alvo_de_certificacao_hash": "0" * 64}

    monkeypatch.setattr(parcelado.alvo_mod, "montar", outro_laboratorio)
    with pytest.raises(parcelado.ExecucaoAbortada) as erro:
        parcelado.rodar_etapa(conn, execucao_id=eid, config=cenario[1])
    assert "o alvo mudou" in str(erro.value)


# ---------------------------------------------------------------------------
# O BANCO impoe ordem, cadeia e aborto - e nao a disciplina de quem chama
# ---------------------------------------------------------------------------


def test_o_BANCO_impoe_ordem_cadeia_e_aborto(conn, cenario):
    e = _iniciar(conn, cenario)
    eid = e["execucao_id"]
    imp = e["copia"]["impressao_do_selo"]
    tentativa = (
        "INSERT INTO certificacao_etapa_tentativa (execucao_id, indice_etapa,"
        " tentativa, impressao_antes, iniciada_em) VALUES (?, ?, ?, ?, 'x')"
    )
    # Fora de ordem: a etapa 3 com zero concluidas.
    with pytest.raises(sqlite3.IntegrityError, match="ordem|impressao"):
        conn.execute(tentativa, (eid, 3, 1, imp))
    # Partindo de OUTRA impressao.
    with pytest.raises(sqlite3.IntegrityError, match="impressao"):
        conn.execute(tentativa, (eid, 0, 1, "f" * 64))
    # Resultado sem tentativa no mesmo indice.
    with pytest.raises(sqlite3.IntegrityError, match="sem tentativa"):
        conn.execute(
            "INSERT INTO certificacao_bloco (execucao_id, escopo, indice_bloco,"
            " estado, quantas, conteudo_json, micros, criado_em)"
            " VALUES (?, 'a1a', 0, 'concluido', 0, ?, 0, 'x')",
            (eid, json.dumps({"impressao_antes": imp, "impressao_depois": imp})),
        )
    # A copia selada nao troca.
    with pytest.raises(sqlite3.IntegrityError, match="imutavel"):
        conn.execute(
            "UPDATE certificacao_copia SET caminho = 'outro' WHERE execucao_id = ?",
            (eid,),
        )
    # Descarte que declara um fim que nao aconteceu.
    with pytest.raises(sqlite3.IntegrityError, match="selada exige manifesto"):
        conn.execute(
            "INSERT INTO certificacao_copia_descarte (execucao_id, motivo,"
            " residuo, descartada_em) VALUES (?, 'selada', 0, 'x')",
            (eid,),
        )
    parcelado.abortar(conn, execucao_id=eid, motivo="fim do teste")
    # Depois do aborto, nenhuma etapa.
    with pytest.raises(sqlite3.IntegrityError, match="abortada"):
        conn.execute(tentativa, (eid, 0, 1, imp))


def test_copia_selada_so_existe_em_a1a_PARCELADO(conn):
    """O A1b não tem cópia, e o banco recusa que ganhe uma."""
    conn.execute(
        "INSERT INTO certificacao_execucao (alvo_hash, componentes_json,"
        " iniciada_em, casos_esperados, intocado_json, escopo,"
        " blocos_esperados) VALUES (?, '{}', 'x', 2, '{}', 'a1b', 8)",
        ("b" * 64,),
    )
    eid = conn.execute("SELECT MAX(id) FROM certificacao_execucao").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="parcelada"):
        conn.execute(
            "INSERT INTO certificacao_copia (execucao_id, token, caminho,"
            " impressao, bytes, micros_para_copiar, selada_em)"
            " VALUES (?, ?, 'c', ?, 1, 0, 'x')",
            (eid, "t" * 32, "i" * 64),
        )


# ---------------------------------------------------------------------------
# 9: o certificado cita CONTEUDO
# ---------------------------------------------------------------------------


def test_canonico_remove_ids_da_COPIA_e_mantem_os_do_OFICIAL():
    """A regra é por FORMA, com lista positiva do que fica — e ela é publicada."""
    limpo, removidos = canonico.sem_ids_temporarios(
        {
            "run_id": 99,
            "b1_run_id": 7,  # nome NOVO: uma lista fixa deixaria passar
            "hypothesis_original": 57,
            "run_ids": [1, 2],
            "config_version_id": 2,
            "dataset_id": 1,
            "promovido": False,
            "content_hash_original": "abc",  # texto: conteudo, e nao id
            "corridas": [{"run_id": 82, "operacoes_alvo": 70}],
        }
    )
    assert limpo == {
        "config_version_id": 2,
        "dataset_id": 1,
        "promovido": False,
        "content_hash_original": "abc",
        "corridas": [{"operacoes_alvo": 70}],
    }
    assert sorted(removidos) == sorted(
        [".run_id", ".b1_run_id", ".hypothesis_original", ".run_ids",
         ".corridas[0].run_id"]
    )


def test_o_a2_tambem_deixa_de_citar_run_da_copia(conn, cenario):
    """O certificado selado do a2 citava os runs 82 e 99 — linhas da cópia."""
    from app.certificacao import suite

    dataset_id, cfg = cenario
    cert = suite.executar(
        conn, escopo="a2", dataset_id=dataset_id, config=cfg,
        config_version_id=1, dataset_hash="a" * 64, build_do_backend="x",
    )
    assert list(_ids_no(cert.manifesto)) == []
    assert "ids_temporarios_removidos" in cert.manifesto


# ---------------------------------------------------------------------------
# Pela API: o que o painel faz, uma etapa por pedido
# ---------------------------------------------------------------------------


def test_o_POST_de_uma_vez_RECUSA_o_a1a(client, cenario):
    """Produção mediu 196 a 207 s numa requisição só: é a aposta no timeout."""
    r = client.post("/api/certificacao", json={"author": "teste", "escopo": "a1a"})
    assert r.status_code == 422
    assert "parcelado" in r.json()["detail"]


def test_o_PAINEL_vai_ate_o_fim_pela_API_uma_etapa_por_pedido(client, cenario):
    antes = client.get("/api/certificacao/a1a").json()
    assert antes["iniciada"] is False
    assert antes["plano"] == list(escopos.PLANO_A1A)

    feitas = []
    for _ in escopos.PLANO_A1A:
        r = client.post("/api/certificacao/a1a", json={"author": "teste"})
        assert r.status_code == 200, r.text
        feitas.append(r.json()["etapa"])
    assert feitas == list(escopos.PLANO_A1A)

    # Nada falta: o pedido seguinte NAO roda nada, e diz o que fazer.
    r = client.post("/api/certificacao/a1a", json={"author": "teste"})
    assert r.status_code == 200 and "selar" in r.json()["por_que"]

    r = client.post("/api/certificacao/a1a", json={"author": "teste", "selar": True})
    assert r.status_code == 200, r.text
    assert r.json()["passa"] is True

    depois = client.get("/api/certificacao/a1a").json()
    assert depois["selado"]["passa"] is True
    assert depois["copia"]["descartada"]["motivo"] == "selada"
    portao = client.get("/api/certificacao").json()["portao_a"]
    assert portao["escopos"]["a1a"]["estado"] == "certificado"


def test_depois_de_um_ABORTO_recomecar_e_pedido_EXPLICITO(client, conn, cenario):
    r = client.post("/api/certificacao/a1a", json={"author": "teste"})
    eid = r.json()["execucao_id"]
    parcelado.abortar(conn, execucao_id=eid, motivo="aborto do teste")

    r = client.post("/api/certificacao/a1a", json={"author": "teste"})
    assert r.status_code == 409
    assert "nova_execucao" in r.json()["detail"]

    r = client.post(
        "/api/certificacao/a1a", json={"author": "teste", "nova_execucao": True}
    )
    assert r.status_code == 200, r.text
    assert r.json()["execucao_id"] != eid
    parcelado.abortar(conn, execucao_id=r.json()["execucao_id"], motivo="fim")


# ---------------------------------------------------------------------------
# 11: a fotografia do banco oficial VE o holdout - e antes nao via
# ---------------------------------------------------------------------------


def test_a_fotografia_ve_TODAS_as_tabelas_que_vigia(conn):
    """Os cinco certificados da cv9 publicaram holdout_uso: -1.

    -1 queria dizer "a tabela nao existe" - e o nome real e holdout_access. A
    conferencia de holdout comparava -1 com -1 e passava sempre.
    """
    foto = laboratorio.fotografia(conn)
    assert "holdout_access" in foto and "test_credit_budget" in foto
    assert "holdout_uso" not in foto
    assert all(isinstance(v, int) and v >= 0 for v in foto.values()), foto


def test_fotografia_CEGA_levanta_em_vez_de_passar(conn, monkeypatch):
    monkeypatch.setattr(
        laboratorio,
        "TABELAS_QUE_A_CERTIFICACAO_NAO_PODE_TOCAR",
        laboratorio.TABELAS_QUE_A_CERTIFICACAO_NAO_PODE_TOCAR + ("nao_existe",),
    )
    with pytest.raises(laboratorio.FotografiaCega, match="nao_existe"):
        laboratorio.fotografia(conn)


def test_um_uso_de_HOLDOUT_no_banco_oficial_RECUSA_o_selo(conn, cenario):
    """A nao-vacuidade da guarda que estava cega.

    Um uso de holdout que aparecesse no banco oficial durante a certificacao
    passava pela fotografia antiga sem ser visto. Agora o selo recusa, e a
    execucao fica abortada com o nome da tabela.
    """
    from app.certificacao import suite

    dataset_id, cfg = cenario
    e = _iniciar(conn, cenario)
    eid = e["execucao_id"]
    while parcelado.estado(conn, eid)["proxima"] is not None:
        parcelado.rodar_etapa(conn, execucao_id=eid, config=cfg)

    hipotese = conn.execute("SELECT MIN(id) FROM hypothesis").fetchone()[0]
    conn.execute(
        "INSERT INTO holdout_access (hypothesis_id, dataset_id, requested_at,"
        " solicitante, finalidade, creditos, barras_lidas)"
        " VALUES (?, ?, 'x', 'validador', 'uso plantado pelo teste', 0, 0)",
        (hipotese, dataset_id),
    )

    with pytest.raises(suite.CertificacaoRecusada, match="holdout_access"):
        parcelado.selar(conn, execucao_id=eid, config=cfg)
    st = parcelado.estado(conn, eid)
    assert st["selado"] is None
    assert "holdout_access" in st["abortada"]["motivo"]

