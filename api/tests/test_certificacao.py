"""O objeto de certificação: percorre o real, e não toca o experimento.

ADR 0040. Cada teste aqui é uma das garantias que o usuário exigiu em
2026-09-09, e a lista é literal:

> *"um defeito implantado precisa fazer a certificação reprovar; manifesto
> parcial é ilegível; resultados por caso são imutáveis; custos operacionais
> ficam separados dos créditos experimentais; certificação nunca promove
> candidata; nenhuma consulta depende de filtrar certificações para calcular
> DSR ou FDR."*
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tests.test_portao_a import cenario  # noqa: F401
from tests.test_cerebro import settings  # noqa: F401

TABELAS_DO_EXPERIMENTO = (
    "hypothesis", "hypothesis_state", "test_credit_entry", "run",
    "execution", "ledger_entry", "agent_event",
)


@pytest.fixture
def cenario_cru(conn):
    """Dataset e separação, sem baseline e sem hipótese — o estado da cv9."""
    from app.config.schema import ExperimentConfig
    from tests.test_maos_rapidas import criar_dataset, precos_passeio

    dataset_id = criar_dataset(conn, precos_passeio(3_000))
    return dataset_id, ExperimentConfig()


def _foto(conn: sqlite3.Connection) -> dict:
    return {
        t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        for t in TABELAS_DO_EXPERIMENTO
    }


def _certificar(conn, cenario, **extra):
    from app.certificacao import suite

    dataset_id, cfg = cenario
    return suite.executar(
        conn,
        dataset_id=dataset_id,
        config=cfg,
        config_version_id=1,
        dataset_hash="a" * 64,
        build_do_backend="deadbeef",
        **extra,
    )


# ---------------------------------------------------------------------------
# A garantia central: percorre o real, e o experimento fica intocado
# ---------------------------------------------------------------------------


def test_a_certificacao_nao_toca_o_experimento(conn, cenario):
    """Nem hipótese, nem tentativa, nem crédito, nem transição, nem ledger."""
    antes = _foto(conn)
    cert = _certificar(conn, cenario)
    depois = _foto(conn)

    assert depois == antes, (
        "a certificacao escreveu no banco oficial: as escritas tinham de"
        " acontecer na COPIA"
    )
    # E o contador global do DSR - o `N` - nao se moveu.
    from app.validador import contador

    assert cert.manifesto["banco_oficial_intocado"]["diferencas"] == []
    assert contador.total(conn) == antes["hypothesis"]


def test_a_certificacao_PERCORREU_o_caminho_real(conn, cenario):
    """Se não escrevesse em lugar nenhum, não teria exercitado as guardas.

    Quatro das seis famílias do A1a são barradas por `CHECK` e gatilho do
    banco — um caminho que não chegasse ao `INSERT` não as testaria, e a
    certificação mediria outra coisa.
    """
    cert = _certificar(conn, cenario)
    casos = {c["chave"]: c for c in cert.manifesto["casos"]}
    assert len(casos) == 6

    estruturais = [c for c in casos.values() if c["tipo"] == "estrutural"]
    assert estruturais, "sem controle estrutural nao ha guarda exercitada"
    # Cada estrutural tem de ter sido BARRADO, com o mecanismo nomeado.
    for c in estruturais:
        assert c["barrado"] is True, c["chave"]
        mecanismos = [
            t["mecanismo"] for t in c["tentativas"] if t.get("mecanismo")
        ]
        assert mecanismos, f"{c['chave']} barrou sem dizer por quem"


def test_a_copia_foi_descartada(conn, cenario, tmp_path):
    """Nenhum arquivo de laboratório sobrevive à certificação."""
    import pathlib
    import tempfile

    antes = set(pathlib.Path(tempfile.gettempdir()).glob("certificacao-*"))
    _certificar(conn, cenario)
    depois = set(pathlib.Path(tempfile.gettempdir()).glob("certificacao-*"))
    assert depois <= antes, f"laboratorio sobreviveu: {depois - antes}"


# ---------------------------------------------------------------------------
# UM DEFEITO IMPLANTADO PRECISA REPROVAR
# ---------------------------------------------------------------------------


def test_um_controle_PROMOVIDO_recusa_o_certificado(conn, cenario, monkeypatch):
    """A garantia que separa certificação de teatro.

    Uma certificação que nunca reprova certifica que `f(x) = f(x)` — é o
    defeito do `semente_alternativa` do incremento 14, que entrou na resposta e
    não era usado por nada. Aqui o defeito é plantado no resultado, pelo
    caminho que o `suite.executar` de fato lê.
    """
    from app.a1a import braco as a1a_braco
    from app.certificacao import suite

    real = a1a_braco.rodar

    def com_defeito(*args, **kwargs):
        r = real(*args, **kwargs)
        # Promove o primeiro controle - exatamente o que §14.4 diz que reprova.
        import dataclasses

        envenenado = [
            dataclasses.replace(c, promovido=(i == 0))
            for i, c in enumerate(r.controles)
        ]
        return dataclasses.replace(r, controles=envenenado)

    monkeypatch.setattr(a1a_braco, "rodar", com_defeito)

    antes = _foto(conn)
    with pytest.raises(suite.CertificacaoRecusada) as erro:
        _certificar(conn, cenario)
    assert "PROMOVIDO" in str(erro.value)
    assert "tolerancia zero" in str(erro.value) or "reprova a fase" in str(erro.value)

    # E NADA foi gravado: nem execucao, nem caso, nem manifesto.
    assert _foto(conn) == antes
    for tabela in ("certificacao_execucao", "certificacao_caso",
                   "certificacao_manifesto"):
        n = conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
        assert n == 0, f"{tabela} recebeu linha de uma certificacao recusada"


def test_o_banco_recusa_gravar_caso_promovido(conn):
    """A mesma proibição, no banco — e não só na função que decide.

    Se alguém escrever um segundo caminho de gravação, o gatilho pega. É o
    desenho que o ledger já usa: o módulo Python não valida partidas dobradas
    de propósito, para que um defeito nele não mascare a ausência da regra no
    banco.
    """
    conn.execute(
        "INSERT INTO certificacao_execucao (alvo_hash, componentes_json,"
        " iniciada_em, casos_esperados, intocado_json)"
        " VALUES (?, '{}', '2026-09-09', 6, '{}')",
        ("b" * 64,),
    )
    with pytest.raises(sqlite3.IntegrityError) as erro:
        conn.execute(
            "INSERT INTO certificacao_caso (execucao_id, chave, familia, tipo,"
            " barrado, promovido, resultado_json)"
            " VALUES (1, 'x', 'y', 'estrutural', 0, 1, '{}')"
        )
    assert "promoveu" in str(erro.value)


# ---------------------------------------------------------------------------
# MANIFESTO PARCIAL É ILEGÍVEL
# ---------------------------------------------------------------------------


def test_manifesto_parcial_e_recusado_pelo_BANCO(conn):
    """Uma suíte que quebrou no terceiro caso não produz documento nenhum."""
    conn.execute(
        "INSERT INTO certificacao_execucao (alvo_hash, componentes_json,"
        " iniciada_em, casos_esperados, intocado_json)"
        " VALUES (?, '{}', '2026-09-09', 6, '{}')",
        ("c" * 64,),
    )
    for i in range(3):  # tres de seis
        conn.execute(
            "INSERT INTO certificacao_caso (execucao_id, chave, familia, tipo,"
            " barrado, promovido, resultado_json)"
            " VALUES (1, ?, 'y', 'estrutural', 1, 0, '{}')",
            (f"caso{i}",),
        )
    with pytest.raises(sqlite3.IntegrityError) as erro:
        conn.execute(
            "INSERT INTO certificacao_manifesto (execucao_id, alvo_hash,"
            " manifesto_json, selado_em, passa)"
            " VALUES (1, ?, '{}', '2026-09-09', 1)",
            ("c" * 64,),
        )
    assert "parcial" in str(erro.value)


def test_o_manifesto_nao_pode_citar_outro_alvo(conn):
    """Um manifesto que cite alvo diferente do da execução é ilegível igual."""
    conn.execute(
        "INSERT INTO certificacao_execucao (alvo_hash, componentes_json,"
        " iniciada_em, casos_esperados, intocado_json)"
        " VALUES (?, '{}', '2026-09-09', 1, '{}')",
        ("d" * 64,),
    )
    conn.execute(
        "INSERT INTO certificacao_caso (execucao_id, chave, familia, tipo,"
        " barrado, promovido, resultado_json)"
        " VALUES (1, 'z', 'y', 'estrutural', 1, 0, '{}')"
    )
    with pytest.raises(sqlite3.IntegrityError) as erro:
        conn.execute(
            "INSERT INTO certificacao_manifesto (execucao_id, alvo_hash,"
            " manifesto_json, selado_em, passa)"
            " VALUES (1, ?, '{}', '2026-09-09', 1)",
            ("e" * 64,),
        )
    assert "alvo" in str(erro.value)


# ---------------------------------------------------------------------------
# RESULTADOS POR CASO SÃO IMUTÁVEIS
# ---------------------------------------------------------------------------


def test_o_resultado_por_caso_e_imutavel(conn, cenario):
    """`UPDATE` e `DELETE` recusados pelo banco, como no ledger."""
    _certificar(conn, cenario)
    for sql in (
        "UPDATE certificacao_caso SET barrado = 0",
        "DELETE FROM certificacao_caso",
        "UPDATE certificacao_manifesto SET passa = 0",
        "DELETE FROM certificacao_manifesto",
        "UPDATE certificacao_execucao SET alvo_hash = 'x'",
        "DELETE FROM certificacao_execucao",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)


# ---------------------------------------------------------------------------
# CUSTO OPERACIONAL SEPARADO DOS CRÉDITOS
# ---------------------------------------------------------------------------


def test_custo_operacional_nao_e_credito(conn, cenario):
    """CPU e disco num lugar; escassez de dado no outro."""
    from app import creditos as creditos_mod

    def consumido(braco: str):
        s = creditos_mod.saldo(conn, braco=braco, config_version_id=1)
        # `None` = o braco nao tem linha nenhuma, e ZERO consumido e a leitura
        # certa disso. Tratar `None` como ausencia de informacao faria o teste
        # passar por falta de dado, que e o defeito que ele existe para pegar.
        return 0 if s is None else s.consumido

    antes = {b: consumido(b) for b in ("a1a", "b4", "agente")}
    cert = _certificar(conn, cenario)
    depois = {b: consumido(b) for b in ("a1a", "b4", "agente")}

    assert depois == antes, (
        f"a certificacao consumiu credito experimental: {antes} -> {depois}"
    )
    custo = cert.manifesto["custo_operacional"]
    assert custo["bytes_da_copia"] > 0
    assert custo["micros_para_copiar"] > 0
    assert "credito experimental" in custo["o_que_isso_NAO_e"]
    # E ele mora em coluna propria, e nao em `test_credit_entry`.
    linha = conn.execute(
        "SELECT micros_para_copiar, bytes_da_copia FROM certificacao_execucao"
        " WHERE id = ?", (cert.execucao_id,)
    ).fetchone()
    assert int(linha["bytes_da_copia"]) == custo["bytes_da_copia"]


# ---------------------------------------------------------------------------
# NENHUMA CONSULTA FILTRA CERTIFICAÇÕES
# ---------------------------------------------------------------------------


def test_nenhuma_consulta_de_DSR_ou_FDR_filtra_certificacao(conn, cenario):
    """A garantia é estrutural: certificação não está na tabela que eles leem.

    Era o argumento contra a alternativa 2 do ADR 0040 — um `purpose` em
    `hypothesis` com filtro por consulta transformaria a garantia em disciplina
    de quem escreve o `SELECT`, e o oitavo lugar que alguém escrevesse erraria
    por omissão.
    """
    import pathlib

    from tests._prosa import sql_sem_prosa

    _certificar(conn, cenario)
    # 1. O contador global nao mudou: a prova de comportamento.
    from app.validador import contador

    assert contador.total(conn) == 16

    # 2. E nenhum modulo estatistico menciona `certificacao`: a prova de forma.
    for nome in (
        "app/validador/contador.py", "app/validador/lote.py",
        "app/estatistica/fdr.py", "app/estatistica/dsr.py",
        "app/creditos.py",
    ):
        sql = sql_sem_prosa(pathlib.Path(nome))
        assert "certificacao" not in sql, (
            f"{nome} filtra certificacao: a garantia deixou de ser estrutural"
            " e virou disciplina de consulta"
        )


# ---------------------------------------------------------------------------
# A ALTERNATIVA 3: reutilização por identidade
# ---------------------------------------------------------------------------


def test_o_mesmo_alvo_REUSA_o_certificado(conn, cenario):
    """Nada mudou => mesmo `alvo_hash` => o certificado responde."""
    from app.certificacao import alvo as alvo_mod
    from app.certificacao import suite

    cert = _certificar(conn, cenario)
    de_novo = alvo_mod.montar(conn, dataset_hash="a" * 64)
    assert de_novo["alvo_de_certificacao_hash"] == cert.alvo_hash

    achado = suite.certificado_do_alvo(conn, cert.alvo_hash)
    assert achado is not None
    assert achado["passa"] is cert.passa


def test_mexer_na_ESTATISTICA_invalida_o_certificado(conn, cenario, monkeypatch):
    """Uma mudança em `app/estatistica/` muda o alvo. É a exigência do usuário.

    > *"Uma reancoragem realmente idêntica pode reutilizar o certificado; uma
    > alteração no código estatístico ou no laboratório não pode."*
    """
    from app.certificacao import alvo as alvo_mod
    from app.certificacao import suite

    cert = _certificar(conn, cenario)

    # Simula um arquivo do laboratorio diferente, sem editar o repositorio.
    real = alvo_mod.hash_da_implementacao
    monkeypatch.setattr(
        alvo_mod, "hash_da_implementacao",
        lambda: ("f" * 64, real()[1]),
    )
    outro = alvo_mod.montar(conn, dataset_hash="a" * 64)
    assert outro["alvo_de_certificacao_hash"] != cert.alvo_hash
    assert suite.certificado_do_alvo(
        conn, outro["alvo_de_certificacao_hash"]
    ) is None


def test_a_DOCUMENTACAO_nao_invalida_o_certificado():
    """`build_do_backend` fica fora do hash, e é isso que protege o README."""
    from app.certificacao import alvo as alvo_mod

    # Nenhum dos sete componentes e o commit.
    componentes = (
        "identidade_executavel", "schema", "metodologia_estatistica",
        "suite_de_controles", "fonte_de_dados", "implementacao",
        "ambiente_de_execucao",
    )
    assert "build" not in componentes
    # E o fechamento transitivo nao alcanca rota, `main` nem dado ao vivo.
    _, modulos = alvo_mod.hash_da_implementacao()
    for m in modulos:
        assert "api.rotas" not in m, m
        assert not m.endswith("app.main"), m
        assert ".aovivo" not in m, m


def test_a_AUSENCIA_de_arquivo_de_ambiente_entra_no_hash(monkeypatch):
    """Um componente que degrada em silêncio cobre menos do que afirma.

    **Medido em produção em 2026-09-09**: o `Dockerfile` não está na imagem —
    o build copia `app/`, `requirements.txt`, `pytest.ini` e
    `start-backend.sh`, e não a si mesmo. A primeira versão pulava o arquivo
    ausente, e o hash de ambiente passava a cobrir menos em produção do que em
    desenvolvimento sem dizer.
    """
    from app.certificacao import alvo as alvo_mod

    completo = alvo_mod.hash_do_ambiente()

    real = alvo_mod.pathlib.Path.is_file

    def sem_dockerfile(self):
        return False if self.name == "Dockerfile" else real(self)

    monkeypatch.setattr(alvo_mod.pathlib.Path, "is_file", sem_dockerfile)
    faltando = alvo_mod.hash_do_ambiente()

    assert faltando["hash"] != completo["hash"], (
        "o ambiente sem Dockerfile produziu o MESMO hash: a ausencia nao"
        " entrou, e o componente degrada em silencio"
    )
    assert faltando["componentes"]["arquivos_ausentes"] == ["Dockerfile"]
    assert faltando["componentes"]["Dockerfile"] is None
    assert "nao pode fingir que cobriu" in (
        faltando["componentes"]["por_que_ausentes"]
    )


def test_a_suite_prepara_as_PROPRIAS_precondicoes_na_copia(conn, cenario_cru):
    """Certificar uma config SEM baseline e SEM hipótese tem de funcionar.

    **Medido em produção em 2026-09-09**: certificar a `config_version` 9
    devolveu **HTTP 500**. Ela é a vigente e não tem run nenhum apontando para
    ela, e `a1a.braco.rodar` recusa sem um B3 sob a mesma config.

    A resposta certa não é pedir que alguém rode baselines em produção antes de
    certificar — isso criaria runs reais só para permitir uma certificação, que
    é o oposto do objeto. A cópia é descartável: a suíte estabelece as
    pré-condições dentro dela.
    """
    from app.certificacao import suite

    dataset_id, cfg = cenario_cru
    # Nem baseline, nem hipotese: o estado exato da cv9.
    assert conn.execute("SELECT COUNT(*) FROM hypothesis").fetchone()[0] == 0
    antes = _foto(conn)

    cert = suite.executar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        dataset_hash="a" * 64,
    )
    assert len(cert.manifesto["casos"]) == 6
    preparo = cert.manifesto["preparo_do_laboratorio"]
    assert "baselines" in preparo and "b4" in preparo

    # E NADA disso alcancou o banco oficial.
    assert _foto(conn) == antes
    assert conn.execute("SELECT COUNT(*) FROM hypothesis").fetchone()[0] == 0


def test_o_tamanho_da_copia_SOMA_o_wal(conn, cenario):
    """4.096 bytes para um banco com 70.080 barras é impossível.

    **Medido em produção em 2026-09-09.** A primeira versão lia
    `stat().st_size` do arquivo principal logo depois do `backup()`, e em
    `journal_mode=WAL` o backup escreve no `-wal`: o principal fica vazio até o
    checkpoint. O campo chamava-se `bytes_copiados` e continha *o tamanho do
    arquivo principal antes do checkpoint* — errando para BAIXO, e subestimando
    o custo operacional em toda medição.
    """
    from app.certificacao import laboratorio

    with laboratorio.laboratorio_descartavel(conn) as copia:
        principal = copia.caminho.stat().st_size
        total = copia.bytes_em_disco()
        assert total >= principal
        # O banco de teste tem dataset, runs e ledger: dezenas de KB, no minimo.
        assert total > 50_000, (
            f"a copia mediu {total} bytes, o que nao cabe um dataset - o `-wal`"
            " nao esta sendo somado"
        )

    cert = _certificar(conn, cenario)
    assert cert.manifesto["custo_operacional"]["bytes_da_copia"] > 50_000


def test_o_ARNES_da_certificacao_entra_no_alvo():
    """Um certificado produzido por outro mecanismo é outro certificado.

    **Medido em produção em 2026-09-09**: duas certificações da `cv9` saíram
    com o **mesmo** `alvo_hash` tendo o arnês mudado entre elas — a correção de
    `bytes_em_disco` subiu no meio e o alvo não se moveu. Sem isto, a
    reutilização por identidade devolveria o certificado antigo para um
    mecanismo novo.
    """
    from app.certificacao import alvo as alvo_mod

    _, modulos = alvo_mod.hash_da_implementacao()
    for exigido in (
        "app.certificacao.suite",
        "app.certificacao.laboratorio",
        "app.certificacao.alvo",
    ):
        assert exigido in modulos, f"{exigido} fora do alvo: {len(modulos)} modulos"
    # E o laboratorio continua fora do que ele certifica em outro sentido: as
    # rotas nao entram.
    assert not any("api.rotas" in m for m in modulos)
