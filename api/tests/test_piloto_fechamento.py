"""O fechamento da janela do piloto, e o manifesto DERIVADO dele.

Uma garantia por teste, nos nomes, porque foram nove exigidas pelo usuario em
2026-09-21 mais a decima que ele acrescentou ao aprovar a alternativa A: a
grade tem de estar completa ANTES de fechar, senao uma insercao tardia ainda
poderia mudar um manifesto que ninguem gravou.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.aovivo import bbo
from app.calibracao import manifesto, piloto

GRADE = 900_000
T0 = 1_756_000_000_000 // GRADE * GRADE
CONTRATO = "bbo@1"
INSTANTES_DA_JANELA = piloto.DIAS_MINIMOS * piloto.MS_POR_DIA // GRADE  # 1.344

SERIE = bbo.Serie(
    venue="binance", symbol="BTCUSDT",
    price_scale_exp=8, volume_scale_exp=8,
)
OFFSET_US = -2_450_000


def amostra(i: int) -> bbo.Amostra:
    t_grid = T0 + i * GRADE
    corrigido = t_grid - 500
    return bbo.Amostra(
        t_grid_ms=t_grid, disponivel=True,
        bid=60_000_00000000, bid_qty=5_00000000,
        ask=60_001_00000000, ask_qty=7_00000000, u=1,
        received_at_ms=corrigido - OFFSET_US // 1000,
        received_at_corrigido_ms=corrigido,
        sampled_at_ms=corrigido + 3, defasagem_ms=500,
        offset_us=OFFSET_US, rtt_us=12_000, incerteza_residual_us=6_000,
        relogio_medido_em_ms=corrigido - 30_000,
    )


def ausente(i: int, motivo: str = "sem_amostra_na_janela") -> bbo.Amostra:
    return bbo.Amostra(
        t_grid_ms=T0 + i * GRADE, disponivel=False, motivo=motivo
    )


def encher(conn: sqlite3.Connection, indices) -> None:
    bbo.receber(conn, SERIE, CONTRATO, [amostra(i) for i in indices])


def janela_inteira(conn: sqlite3.Connection, *, extra: int = 200) -> None:
    """A grade COMPLETA da janela, mais `extra` instantes depois dela."""
    encher(conn, range(INSTANTES_DA_JANELA + extra))


def do_registro(conn: sqlite3.Connection) -> dict:
    return manifesto.do_registro(conn, serie=SERIE, contrato=CONTRATO)


def fechar(conn: sqlite3.Connection) -> dict:
    return manifesto.fechar(conn, serie=SERIE, contrato=CONTRATO)


# ===========================================================================
# A janela: pre-declarada, derivada do registro, e nada do corpo do pedido
# ===========================================================================


def test_a_janela_e_derivada_do_registro_e_tem_os_1344_instantes(
    conn: sqlite3.Connection,
):
    """14 dias de grade de 15 min sao 1.344 instantes. O numero nao e digitado."""
    janela_inteira(conn)
    m = fechar(conn)["manifesto"]

    assert m["grade"]["esperadas"] == INSTANTES_DA_JANELA == 1_344
    assert m["janela"]["ate_ms_exclusive"] - m["janela"]["de_ms"] == (
        piloto.DIAS_MINIMOS * piloto.MS_POR_DIA
    )
    assert m["janela"]["de_ms"] == T0, "o inicio e a primeira valida, derivada"


def test_o_corpo_do_pedido_NAO_aceita_data_nem_minimo(client: TestClient):
    """`extra=forbid` recusa no MODELO, e nao numa checagem removivel."""
    for corpo in (
        {"de_ms": 1},
        {"ate_ms_exclusive": 2},
        {"observacoes_minimas": 10},
        {"dias_minimos": 1},
        {"author": "t"},  # nem autor: nao ha onde grava-lo, e nao ha escolha
    ):
        r = client.post("/api/calibracao/piloto/fechar", json=corpo)
        assert r.status_code == 422, corpo


# ===========================================================================
# Autenticacao e concorrencia
# ===========================================================================


def test_fechar_exige_a_MESMA_autenticacao_das_demais_operacoes(
    client: TestClient,
):
    """Sem token, com token errado, e com o certo - os tres exercitados.

    A dependencia nao mora nesta rota: `app/api/rotas/__init__.py` prende
    `exigir_token_de_servico` no router que agrega TODOS. Isto aqui existe
    porque "herda do agregador" e uma afirmacao sobre outro arquivo, e uma
    rota nova podia ter sido pendurada fora dele sem nada acusar.

    O 409 do caso autenticado e a prova de que o token passou: a recusa vem do
    ESTADO (sem dado, o piloto nao fecha), e nao da credencial.
    """
    # `headers={"Authorization": None}` NAO remove o cabecalho do cliente: o
    # httpx recusa `None` como valor. Tirar do proprio cliente e o unico jeito
    # de exercitar "nenhuma credencial chegou".
    token = client.headers.pop("Authorization")
    try:
        r = client.post("/api/calibracao/piloto/fechar", json={})
        assert r.status_code == 401, r.text
        assert r.json()["detail"] == "credencial ausente"

        # E o GET tambem: ler o manifesto nao e mais publico que grava-lo.
        assert client.get("/api/calibracao/piloto").status_code == 401
    finally:
        client.headers["Authorization"] = token

    r = client.post(
        "/api/calibracao/piloto/fechar", json={},
        headers={"Authorization": "Bearer nao-e-o-token"},
    )
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == "credencial invalida"

    r = client.post("/api/calibracao/piloto/fechar", json={})
    assert r.status_code == 409, "com o token certo, a recusa e de ESTADO"


def test_duas_chamadas_CONCORRENTES_dao_UMA_linha_e_o_mesmo_fechamento(
    ambiente: Path, client: TestClient, conn: sqlite3.Connection,
):
    """Duas conexoes, duas threads, um arquivo - e uma linha so.

    Nao e `TestClient` em paralelo: ele serializa, e um teste que passa por
    serializacao do cliente nao diria nada sobre a corrida. Aqui sao duas
    conexoes de verdade ao MESMO arquivo, como o threadpool do FastAPI produz
    (`conexao_do_thread`, ADR 0031) - WAL, `busy_timeout`, e o
    `UNIQUE (contrato, venue, symbol)` como quem serializa.

    Antes da correcao, a perdedora subia `IntegrityError` e a rota devolvia
    500: uma linha so, e um erro parcial para quem chamou.
    """
    from app.store import conexao_do_thread

    janela_inteira(conn)
    largada = threading.Barrier(2)

    def fechar_em_thread(_: int) -> dict:
        c = conexao_do_thread(ambiente)
        largada.wait(timeout=10)
        return manifesto.fechar(c, serie=SERIE, contrato=CONTRATO)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(fechar_em_thread, [1, 2]))

    linhas = conn.execute(
        "SELECT COUNT(*) AS n FROM janela_piloto"
    ).fetchone()["n"]
    assert linhas == 1, "duas chamadas, uma linha"

    assert {a["criado_agora"], b["criado_agora"]} == {True, False}, (
        "exatamente uma criou; a outra reconheceu que perdeu a corrida"
    )
    assert a["fechado"] is True and b["fechado"] is True
    assert a["gravado"] == b["gravado"]
    assert json.dumps(a["manifesto"], sort_keys=True) == json.dumps(
        b["manifesto"], sort_keys=True
    ), "o mesmo fechamento, byte a byte, para as duas"


# ===========================================================================
# A fonte: unicidade, imutabilidade, grade, e so dentro do intervalo
# ===========================================================================


def test_a_unicidade_por_instante_e_ESTRUTURAL(conn: sqlite3.Connection):
    """Uma linha por instante, imposta pela PRIMARY KEY - nao pelo manifesto."""
    encher(conn, [0])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO bbo_amostra (venue, symbol, t_grid_ms, contrato,"
            " grade_ms, disponivel, motivo, price_scale_exp, volume_scale_exp,"
            " recebido_em) VALUES (?,?,?,?,?,0,'x',8,8,'agora')",
            (SERIE.venue, SERIE.symbol, T0, CONTRATO, GRADE),
        )


def test_a_fonte_recusa_UPDATE_e_DELETE(conn: sqlite3.Connection):
    encher(conn, [0])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE bbo_amostra SET disponivel = 0")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM bbo_amostra")


def test_linha_FORA_da_grade_nao_entra_na_contagem_nem_no_hash(
    conn: sqlite3.Connection,
):
    """E, enquanto ela existir, o fechamento e recusado.

    O `CHECK` do banco alinha a linha a propria `grade_ms` dela; uma linha com
    grade diferente passa por ele e mesmo assim nao pertence a esta grade.
    """
    janela_inteira(conn)
    m_antes = do_registro(conn)["manifesto"]

    conn.execute(
        "INSERT INTO bbo_amostra (venue, symbol, t_grid_ms, contrato,"
        " grade_ms, disponivel, motivo, price_scale_exp, volume_scale_exp,"
        " recebido_em) VALUES (?,?,?,?,?,0,'desalinhada',8,8,'agora')",
        (SERIE.venue, SERIE.symbol, T0 + GRADE // 2, CONTRATO, GRADE // 2),
    )
    m_depois = do_registro(conn)["manifesto"]

    assert m_depois["grade"]["fora_da_grade"] == 1
    assert m_depois["grade"]["recebidas"] == m_antes["grade"]["recebidas"]
    assert m_depois["observacoes"] == m_antes["observacoes"]
    assert m_depois["hash_do_intervalo"] == m_antes["hash_do_intervalo"]
    assert m_depois["grade"]["completa"] is False

    with pytest.raises(manifesto.GradeIncompleta):
        fechar(conn)


def test_o_que_esta_FORA_do_intervalo_nao_conta(conn: sqlite3.Connection):
    """As observacoes depois do fim da janela existem e nao entram."""
    janela_inteira(conn, extra=300)
    m = fechar(conn)["manifesto"]

    total_gravado = conn.execute(
        "SELECT COUNT(*) AS n FROM bbo_amostra"
    ).fetchone()["n"]
    assert total_gravado == INSTANTES_DA_JANELA + 300
    assert m["grade"]["recebidas"] == INSTANTES_DA_JANELA
    assert m["observacoes"]["validas"] == INSTANTES_DA_JANELA


# ===========================================================================
# A grade completa: a condicao que o usuario acrescentou
# ===========================================================================


def test_fechar_e_RECUSADO_com_instante_ausente_na_janela(
    conn: sqlite3.Connection,
):
    """Faltando uma linha, uma insercao tardia ainda poderia mudar o manifesto."""
    faltando = 700
    encher(conn, [i for i in range(INSTANTES_DA_JANELA + 200) if i != faltando])

    with pytest.raises(manifesto.GradeIncompleta) as e:
        fechar(conn)
    assert "1344" in str(e.value).replace(".", "")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM janela_piloto"
    ).fetchone()["n"] == 0, "recusou e mesmo assim gravou"

    # E o buraco fechado libera - provando que a recusa era sobre a ausencia.
    encher(conn, [faltando])
    assert fechar(conn)["criado_agora"] is True


def test_as_duas_travas_sao_reconferidas_ANTES_de_gravar(
    conn: sqlite3.Connection,
):
    """Grade completa nao basta: sem as 1.000 validas, recusa."""
    # Metade indisponivel: 14 dias completos de grade, so 672 validas.
    bbo.receber(conn, SERIE, CONTRATO, [
        amostra(i) if i % 2 == 0 else ausente(i)
        for i in range(INSTANTES_DA_JANELA)
    ])
    with pytest.raises(piloto.PilotoNaoFechaAinda):
        fechar(conn)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM janela_piloto"
    ).fetchone()["n"] == 0


# ===========================================================================
# O fechamento: uma escrita, idempotente, e nada disparado junto
# ===========================================================================


def test_repetir_o_POST_devolve_o_MESMO_fechamento_sem_nova_escrita(
    conn: sqlite3.Connection,
):
    janela_inteira(conn)
    primeiro = fechar(conn)
    segundo = fechar(conn)

    assert primeiro["criado_agora"] is True
    assert segundo["criado_agora"] is False
    assert segundo["manifesto"] == primeiro["manifesto"]
    assert segundo["gravado"] == primeiro["gravado"]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM janela_piloto"
    ).fetchone()["n"] == 1


def test_o_fechamento_NAO_dispara_estimativa_nem_calibracao(
    conn: sqlite3.Connection,
):
    """Sao atos separados no ADR 0027, e juntar os dois seria decidir por junto."""
    janela_inteira(conn)
    fechar(conn)
    for tabela in ("calibracao_versao", "janela_revalidacao", "revalidacao_uso"):
        assert conn.execute(
            f"SELECT COUNT(*) AS n FROM {tabela}"
        ).fetchone()["n"] == 0, tabela


def test_divergencia_entre_o_GRAVADO_e_o_DERIVADO_e_erro_alto(
    conn: sqlite3.Connection,
):
    """Uma linha forjada nao passa por verdade so porque esta gravada."""
    janela_inteira(conn)
    prevista = piloto.derivar(conn, SERIE, CONTRATO)
    conn.execute(
        "INSERT INTO janela_piloto (contrato, venue, symbol, de_ms,"
        " ate_ms_exclusive, observacoes_validas, observacoes_totais,"
        " dias_corridos_x1000, fechada_por, criado_em)"
        " VALUES (?,?,?,?,?,?,?,?,'dias','forjada')",
        (CONTRATO, SERIE.venue, SERIE.symbol, prevista.de_ms,
         prevista.ate_ms_exclusive, prevista.observacoes_validas - 7,
         prevista.observacoes_totais, prevista.dias_corridos_x1000),
    )
    with pytest.raises(manifesto.ManifestoDivergente) as e:
        do_registro(conn)
    assert "observacoes_validas" in str(e.value)


def _forjar(conn: sqlite3.Connection, **trocas) -> None:
    """Uma linha de fechamento coerente consigo mesma, e so com ela mesma."""
    prevista = piloto.derivar(conn, SERIE, CONTRATO)
    campos = {
        "de_ms": prevista.de_ms,
        "ate_ms_exclusive": prevista.ate_ms_exclusive,
        "fechada_por": prevista.fechada_por,
    }
    campos.update(trocas)
    n = conn.execute(
        "SELECT COUNT(*) AS total, SUM(disponivel) AS validas FROM bbo_amostra"
        " WHERE venue = ? AND symbol = ? AND contrato = ?"
        "   AND t_grid_ms >= ? AND t_grid_ms < ?",
        (SERIE.venue, SERIE.symbol, CONTRATO,
         campos["de_ms"], campos["ate_ms_exclusive"]),
    ).fetchone()
    conn.execute(
        "INSERT INTO janela_piloto (contrato, venue, symbol, de_ms,"
        " ate_ms_exclusive, observacoes_validas, observacoes_totais,"
        " dias_corridos_x1000, fechada_por, criado_em)"
        " VALUES (?,?,?,?,?,?,?,?,?,'forjada')",
        (CONTRATO, SERIE.venue, SERIE.symbol, campos["de_ms"],
         campos["ate_ms_exclusive"], int(n["validas"]), int(n["total"]),
         round((campos["ate_ms_exclusive"] - campos["de_ms"]) * 1000
               / piloto.MS_POR_DIA),
         campos["fechada_por"]),
    )


def test_uma_FRONTEIRA_forjada_nao_passa_pela_propria_coerencia(
    conn: sqlite3.Connection,
):
    """O teste que so a RE-DERIVACAO pega, e a razao de ela existir.

    A linha aqui e coerente consigo mesma: as contagens conferem com o
    intervalo que ela declara. Comparar o manifesto com a linha que o definiu
    seria **tautologia** e diria que esta tudo bem. Quem discorda e
    `piloto.derivar`, que recomeca da primeira valida e das duas travas e nao
    sabe o que foi gravado.
    """
    janela_inteira(conn)
    prevista = piloto.derivar(conn, SERIE, CONTRATO)
    _forjar(conn, ate_ms_exclusive=prevista.ate_ms_exclusive + 10 * GRADE)

    with pytest.raises(manifesto.ManifestoDivergente) as e:
        do_registro(conn)
    assert "ate_ms_exclusive" in str(e.value)


def test_a_TRAVA_que_venceu_tambem_e_conferida(conn: sqlite3.Connection):
    """`fechada_por` e derivado, entao mentir nele tambem e divergencia."""
    janela_inteira(conn)
    _forjar(conn, fechada_por="observacoes")

    with pytest.raises(manifesto.ManifestoDivergente) as e:
        do_registro(conn)
    assert "fechada_por" in str(e.value)


def test_uma_falha_DEPOIS_do_insert_nao_deixa_fechamento_legivel(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
):
    """A exigencia de "fechamento parcial nunca legivel", e ela e da transacao.

    Sob a alternativa A existe **uma** escrita - a linha -, entao nao ha ordem
    de escritas a acertar. O que resta e a garantia de que essa unica escrita
    nao sobrevive a uma recusa posterior: `bloco_atomico` desfaz, e o proximo
    leitor ve "nao fechado" em vez de meio fechamento.
    """
    janela_inteira(conn)

    def explodir(*a, **k):
        raise manifesto.ManifestoDivergente("falha plantada depois do INSERT")

    monkeypatch.setattr(manifesto, "_conferir", explodir)
    with pytest.raises(manifesto.ManifestoDivergente):
        fechar(conn)

    monkeypatch.undo()
    n = conn.execute("SELECT COUNT(*) AS n FROM janela_piloto").fetchone()["n"]
    assert n == 0, "a linha tinha de ter sido desfeita com a transacao"
    assert do_registro(conn)["fechado"] is False


def test_fechada_por_e_publicado_como_TRAVA_e_nao_como_pessoa(
    conn: sqlite3.Connection,
):
    janela_inteira(conn)
    gravado = fechar(conn)["gravado"]
    assert gravado["fechada_por"] in ("dias", "observacoes")
    assert "nunca uma pessoa" in gravado["fechada_por_e"]


# ===========================================================================
# O manifesto: derivado, canonico, e igual no POST e no GET
# ===========================================================================


def test_o_manifesto_publica_o_instante_da_MILESIMA(conn: sqlite3.Connection):
    janela_inteira(conn)
    m = fechar(conn)["manifesto"]
    assert m["observacoes"]["instante_da_milesima_ms"] == T0 + 999 * GRADE


def test_o_manifesto_conta_lacunas_e_a_MAIOR(conn: sqlite3.Connection):
    """Tres instantes seguidos sem validade, e dois soltos."""
    perdidos = {100, 101, 102, 500, 900}
    bbo.receber(conn, SERIE, CONTRATO, [
        ausente(i) if i in perdidos else amostra(i)
        for i in range(INSTANTES_DA_JANELA + 50)
    ])
    m = fechar(conn)["manifesto"]

    assert m["lacunas"]["quantas"] == 3
    assert m["lacunas"]["instantes_sem_validade"] == 5
    assert m["lacunas"]["maior"]["instantes"] == 3
    assert m["lacunas"]["maior"]["de_ms"] == T0 + 100 * GRADE
    assert m["lacunas"]["maior"]["duracao_ms"] == 3 * GRADE
    assert m["observacoes"]["por_motivo"] == {"sem_amostra_na_janela": 5}


def test_o_hash_NAO_muda_entre_leituras_nem_com_dado_novo_depois(
    conn: sqlite3.Connection,
):
    """A fonte do intervalo e imutavel, entao o numero e o mesmo para sempre."""
    janela_inteira(conn)
    h = fechar(conn)["manifesto"]["hash_do_intervalo"]

    encher(conn, range(INSTANTES_DA_JANELA + 200, INSTANTES_DA_JANELA + 400))
    assert do_registro(conn)["manifesto"]["hash_do_intervalo"] == h
    assert do_registro(conn)["manifesto"]["hash_do_intervalo"] == h


def test_o_hash_MUDA_quando_o_conteudo_do_intervalo_e_outro(
    conn: sqlite3.Connection,
):
    """Sem isto, o hash seria enfeite: ele precisa separar dois conteudos."""
    bbo.receber(conn, SERIE, CONTRATO, [
        ausente(i) if i == 3 else amostra(i) for i in range(10)
    ])
    comum = dict(serie=SERIE, contrato=CONTRATO, grade_ms=GRADE)
    com_lacuna = manifesto.derivar(
        conn, de_ms=T0, ate_ms_exclusive=T0 + 5 * GRADE, **comum
    )
    sem_lacuna = manifesto.derivar(
        conn, de_ms=T0 + 5 * GRADE, ate_ms_exclusive=T0 + 10 * GRADE, **comum
    )
    assert com_lacuna["grade"]["recebidas"] == sem_lacuna["grade"]["recebidas"]
    assert (
        com_lacuna["hash_do_intervalo"] != sem_lacuna["hash_do_intervalo"]
    ), "dois conteudos diferentes com o mesmo hash"

    # E, com o MESMO cabecalho, trocar so um campo material muda o numero.
    cab = ["piloto-manifesto@1", CONTRATO, SERIE.venue, SERIE.symbol,
           str(T0), str(T0 + GRADE), str(GRADE), "1"]
    valida = [{"t_grid_ms": T0, "disponivel": 1, "motivo": None}]
    invalida = [{"t_grid_ms": T0, "disponivel": 0, "motivo": "queda"}]
    outro_motivo = [{"t_grid_ms": T0, "disponivel": 0, "motivo": "defasada"}]
    hashes = {
        manifesto._hash_do_intervalo(cab, valida),
        manifesto._hash_do_intervalo(cab, invalida),
        manifesto._hash_do_intervalo(cab, outro_motivo),
    }
    assert len(hashes) == 3, "campo material mudou e o hash nao"



def test_o_hash_declara_a_VERSAO_do_formato_e_o_que_cobre(
    conn: sqlite3.Connection,
):
    janela_inteira(conn)
    m = fechar(conn)["manifesto"]
    assert m["formato"] == manifesto.FORMATO == "piloto-manifesto@1"
    assert m["o_que_o_hash_cobre"]["por_instante"] == [
        "t_grid_ms", "disponivel", "motivo"
    ]
    assert m["o_que_o_hash_cobre"]["ordem"] == "t_grid_ms ascendente"
    assert m["o_que_o_hash_cobre"]["so_dentro_do_intervalo"] is True


def test_POST_e_GET_devolvem_o_MESMO_manifesto_byte_a_byte(
    client: TestClient, conn: sqlite3.Connection,
):
    janela_inteira(conn)
    post = client.post("/api/calibracao/piloto/fechar", json={})
    assert post.status_code == 200, post.text
    get = client.get("/api/calibracao/piloto")
    assert get.status_code == 200, get.text

    bytes_post = json.dumps(post.json()["manifesto"], sort_keys=True)
    bytes_get = json.dumps(get.json()["manifesto"], sort_keys=True)
    assert bytes_post == bytes_get
    assert post.json()["criado_agora"] is True
    assert "criado_agora" not in get.json(), (
        "o GET nao afirma criacao; quem cria e o POST"
    )


def test_o_GET_antes_do_fechamento_diz_que_e_PREVIA(
    client: TestClient, conn: sqlite3.Connection,
):
    janela_inteira(conn)
    corpo = client.get("/api/calibracao/piloto").json()
    assert corpo["fechado"] is False
    assert "NAO foi gravada" in corpo["por_que"]
    assert corpo["manifesto"]["grade"]["esperadas"] == INSTANTES_DA_JANELA

    # A previa traz os SETE campos da linha, e nao seis: `fechada_por` e a
    # unica coisa que nao se le do manifesto, e sem ela quem le teria de
    # deduzir a trava vencedora de `dias_corridos_x1000`.
    seria = corpo["seria_gravado"]
    assert set(seria) == {
        "de_ms", "ate_ms_exclusive", "observacoes_validas",
        "observacoes_totais", "dias_corridos_x1000", "fechada_por",
        "fechada_por_e",
    }
    assert seria["fechada_por"] == "dias", (
        "a janela inteira alcanca 1.000 validas muito antes dos 14 dias,"
        " entao quem vence e o CALENDARIO"
    )
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM janela_piloto"
    ).fetchone()["n"] == 0, "uma previa nao escreve nada"


def test_o_GET_sem_dado_nenhum_diz_o_motivo_e_nao_quebra(client: TestClient):
    corpo = client.get("/api/calibracao/piloto").json()
    assert corpo["fechado"] is False
    assert corpo["manifesto"] is None
    assert "valida" in corpo["por_que"]


def test_a_rota_recusa_com_409_enquanto_a_grade_tem_buraco(
    client: TestClient, conn: sqlite3.Connection,
):
    encher(conn, [i for i in range(INSTANTES_DA_JANELA + 50) if i != 42])
    r = client.post("/api/calibracao/piloto/fechar", json={})
    assert r.status_code == 409
    assert "ausentes" in r.json()["detail"]


def test_o_manifesto_nomeia_o_que_e_gravado_e_o_que_e_derivado(
    conn: sqlite3.Connection,
):
    """O vocabulario faz parte da garantia: manifesto nao e gravado."""
    janela_inteira(conn)
    m = fechar(conn)["manifesto"]
    assert "janela_piloto" in m["o_que_e_gravado"]
    assert "bbo_amostra" in m["o_que_e_derivado"]
