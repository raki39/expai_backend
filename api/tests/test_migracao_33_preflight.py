"""A migração 33 recria cinco tabelas, e PARA se alguma tiver ganhado linha.

O ADR 0041 autoriza recriar em vez de migrar porque elas estão vazias em
produção — conferido por leitura em 2026-09-22 e de novo em 2026-10-07. Mas
**"estava vazio quando eu olhei" não é garantia**: entre a leitura e o deploy
cabe uma chamada, e um `DROP TABLE` sobre linha que alguém gravou apaga
evidência sem volta.

Exigência do usuário em 2026-10-07: a conferência roda **imediatamente antes**
da migração, e a linha de `janela_piloto` atravessa inteira.
"""

from __future__ import annotations

import sqlite3

import pytest

from app import migrations, store

GRADE = 900_000
RECRIADAS = (
    "calibracao_observacao", "janela_revalidacao", "revalidacao_uso",
    "calibracao_versao", "calibracao_regime",
)


def _ate(conn: sqlite3.Connection, versao: int) -> None:
    """Aplica as migrações até `versao`, inclusive, pelo caminho oficial."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration ("
        " version INTEGER PRIMARY KEY, description TEXT NOT NULL,"
        " applied_at TEXT NOT NULL)"
    )
    for v, d, sql in migrations.MIGRACOES:
        if v > versao:
            break
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migration (version, description, applied_at)"
            " VALUES (?, ?, datetime('now'))", (v, d),
        )


@pytest.fixture
def em_32(tmp_path):
    caminho = tmp_path / "em32.sqlite3"
    conn = sqlite3.connect(caminho, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    _ate(conn, 32)
    assert store.versao_schema(conn) == 32
    yield conn
    conn.close()


def _contrato_e_piloto(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO bbo_contrato (contrato, timeframe, execution_reference,"
        " latency_bars, grade_ms, tolerancia_ms, criado_em)"
        " VALUES ('bbo@1','15m','abertura',1,?,2000,'x')", (GRADE,)
    )
    conn.execute(
        "INSERT INTO janela_piloto (contrato, venue, symbol, de_ms,"
        " ate_ms_exclusive, observacoes_validas, observacoes_totais,"
        " dias_corridos_x1000, fechada_por, criado_em)"
        " VALUES ('bbo@1','binance','BTCUSDT',1788538500000,1789748100000,"
        " 1222,1344,14000,'dias','2026-09-21T15:40:12Z')"
    )


def test_com_as_cinco_VAZIAS_a_migracao_33_aplica(em_32):
    """O caminho normal, e ele é o que produção vai percorrer."""
    _contrato_e_piloto(em_32)
    antes = store._impressao_da_janela_piloto(em_32)
    assert len(antes) == 1

    # `migrar` aplica TODAS as pendentes, e a 34 veio depois (OP-3). O que
    # este teste mede e a 33 ter passado pelo preflight sem parar.
    assert store.migrar(em_32) == migrations.MIGRACOES[-1][0]
    assert store.versao_schema(em_32) >= 33

    # A LINHA DO PILOTO ATRAVESSOU INTEIRA — conferida pelo CONTEÚDO, e não
    # pela contagem: recriar a tabela com outra linha faria a contagem fechar.
    assert store._impressao_da_janela_piloto(em_32) == antes
    linha = em_32.execute("SELECT * FROM janela_piloto").fetchone()
    assert linha["de_ms"] == 1788538500000
    assert linha["ate_ms_exclusive"] == 1789748100000
    assert linha["observacoes_validas"] == 1222
    assert linha["fechada_por"] == "dias"
    assert linha["criado_em"] == "2026-09-21T15:40:12Z"


@pytest.mark.parametrize("tabela", RECRIADAS)
def test_uma_linha_em_qualquer_das_cinco_PARA_a_migracao(em_32, tabela):
    """Cada uma das cinco, por parametrização: a lista não é decorativa.

    Um teste só sobre `calibracao_observacao` deixaria as outras quatro
    declaradas na constante e não conferidas — e lista fechada que ninguém
    exercita é o que este projeto chama de guarda vazia.
    """
    _contrato_e_piloto(em_32)
    _semear(em_32, tabela)
    assert em_32.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0] == 1

    with pytest.raises(store.PreflightFalhou) as e:
        store.migrar(em_32)
    # A mensagem NOMEIA cada tabela com linha, e não só a primeira: "alguma
    # delas tem dado" mandaria procurar, e a conferência existe para dizer
    # ONDE. Semear `calibracao_versao` ou `calibracao_regime` arrasta
    # `janela_revalidacao` junto, pela `NOT NULL` do schema 32 — e as duas
    # aparecem.
    assert tabela in str(e.value)
    assert "a migracao 33 recria" in str(e.value)

    # E NADA FOI DESTRUÍDO: o schema continua 32 e a linha continua lá.
    assert store.versao_schema(em_32) == 32
    assert em_32.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0] == 1
    assert len(store._impressao_da_janela_piloto(em_32)) == 1


def _semear(conn: sqlite3.Connection, tabela: str) -> None:
    """Uma linha em cada uma das cinco, no schema 32 (o ANTIGO)."""
    if tabela == "calibracao_observacao":
        conn.execute(
            "INSERT INTO calibracao_observacao (contrato, venue, symbol,"
            " t_grid_ms, lado, config_version_id, abertura, bbo_bid,"
            " bbo_bid_qty, bbo_ask, bbo_ask_qty, p_exec_previsto,"
            " e1_mili_bps, e2_mili_bps, nocional_cents, dentro_do_l1,"
            " criado_em) VALUES ('bbo@1','binance','BTCUSDT',?,'compra',1,"
            " 100,99,1,101,1,102,1,1,10,1,'x')", (GRADE,)
        )
    elif tabela == "janela_revalidacao":
        conn.execute(
            "INSERT INTO janela_revalidacao (contrato, venue, symbol, de_ms,"
            " ate_ms_exclusive, piloto_ate_ms_exclusive, selada_em)"
            " VALUES ('bbo@1','binance','BTCUSDT',100,200,100,'x')"
        )
    elif tabela == "revalidacao_uso":
        conn.execute(
            "INSERT INTO janela_revalidacao (contrato, venue, symbol, de_ms,"
            " ate_ms_exclusive, piloto_ate_ms_exclusive, selada_em)"
            " VALUES ('bbo@1','binance','BTCUSDT',100,200,100,'x')"
        )
        jid = conn.execute("SELECT id FROM janela_revalidacao").fetchone()[0]
        _versao(conn)
        cid = conn.execute("SELECT id FROM calibracao_versao").fetchone()[0]
        conn.execute(
            "INSERT INTO revalidacao_uso (janela_id, calibracao_versao_id,"
            " lb95_mili_bps, p10_mili_bps, n, passou, usada_em)"
            " VALUES (?,?,1,1,1,1,'x')", (jid, cid)
        )
    elif tabela == "calibracao_versao":
        _versao(conn)
    else:
        _versao(conn)
        cid = conn.execute("SELECT id FROM calibracao_versao").fetchone()[0]
        conn.execute(
            "INSERT INTO calibracao_regime (calibracao_versao_id, regime, n,"
            " n_efetivo_x1000, estado, motivo) VALUES (?, 'vol_baixa', 1,"
            " 1000, 'nao_calibrado', 'semeado pelo teste do preflight')",
            (cid,)
        )


def _versao(conn: sqlite3.Connection) -> None:
    """Uma `calibracao_versao` no schema 32, que exige `janela_revalidacao_id`.

    No 32 a coluna é `NOT NULL` — era o desenho em que a revalidação era UMA,
    global. A migração 33 a remove justamente porque a revalidação passou a ser
    por regime; aqui ela tem de ser satisfeita, porque o que se testa é o
    preflight sobre o schema ANTIGO.
    """
    if not conn.execute("SELECT 1 FROM janela_revalidacao").fetchone():
        conn.execute(
            "INSERT INTO janela_revalidacao (contrato, venue, symbol, de_ms,"
            " ate_ms_exclusive, piloto_ate_ms_exclusive, selada_em)"
            " VALUES ('bbo@1','binance','BTCUSDT',100,200,100,'x')"
        )
    jid = conn.execute("SELECT id FROM janela_revalidacao").fetchone()[0]
    conn.execute(
        "INSERT INTO calibracao_versao (contrato, venue, symbol,"
        " config_version_origem, piloto_de_ms, piloto_ate_ms_exclusive,"
        " janela_revalidacao_id, delta_mili_bps, n, n_efetivo_x1000,"
        " tau_x1000, sigma_e_x1000, p10_mili_bps, n_necessario, aplicado,"
        " motivo, criado_em)"
        " VALUES ('bbo@1','binance','BTCUSDT',1,1,2,?,500,1,1000,1000,1,1,1,0,"
        " 'x','x')", (jid,)
    )


def test_o_PREFLIGHT_esta_ligado_a_migracao_33_e_nao_a_outra(em_32):
    """A lista não é um `if` perdido: ela é um mapa por versão, e só a 33 está
    nele — porque só ela é destrutiva."""
    assert sorted(store.PREFLIGHT) == [33]
    assert set(RECRIADAS) <= set(store._preflight_33.__doc__ and RECRIADAS)
