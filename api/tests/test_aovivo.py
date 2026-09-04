"""Fluxo aberto e snapshots fechados (ADR 0029, incremento 16).

As cinco garantias que o usuário exigiu antes de fechar a D50 têm teste cada
uma, e os nomes dizem qual.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.aovivo import fluxo, snapshot

MIN = 60_000
PASSO = 15 * MIN
T0 = 1_756_000_000_000 // PASSO * PASSO   # alinhado a grade

SERIE = fluxo.Serie(
    venue="binance", symbol="BTCUSDT", timeframe="15m",
    interval_ms=PASSO, price_scale_exp=8, volume_scale_exp=8,
)


def barra(i: int, *, close: int = 60_000_00000000) -> fluxo.Barra:
    return fluxo.Barra(
        open_time_ms=T0 + i * PASSO,
        open=close, high=close + 100, low=close - 100, close=close,
        volume=10, quote_volume=20, trades=3,
    )


def lote(n: int, *, de: int = 0) -> list[fluxo.Barra]:
    return [barra(i) for i in range(de, de + n)]


@pytest.fixture
def config_version_id(conn: sqlite3.Connection) -> int:
    """Uma versao de config, que e o minimo para abrir run."""
    from app.config import service as config_service

    v = config_service.versao_atual(conn)
    assert v is not None, 'o boot do app cria a versao 1'
    return int(v.id)


@pytest.fixture
def duas_hipoteses(conn: sqlite3.Connection, config_version_id: int) -> list[int]:
    """Duas hipoteses reais. A FK exige linhas de verdade.

    Inseridas por SQL cru de proposito: montar o caminho do agente inteiro
    para testar linhagem de snapshot puxaria o cerebro para dentro de um teste
    de dado ao vivo, e sao fronteiras diferentes.
    """
    conn.execute(
        "INSERT INTO run (agent_id, state, config_version_id,"
        " created_at, updated_at) VALUES ('t','concluido',?,'x','x')",
        (config_version_id,),
    )
    run_id = conn.execute("SELECT MAX(id) FROM run").fetchone()[0]
    conn.execute(
        "INSERT INTO agent_event (run_id, occurred_at, node, kind)"
        " VALUES (?, 'x', 'observar', 'observacao')",
        (run_id,),
    )
    ev = conn.execute("SELECT MAX(id) FROM agent_event").fetchone()[0]

    ids: list[int] = []
    for i in (1, 2):
        conn.execute(
            "INSERT INTO hypothesis (run_id, agent_event_id, enunciado,"
            " agente_origem, timestamp_registro, metrica_primaria,"
            " efeito_minimo, n_minimo, sharpe_esperado_milesimos,"
            " criterio_parada, condicoes_validade_json,"
            " condicoes_falseamento_json, testavel, horizonte_barras,"
            " content_hash)"
            " VALUES (?,?,?,'transacao@0c','x','excesso_sobre_b3_cents',"
            " 100, 10, 2000, 'fim_da_janela', '{}', '[\"c\"]', 1, 1000, ?)",
            (run_id, ev, f"hipotese {i} de teste", f"hash-teste-{i}"),
        )
        ids.append(int(conn.execute("SELECT MAX(id) FROM hypothesis").fetchone()[0]))
    return ids


# ============================================================== o FLUXO

def test_o_fluxo_nao_tem_coluna_de_hash(conn: sqlite3.Connection):
    """A ausência é o desenho, e não economia.

    É o que impede citar o fluxo como se fosse reproduzível. A condição do ADR
    0029 — "o fluxo aberto não finge ser um dataset fechado" — vira estrutura
    em vez de disciplina.
    """
    colunas = {c[1] for c in conn.execute("PRAGMA table_info(stream_bar)")}
    assert "sha256" not in colunas
    assert not any("hash" in c for c in colunas), colunas


def test_o_fluxo_nao_esta_em_dataset_split(conn: sqlite3.Connection):
    """Logo não aparece em `bar_por_finalidade`, a única porta do agente.

    O agente não alcança o fluxo por CONSTRUÇÃO, e não por permissão.
    """
    finalidades = {
        r[0] for r in conn.execute("SELECT DISTINCT finalidade FROM dataset_split")
    }
    assert "ao_vivo" not in finalidades
    # E a view do agente não menciona stream_bar.
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'bar_por_finalidade'"
    ).fetchone()[0]
    assert "stream_bar" not in sql


def test_a_chave_idempotente_e_a_identidade_da_barra(conn: sqlite3.Connection):
    """Retry do relé reenvia a MESMA barra, e a unicidade a recusa.

    Quatro colunas, e não um id sintético: um id que o remetente escolhe não
    identifica a barra, identifica o envio.
    """
    pk = [c for c in conn.execute("PRAGMA table_info(stream_bar)") if c[5]]
    nomes = sorted(c[1] for c in pk)
    assert nomes == ["open_time_ms", "symbol", "timeframe", "venue"]


def test_reenvio_identico_e_absorvido_em_silencio(conn: sqlite3.Connection):
    r1 = fluxo.receber(conn, SERIE, lote(5), origem="ao_vivo")
    assert (r1.aceitas, r1.repetidas) == (5, 0)

    r2 = fluxo.receber(conn, SERIE, lote(5), origem="ao_vivo")
    assert (r2.aceitas, r2.repetidas) == (0, 5), "retry normal do relé"

    total = conn.execute("SELECT COUNT(*) FROM stream_bar").fetchone()[0]
    assert total == 5


def test_divergencia_de_conteudo_e_ERRO_ALTO(conn: sqlite3.Connection):
    """A mesma barra com conteúdo diferente não escolhe versão.

    Ou a origem revisou o passado, ou algo corrompeu o dado, ou dois
    remetentes discordam — e nenhuma das três se resolve escolhendo uma.
    """
    fluxo.receber(conn, SERIE, [barra(0)], origem="ao_vivo")
    outra = fluxo.Barra(
        open_time_ms=T0, open=1, high=2, low=1, close=2,
        volume=0, quote_volume=0, trades=0,
    )
    with pytest.raises(fluxo.DivergenciaDeConteudo) as e:
        fluxo.receber(conn, SERIE, [outra], origem="ao_vivo")
    assert "append-only" in str(e.value)


def test_o_fluxo_e_append_only_no_BANCO(conn: sqlite3.Connection):
    """Gatilho, e não disciplina. É o que torna o hash de prefixo estável."""
    fluxo.receber(conn, SERIE, [barra(0)], origem="ao_vivo")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE stream_bar SET close = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM stream_bar")


def test_a_api_valida_por_conta_e_nao_confia_no_rele(conn: sqlite3.Connection):
    """O relé é código nosso, mas roda noutro lugar e fala pela rede."""
    casos = [
        (fluxo.Barra(T0 + 1, 10, 10, 10, 10, 0, 0, 0), "grade"),
        (fluxo.Barra(T0, 10, 5, 10, 10, 0, 0, 0), "high"),
        (fluxo.Barra(T0, 0, 10, 0, 10, 0, 0, 0), "positivo"),
        (fluxo.Barra(T0, 99, 10, 1, 5, 0, 0, 0), "fora de"),
        (fluxo.Barra(T0, 10, 10, 10, 10, -1, 0, 0), "negativ"),
    ]
    for b, esperado in casos:
        with pytest.raises(fluxo.BarraInvalida) as e:
            fluxo.receber(conn, SERIE, [b], origem="ao_vivo")
        assert esperado in str(e.value).lower(), (b, str(e.value))


def test_backfill_parte_da_ultima_confirmada(conn: sqlite3.Connection):
    """O relé pergunta onde paramos, em vez de supor ou reenviar tudo."""
    assert fluxo.ultima_confirmada(conn, SERIE) is None
    fluxo.receber(conn, SERIE, lote(3), origem="ao_vivo")
    assert fluxo.ultima_confirmada(conn, SERIE) == T0 + 2 * PASSO

    # Queda: as barras 3 e 4 chegam depois, marcadas como backfill.
    fluxo.receber(conn, SERIE, lote(2, de=3), origem="backfill")
    assert fluxo.ultima_confirmada(conn, SERIE) == T0 + 4 * PASSO
    origens = {
        r[0] for r in conn.execute("SELECT DISTINCT origem FROM stream_bar")
    }
    assert origens == {"ao_vivo", "backfill"}


def test_queda_do_rele_e_ATRASO_e_nao_lacuna(conn: sqlite3.Connection):
    """Kline é RECUPERÁVEL; snapshot de BBO não é.

    Se o relé cair por uma hora, as barras continuam existindo na Binance.
    Chamar isso de lacuna cedo demais declararia perdido um dado que ainda
    pode ser buscado — e no coletor (ADR 0028) é o contrário, porque não
    existe histórico de topo de livro a 1 Hz.
    """
    fluxo.receber(conn, SERIE, lote(3), origem="ao_vivo")

    # Em `agora = T0 + 7P`, a ULTIMA barra que ja FECHOU e a que abriu em
    # T0 + 6P (uma barra fecha em `open + interval`). Recebemos ate T0 + 2P,
    # entao o atraso e de QUATRO barras.
    #
    # A primeira versao deste teste esperava tres, e estava errada - o codigo
    # estava certo. Fica a conta escrita para nao errar de novo.
    agora = T0 + 7 * PASSO
    assert fluxo.atraso_ms(conn, SERIE, agora) == 4 * PASSO

    # O backfill fecha o atraso, e nenhuma lacuna foi declarada.
    fluxo.receber(conn, SERIE, lote(4, de=3), origem="backfill")
    assert fluxo.atraso_ms(conn, SERIE, agora) == 0


# ========================================================== o SNAPSHOT

def test_hash_canonico_cobre_a_IDENTIDADE_da_serie():
    """As mesmas barras de outra venue não podem hashear igual."""
    b = lote(4)
    a = snapshot.hash_canonico(SERIE, T0, T0 + 4 * PASSO, b)

    outra_venue = fluxo.Serie(**{**SERIE.__dict__, "venue": "outra"})
    outro_simbolo = fluxo.Serie(**{**SERIE.__dict__, "symbol": "ETHUSDT"})
    outra_escala = fluxo.Serie(**{**SERIE.__dict__, "price_scale_exp": 2})

    for s in (outra_venue, outro_simbolo, outra_escala):
        assert snapshot.hash_canonico(s, T0, T0 + 4 * PASSO, b) != a


def test_hash_ignora_a_ORDEM_de_leitura():
    """SQLite pode devolver em ordem diferente; o hash não pode mudar."""
    b = lote(6)
    a = snapshot.hash_canonico(SERIE, T0, T0 + 6 * PASSO, b)
    invertido = snapshot.hash_canonico(SERIE, T0, T0 + 6 * PASSO, list(reversed(b)))
    assert a == invertido


def test_hash_NAO_inclui_nada_nao_deterministico(conn: sqlite3.Connection):
    """Se `criado_em` entrasse, a IDEMPOTÊNCIA morreria.

    E é nela que as garantias 3 e 4 se apoiam: sem ela, re-materializar o
    mesmo intervalo criaria um segundo snapshot em vez de devolver o primeiro.
    """
    import inspect

    fonte = inspect.getsource(snapshot.hash_canonico)
    for proibido in ("criado_em", "_agora", "recebido_em", "id"):
        assert f'"{proibido}"' not in fonte and f"'{proibido}'" not in fonte

    fluxo.receber(conn, SERIE, lote(4), origem="ao_vivo")
    a = snapshot.materializar(
        conn, SERIE, de_ms=T0, ate_ms_exclusive=T0 + 4 * PASSO,
        finalidade="calibracao", calibration_version=1,
    )
    b = snapshot.materializar(
        conn, SERIE, de_ms=T0, ate_ms_exclusive=T0 + 4 * PASSO,
        finalidade="calibracao", calibration_version=1,
    )
    assert a == b, "re-materializar devolve o MESMO snapshot"


def test_completude_e_CONFERIDA_no_fechamento(conn: sqlite3.Connection):
    """Sem aceite explícito, snapshot incompleto não fecha."""
    fluxo.receber(conn, SERIE, [barra(0), barra(1), barra(3)], origem="ao_vivo")
    with pytest.raises(snapshot.SnapshotIncompleto) as e:
        snapshot.materializar(
            conn, SERIE, de_ms=T0, ate_ms_exclusive=T0 + 4 * PASSO,
            finalidade="calibracao", calibration_version=1,
        )
    assert e.value.manifesto.barras_presentes == 3
    assert e.value.manifesto.barras_esperadas == 4
    assert "backfill" in str(e.value), "a mensagem tem de dizer a AÇÃO"
    assert conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0] == 0


def test_lacuna_aceita_por_uma_pessoa_com_motivo_fecha(conn: sqlite3.Connection):
    """Mesmo padrão da ingestão histórica: a decisão é de uma pessoa."""
    fluxo.receber(conn, SERIE, [barra(0), barra(1), barra(3)], origem="ao_vivo")
    sid = snapshot.materializar(
        conn, SERIE, de_ms=T0, ate_ms_exclusive=T0 + 4 * PASSO,
        finalidade="calibracao", calibration_version=1,
        lacuna_aceita_por="tiago",
        lacuna_aceita_motivo="indisponibilidade da exchange em 2026-09-04",
    )
    m = snapshot.manifesto(conn, sid)
    assert m["barras_presentes"] == 3 and m["barras_esperadas"] == 4
    assert m["lacunas"] == 1 and m["maior_lacuna_barras"] == 1
    assert m["lacuna_aceita_por"] == "tiago"


def test_o_banco_tambem_recusa_incompleto_sem_aceite(conn: sqlite3.Connection):
    """A guarda não é só do Python: é `CHECK` na tabela."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO snapshot (venue,symbol,timeframe,from_ms,"
            " to_ms_exclusive,barras_esperadas,barras_presentes,lacunas,"
            " maior_lacuna_barras,sha256,finalidade,calibration_version,"
            " criado_em) VALUES ('b','B','15m',1,2,10,5,1,1,'h','calibracao',1,'x')"
        )


def test_snapshot_e_imutavel_no_banco(conn: sqlite3.Connection):
    fluxo.receber(conn, SERIE, lote(4), origem="ao_vivo")
    sid = snapshot.materializar(
        conn, SERIE, de_ms=T0, ate_ms_exclusive=T0 + 4 * PASSO,
        finalidade="calibracao", calibration_version=1,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE snapshot SET sha256 = 'outro' WHERE id = ?", (sid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM snapshot WHERE id = ?", (sid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE snapshot_bar SET close = 1")


def test_o_hash_gravado_ainda_descreve_as_barras(conn: sqlite3.Connection):
    """Um hash que ninguém recalcula é afirmação sem verificação."""
    fluxo.receber(conn, SERIE, lote(8), origem="ao_vivo")
    sid = snapshot.materializar(
        conn, SERIE, de_ms=T0, ate_ms_exclusive=T0 + 8 * PASSO,
        finalidade="revalidacao", calibration_version=1,
    )
    assert snapshot.reconferir(conn, sid) is True


# --------------------------------- garantia 4: disjunção POR LINHAGEM

def test_finalidades_disjuntas_na_MESMA_calibracao_nao_se_sobrepoem(
    conn: sqlite3.Connection,
):
    """O ADR 0027 exige piloto fora da calibração E da revalidação."""
    fluxo.receber(conn, SERIE, lote(12), origem="ao_vivo")
    snapshot.materializar(
        conn, SERIE, de_ms=T0, ate_ms_exclusive=T0 + 6 * PASSO,
        finalidade="piloto", calibration_version=1,
    )
    with pytest.raises(snapshot.SobreposicaoNaLinhagem) as e:
        snapshot.materializar(
            conn, SERIE, de_ms=T0 + 3 * PASSO, ate_ms_exclusive=T0 + 9 * PASSO,
            finalidade="calibracao", calibration_version=1,
        )
    assert "calibration_version=1" in str(e.value)


def test_a_MESMA_janela_em_calibracoes_DIFERENTES_e_permitida(
    conn: sqlite3.Connection,
):
    """A disjunção é por linhagem, e não global — a correção do usuário.

    Global impediria uma segunda calibração de usar um período que a primeira
    usou, e duas calibrações do mesmo mercado no mesmo mês são duas medições.
    """
    fluxo.receber(conn, SERIE, lote(6), origem="ao_vivo")
    a = snapshot.materializar(
        conn, SERIE, de_ms=T0, ate_ms_exclusive=T0 + 6 * PASSO,
        finalidade="calibracao", calibration_version=1,
    )
    b = snapshot.materializar(
        conn, SERIE, de_ms=T0, ate_ms_exclusive=T0 + 6 * PASSO,
        finalidade="calibracao", calibration_version=2,
    )
    assert a != b


def test_dois_forwards_de_hipoteses_DIFERENTES_podem_coexistir(
    conn: sqlite3.Connection, duas_hipoteses: list[int],
):
    """Duas candidatas observando o mesmo mercado são duas observações."""
    ids = duas_hipoteses
    fluxo.receber(conn, SERIE, lote(6), origem="ao_vivo")
    a = snapshot.materializar(
        conn, SERIE, de_ms=T0, ate_ms_exclusive=T0 + 6 * PASSO,
        finalidade="forward", hypothesis_id=ids[0],
    )
    b = snapshot.materializar(
        conn, SERIE, de_ms=T0, ate_ms_exclusive=T0 + 6 * PASSO,
        finalidade="forward", hypothesis_id=ids[1],
    )
    assert a != b


# --------------------------------- garantia 5: uso único no dono certo

def test_calibracao_pertence_a_calibration_version_e_nao_a_hipotese(
    conn: sqlite3.Connection,
):
    """A calibração do simulador é do AMBIENTE, e vale para todas as candidatas."""
    with pytest.raises(snapshot.DonoErrado) as e:
        snapshot.materializar(
            conn, SERIE, de_ms=T0, ate_ms_exclusive=T0 + PASSO,
            finalidade="calibracao", hypothesis_id=1,
        )
    assert "AMBIENTE" in str(e.value)


def test_forward_pertence_a_hipotese_e_nao_a_calibracao(conn: sqlite3.Connection):
    with pytest.raises(snapshot.DonoErrado) as e:
        snapshot.materializar(
            conn, SERIE, de_ms=T0, ate_ms_exclusive=T0 + PASSO,
            finalidade="forward", calibration_version=1,
        )
    assert "holdout" in str(e.value)


def test_UMA_revalidacao_por_calibracao(conn: sqlite3.Connection):
    """Repetir a confirmação até ela passar é o modo de falha."""
    fluxo.receber(conn, SERIE, lote(12), origem="ao_vivo")
    snapshot.materializar(
        conn, SERIE, de_ms=T0, ate_ms_exclusive=T0 + 6 * PASSO,
        finalidade="revalidacao", calibration_version=7,
    )
    with pytest.raises(sqlite3.IntegrityError):
        snapshot.materializar(
            conn, SERIE, de_ms=T0 + 6 * PASSO, ate_ms_exclusive=T0 + 12 * PASSO,
            finalidade="revalidacao", calibration_version=7,
        )


# ------------------------- garantia 1: snapshot_id só para run ao vivo

def test_run_ao_vivo_sem_snapshot_nao_existe(
    conn: sqlite3.Connection, config_version_id: int
):
    cv = config_version_id
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute(
            "INSERT INTO run (agent_id, state, config_version_id,"
            " created_at, updated_at, fonte_de_dados)"
            " VALUES ('a','pendente',?, 'x','x','ao_vivo')",
            (cv,),
        )
    assert "snapshot" in str(e.value)


def test_run_historico_NAO_aponta_para_snapshot(
    conn: sqlite3.Connection, config_version_id: int
):
    """A recíproca importa: uma das duas declarações estaria errada."""
    cv = config_version_id
    fluxo.receber(conn, SERIE, lote(4), origem="ao_vivo")
    sid = snapshot.materializar(
        conn, SERIE, de_ms=T0, ate_ms_exclusive=T0 + 4 * PASSO,
        finalidade="calibracao", calibration_version=1,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO run (agent_id, state, config_version_id,"
            " created_at, updated_at, fonte_de_dados, snapshot_id)"
            " VALUES ('a','pendente',?, 'x','x','historico',?)",
            (cv, sid),
        )


def test_o_default_de_fonte_de_dados_e_HISTORICO(conn: sqlite3.Connection):
    """É o que é VERDADE sobre todo run existente.

    `NOT NULL` sem default forçaria inventar snapshot para os runs da 0A e da
    0B — a mesma falsificação de dar requisito de regime retroativo às
    hipóteses 1 a 41.
    """
    linha = [
        c for c in conn.execute("PRAGMA table_info(run)")
        if c[1] == "fonte_de_dados"
    ][0]
    assert linha[4] == "'historico'", linha


# ----------------------------------------------------------- fronteira

def test_o_caminho_do_agente_nao_alcanca_o_aovivo():
    """Guarda de importação, como a das mãos rápidas e a do coletor."""
    import ast
    from pathlib import Path

    app = Path(__file__).resolve().parents[1] / "app"
    proibidos = ("cerebro", "maos_rapidas")
    infratores = []
    for pasta in proibidos:
        for py in (app / pasta).rglob("*.py"):
            arvore = ast.parse(py.read_text(encoding="utf-8"))
            for no in ast.walk(arvore):
                alvo = ""
                if isinstance(no, ast.ImportFrom):
                    alvo = no.module or ""
                elif isinstance(no, ast.Import):
                    alvo = " ".join(a.name for a in no.names)
                if "aovivo" in alvo:
                    infratores.append(f"{pasta}/{py.name}")
    assert not infratores, (
        f"o caminho do agente importando app/aovivo: {infratores}. "
        "Dado ao vivo e do Executor e do Validador (§8.5.1, D49)"
    )


def test_o_acesso_do_snapshot_nunca_e_do_agente(conn: sqlite3.Connection):
    """O `CHECK` é a fronteira: não existe valor que o torne do agente."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO snapshot (venue,symbol,timeframe,from_ms,"
            " to_ms_exclusive,barras_esperadas,barras_presentes,lacunas,"
            " maior_lacuna_barras,sha256,finalidade,calibration_version,"
            " acesso,criado_em)"
            " VALUES ('b','B','15m',1,2,1,1,0,0,'h','calibracao',1,'agente','x')"
        )
