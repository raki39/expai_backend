"""Testes do incremento 2: ledger, carteira e o fluxo de eventos.

O que estes testes precisam provar nao e que o codigo do modulo faz a coisa
certa - e que **o banco nao deixa fazer a coisa errada**. Por isso varios
deles inserem SQL cru, contornando o modulo de proposito: uma garantia que
depende de o caminho certo ter sido usado nao e garantia, e convencao.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.ledger import contas
from app.ledger.livro import (
    Lancamento,
    TransacaoInvalida,
    Uso,
    abrir_run,
    carteira,
    colunas_em_ponto_flutuante,
    conferir_partidas_dobradas,
    conferir_vinculo_inferencia,
    estornar,
    fx_micro,
    reconciliar,
    registrar,
    registrar_custo_reflexao,
    saldos,
    usd_para_brl,
)

FX = 5_400_000          # 5,40 BRL/USD em micros
DATA_FX = "2026-09-01"
SEMENTE_USD = 100_000   # US$ 1.000,00 em centavos


@pytest.fixture
def run(conn: sqlite3.Connection) -> int:
    run_id, _ = abrir_run(
        conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD
    )
    return run_id


def _id_conta(conn: sqlite3.Connection, code: str) -> int:
    return contas.id_por_codigo(conn)[code]


def _saldo(conn: sqlite3.Connection, code: str) -> int:
    linha = conn.execute(
        "SELECT balance_minor FROM account_balance WHERE code = ?", (code,)
    ).fetchone()
    return int(linha["balance_minor"])


# ============================================================================
# CRITERIO 1 - partidas dobradas, sem excecao
# ============================================================================


def test_transacao_equilibrada_fecha(conn: sqlite3.Connection) -> None:
    tx = registrar(
        conn,
        kind="abertura",
        lancamentos=[
            Lancamento(contas.CAIXA_SIM, 1000),
            Lancamento(contas.SEMENTE, -1000),
        ],
    )
    assert conn.execute(
        "SELECT posted_at FROM ledger_transaction WHERE id = ?", (tx,)
    ).fetchone()["posted_at"]


def test_o_BANCO_recusa_transacao_desequilibrada(conn: sqlite3.Connection) -> None:
    """SQL cru, contornando o modulo: a regra tem de morar no banco."""
    cur = conn.execute(
        "INSERT INTO ledger_transaction (kind, occurred_at) VALUES ('abertura','x')"
    )
    tx = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO ledger_entry (transaction_id, account_id, amount_minor)"
        " VALUES (?,?,?)",
        (tx, _id_conta(conn, contas.CAIXA_SIM), 1000),
    )
    with pytest.raises(sqlite3.IntegrityError, match="partidas dobradas"):
        conn.execute(
            "UPDATE ledger_transaction SET posted_at = 'agora' WHERE id = ?", (tx,)
        )


def test_transacao_vazia_nao_fecha(conn: sqlite3.Connection) -> None:
    cur = conn.execute(
        "INSERT INTO ledger_transaction (kind, occurred_at) VALUES ('abertura','x')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="sem lancamento"):
        conn.execute(
            "UPDATE ledger_transaction SET posted_at = 'agora' WHERE id = ?",
            (int(cur.lastrowid),),
        )


def test_equilibrio_e_POR_LIVRO_e_nao_no_total(conn: sqlite3.Connection) -> None:
    """Somar BRL com USD nao significa nada.

    Estes quatro lancamentos somam zero se voce ignorar a moeda, e nenhum dos
    dois livros fecha. Tem de ser recusado.
    """
    cur = conn.execute(
        "INSERT INTO ledger_transaction (kind, occurred_at) VALUES ('reflexao','x')"
    )
    tx = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO ledger_entry (transaction_id, account_id, amount_minor)"
        " VALUES (?,?,?)",
        [
            (tx, _id_conta(conn, contas.CAIXA_SIM), -100),      # simulado: -100
            (tx, _id_conta(conn, contas.CAIXA_REAL), 100),      # real:     +100
        ],
    )
    with pytest.raises(sqlite3.IntegrityError, match="partidas dobradas"):
        conn.execute(
            "UPDATE ledger_transaction SET posted_at = 'agora' WHERE id = ?", (tx,)
        )


def test_livro_inteiro_fecha_depois_de_varios_eventos(
    conn: sqlite3.Connection, run: int
) -> None:
    for _ in range(5):
        registrar_custo_reflexao(
            conn, run_id=run, node="reflexao", kind="decisao",
            custo_usd_minor=37, fx_rate_micro=FX, fx_rate_date=DATA_FX,
        )
    assert conferir_partidas_dobradas(conn) == []
    for livro in ("simulado", "real"):
        total = sum(s["balance_minor"] for s in saldos(conn, livro))
        assert total == 0, f"livro {livro} nao fecha em zero"


def test_lancamento_de_valor_zero_e_proibido(conn: sqlite3.Connection) -> None:
    cur = conn.execute(
        "INSERT INTO ledger_transaction (kind, occurred_at) VALUES ('abertura','x')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ledger_entry (transaction_id, account_id, amount_minor)"
            " VALUES (?,?,0)",
            (int(cur.lastrowid), _id_conta(conn, contas.CAIXA_SIM)),
        )


# ============================================================================
# CRITERIO 2 - imutabilidade
# ============================================================================


def test_lancamento_nao_aceita_update_nem_delete(
    conn: sqlite3.Connection, run: int
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="imutavel"):
        conn.execute("UPDATE ledger_entry SET amount_minor = 1")
    with pytest.raises(sqlite3.IntegrityError, match="acrescimo"):
        conn.execute("DELETE FROM ledger_entry")


def test_transacao_fechada_nao_muda_mais(conn: sqlite3.Connection, run: int) -> None:
    tx = conn.execute(
        "SELECT id FROM ledger_transaction WHERE kind = 'abertura'"
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError, match="estorno"):
        conn.execute("UPDATE ledger_transaction SET memo = 'outro' WHERE id = ?", (tx,))
    with pytest.raises(sqlite3.IntegrityError, match="acrescimo"):
        conn.execute("DELETE FROM ledger_transaction WHERE id = ?", (tx,))


def test_transacao_fechada_nao_aceita_novo_lancamento(
    conn: sqlite3.Connection, run: int
) -> None:
    """Senao daria para desequilibrar o que o banco ja declarou equilibrado."""
    tx = conn.execute(
        "SELECT id FROM ledger_transaction WHERE kind = 'abertura'"
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError, match="fechada nao aceita"):
        conn.execute(
            "INSERT INTO ledger_entry (transaction_id, account_id, amount_minor)"
            " VALUES (?,?,1)",
            (tx, _id_conta(conn, contas.CAIXA_SIM)),
        )


# ============================================================================
# CRITERIO 3 - estorno
# ============================================================================


def test_estorno_leva_o_par_a_zero_e_os_dois_ficam_visiveis(
    conn: sqlite3.Connection, run: int
) -> None:
    antes = _saldo(conn, contas.CAIXA_SIM)
    tx = registrar(
        conn,
        kind="operacao",
        run_id=run,
        lancamentos=[
            Lancamento(contas.CAIXA_SIM, -5_000),
            Lancamento(contas.DESPESA_TAXA, 5_000),
        ],
    )
    assert _saldo(conn, contas.CAIXA_SIM) == antes - 5_000

    estorno = estornar(conn, tx)

    # Liquido zero...
    assert _saldo(conn, contas.CAIXA_SIM) == antes
    assert _saldo(conn, contas.DESPESA_TAXA) == 0

    # ...e os DOIS continuam no historico. O erro nao some, fica ao lado da
    # correcao. Historia que pode ser apagada nao e historia.
    ids = {
        int(l["id"])
        for l in conn.execute("SELECT id FROM ledger_transaction")
    }
    assert {tx, estorno} <= ids
    assert conn.execute(
        "SELECT reverses_transaction_id FROM ledger_transaction WHERE id = ?",
        (estorno,),
    ).fetchone()["reverses_transaction_id"] == tx


def test_estornar_duas_vezes_e_impedido_pelo_banco(
    conn: sqlite3.Connection, run: int
) -> None:
    tx = registrar(
        conn,
        kind="operacao",
        lancamentos=[
            Lancamento(contas.CAIXA_SIM, -100),
            Lancamento(contas.DESPESA_TAXA, 100),
        ],
    )
    estornar(conn, tx)
    with pytest.raises(sqlite3.IntegrityError):
        estornar(conn, tx)


def test_nao_se_estorna_transacao_aberta(conn: sqlite3.Connection) -> None:
    cur = conn.execute(
        "INSERT INTO ledger_transaction (kind, occurred_at) VALUES ('operacao','x')"
    )
    with pytest.raises(TransacaoInvalida, match="aberta"):
        estornar(conn, int(cur.lastrowid))


# ============================================================================
# CRITERIO 4 - nenhuma coluna monetaria em ponto flutuante
# ============================================================================


def test_nenhuma_coluna_e_real(conn: sqlite3.Connection) -> None:
    """Le o SCHEMA. Protege contra a coluna que alguem criar amanha."""
    assert colunas_em_ponto_flutuante(conn) == []


# ============================================================================
# CRITERIO 5 - carteira derivada, divergencia e erro
# ============================================================================


def test_saldo_recalculado_do_zero_bate_com_o_exibido(
    conn: sqlite3.Connection, run: int
) -> None:
    for i in range(3):
        registrar_custo_reflexao(
            conn, run_id=run, node=f"n{i}", kind="decisao",
            custo_usd_minor=11 + i, fx_rate_micro=FX, fx_rate_date=DATA_FX,
        )
    assert reconciliar(conn) == []


def test_transacao_ABERTA_nao_entra_no_saldo(conn: sqlite3.Connection, run: int) -> None:
    """Ela ainda nao passou pela conferencia de partidas dobradas."""
    antes = _saldo(conn, contas.CAIXA_SIM)
    cur = conn.execute(
        "INSERT INTO ledger_transaction (kind, occurred_at) VALUES ('operacao','x')"
    )
    conn.execute(
        "INSERT INTO ledger_entry (transaction_id, account_id, amount_minor)"
        " VALUES (?,?,-999999)",
        (int(cur.lastrowid), _id_conta(conn, contas.CAIXA_SIM)),
    )
    assert _saldo(conn, contas.CAIXA_SIM) == antes
    assert reconciliar(conn) == []


def test_nao_existe_coluna_de_saldo_em_lugar_nenhum(conn: sqlite3.Connection) -> None:
    """Duas fontes de verdade sobre dinheiro sao proibidas (regra 16).

    O saldo e derivado. Se um dia alguem acrescentar uma coluna de saldo, ela
    vai divergir - e este teste avisa antes.
    """
    suspeitas = []
    for tabela in ("account", "run", "ledger_transaction", "agent_event"):
        for coluna in conn.execute(f"PRAGMA table_info({tabela})"):
            nome = coluna["name"].lower()
            if "balance" in nome or nome.startswith("saldo"):
                suspeitas.append(f"{tabela}.{nome}")
    assert suspeitas == []


# ============================================================================
# CRITERIO 6 - dois livros, com a ponte de cambio no evento
# ============================================================================


def test_custo_de_reflexao_move_os_dois_livros(
    conn: sqlite3.Connection, run: int
) -> None:
    caixa_sim_antes = _saldo(conn, contas.CAIXA_SIM)
    custo_usd = 250                       # US$ 2,50
    custo_brl = usd_para_brl(custo_usd, FX)  # 1350 = R$ 13,50

    event_id, tx = registrar_custo_reflexao(
        conn, run_id=run, node="reflexao", kind="decisao",
        custo_usd_minor=custo_usd, fx_rate_micro=FX, fx_rate_date=DATA_FX,
        tier="padrao", provider="anthropic", model="claude-sonnet-5",
        uso=Uso(tokens_in=1500, tokens_out=300, tokens_cache_read=1200,
                tokens_cache_write=200),
        expectation="a regra deve superar B3", confidence_ppm=600_000,
    )

    # Livro simulado: a carteira paga, a tesouraria recebe.
    assert _saldo(conn, contas.CAIXA_SIM) == caixa_sim_antes - custo_usd
    assert _saldo(conn, contas.TESOURARIA_SIM) == custo_usd

    # Livro real: sai caixa, entra despesa.
    assert _saldo(conn, contas.CAIXA_REAL) == -custo_brl
    assert _saldo(conn, contas.DESPESA_INFERENCIA) == custo_brl

    # A taxa e a data ficam gravadas NA TRANSACAO, nao aplicadas na leitura.
    linha = conn.execute(
        "SELECT fx_rate_micro, fx_rate_date FROM ledger_transaction WHERE id = ?",
        (tx,),
    ).fetchone()
    assert linha["fx_rate_micro"] == FX
    assert linha["fx_rate_date"] == DATA_FX

    assert conferir_partidas_dobradas(conn) == []


def test_conversao_arredonda_para_cima_porque_e_custo() -> None:
    """Na duvida o experimento paga mais, nunca menos (secao 8.4.1)."""
    assert usd_para_brl(1, FX) == 6      # 5,4 -> 6
    assert usd_para_brl(200, FX) == 1080  # exato, sem sobra
    assert usd_para_brl(0, FX) == 0


def test_taxa_de_cambio_nao_passa_por_float() -> None:
    assert fx_micro(Decimal("5.40")) == 5_400_000
    with pytest.raises(ValueError, match="casas decimais"):
        fx_micro(Decimal("5.4000001"))


def test_os_dois_livros_nunca_se_misturam(conn: sqlite3.Connection, run: int) -> None:
    for s in saldos(conn, "simulado"):
        assert s["currency"] == "USD"
    for s in saldos(conn, "real"):
        assert s["currency"] == "BRL"


# ============================================================================
# CRITERIO 7 - capital semente e lancamento, nao valor magico
# ============================================================================


def test_capital_semente_entra_como_lancamento(conn: sqlite3.Connection, run: int) -> None:
    assert _saldo(conn, contas.CAIXA_SIM) == SEMENTE_USD
    # A contrapartida existe: nao ha dinheiro que apareceu do nada.
    assert _saldo(conn, contas.SEMENTE) == -SEMENTE_USD

    linhas = conn.execute(
        "SELECT e.amount_minor FROM ledger_entry e"
        " JOIN ledger_transaction t ON t.id = e.transaction_id"
        " WHERE t.kind = 'abertura'"
    ).fetchall()
    assert len(linhas) == 2
    assert sum(int(l["amount_minor"]) for l in linhas) == 0


def test_capital_semente_precisa_ser_positivo(conn: sqlite3.Connection) -> None:
    with pytest.raises(TransacaoInvalida):
        abrir_run(conn, config_version_id=1, seed_capital_usd_cents=0)


# ============================================================================
# CRITERIO 8 - agent_event tambem e imutavel
# ============================================================================


def test_agent_event_nao_aceita_update_nem_delete(
    conn: sqlite3.Connection, run: int
) -> None:
    registrar_custo_reflexao(
        conn, run_id=run, node="reflexao", kind="decisao",
        custo_usd_minor=10, fx_rate_micro=FX, fx_rate_date=DATA_FX,
    )
    with pytest.raises(sqlite3.IntegrityError, match="evento novo"):
        conn.execute("UPDATE agent_event SET expectation = 'outra'")
    with pytest.raises(sqlite3.IntegrityError, match="acrescimo"):
        conn.execute("DELETE FROM agent_event")


def test_avaliacao_posterior_e_evento_FILHO_e_nao_edicao(
    conn: sqlite3.Connection, run: int
) -> None:
    """Regra 17: expectativa e declarada antes; a avaliacao vem depois, ao lado."""
    decisao, _ = registrar_custo_reflexao(
        conn, run_id=run, node="reflexao", kind="decisao",
        custo_usd_minor=10, fx_rate_micro=FX, fx_rate_date=DATA_FX,
        expectation="deve superar B3", confidence_ppm=700_000,
    )
    avaliacao, _ = registrar_custo_reflexao(
        conn, run_id=run, node="avaliar_resultado", kind="avaliacao",
        custo_usd_minor=0, fx_rate_micro=FX, fx_rate_date=DATA_FX,
        parent_event_id=decisao,
        evaluation_json='{"realizado": {"patrimonio_final_cents": 1}}',
    )
    linha = conn.execute(
        "SELECT parent_event_id FROM agent_event WHERE id = ?", (avaliacao,)
    ).fetchone()
    assert linha["parent_event_id"] == decisao

    # A expectativa original segue intacta.
    original = conn.execute(
        "SELECT expectation, confidence_ppm FROM agent_event WHERE id = ?", (decisao,)
    ).fetchone()
    assert original["expectation"] == "deve superar B3"
    assert original["confidence_ppm"] == 700_000


def test_token_nao_informado_fica_nulo_e_nao_zero(
    conn: sqlite3.Connection, run: int
) -> None:
    """"Nao sei" e "foi zero" sao afirmacoes diferentes (secao 5.2)."""
    event_id, _ = registrar_custo_reflexao(
        conn, run_id=run, node="reflexao", kind="decisao",
        custo_usd_minor=10, fx_rate_micro=FX, fx_rate_date=DATA_FX,
        uso=Uso(tokens_in=100, tokens_out=50),  # cache nao informado
    )
    linha = conn.execute(
        "SELECT tokens_in, tokens_out, tokens_cache_read, tokens_cache_write"
        " FROM agent_event WHERE id = ?",
        (event_id,),
    ).fetchone()
    assert linha["tokens_in"] == 100
    assert linha["tokens_cache_read"] is None
    assert linha["tokens_cache_write"] is None


def test_profile_id_existe_e_e_inerte(conn: sqlite3.Connection, run: int) -> None:
    """Regra 18: presente, sempre 'neutro@1', e nenhum ramo o le."""
    event_id, _ = registrar_custo_reflexao(
        conn, run_id=run, node="reflexao", kind="decisao",
        custo_usd_minor=1, fx_rate_micro=FX, fx_rate_date=DATA_FX,
    )
    assert conn.execute(
        "SELECT profile_id FROM agent_event WHERE id = ?", (event_id,)
    ).fetchone()["profile_id"] == "neutro@1"


# ============================================================================
# CRITERIO 9 - os dois registros se referenciam
# ============================================================================


def test_evento_e_lancamento_apontam_um_para_o_outro(
    conn: sqlite3.Connection, run: int
) -> None:
    event_id, tx = registrar_custo_reflexao(
        conn, run_id=run, node="reflexao", kind="decisao",
        custo_usd_minor=99, fx_rate_micro=FX, fx_rate_date=DATA_FX,
    )
    assert conn.execute(
        "SELECT ledger_transaction_id FROM agent_event WHERE id = ?", (event_id,)
    ).fetchone()["ledger_transaction_id"] == tx
    assert conn.execute(
        "SELECT agent_event_id FROM ledger_transaction WHERE id = ?", (tx,)
    ).fetchone()["agent_event_id"] == event_id


def test_nao_ha_lancamento_orfao_nem_evento_sem_contrapartida(
    conn: sqlite3.Connection, run: int
) -> None:
    for i in range(4):
        registrar_custo_reflexao(
            conn, run_id=run, node=f"n{i}", kind="decisao",
            custo_usd_minor=5 * (i + 1), fx_rate_micro=FX, fx_rate_date=DATA_FX,
        )
    assert conferir_vinculo_inferencia(conn) == {
        "transacoes_sem_evento": [],
        "eventos_com_custo_sem_lancamento": [],
        "vinculos_assimetricos": [],
    }


def test_a_conferencia_DETECTA_transacao_de_reflexao_orfa(
    conn: sqlite3.Connection,
) -> None:
    """Uma conferencia que nunca acusa nada nao esta conferindo."""
    conn.execute(
        "INSERT INTO ledger_transaction (kind, occurred_at) VALUES ('reflexao','x')"
    )
    assert conferir_vinculo_inferencia(conn)["transacoes_sem_evento"] != []


def test_a_conferencia_DETECTA_evento_com_custo_sem_ledger(
    conn: sqlite3.Connection, run: int
) -> None:
    conn.execute(
        "INSERT INTO agent_event (run_id, occurred_at, node, kind, cost_usd_minor)"
        " VALUES (?, 'x', 'n', 'decisao', 500)",
        (run,),
    )
    assert conferir_vinculo_inferencia(conn)["eventos_com_custo_sem_lancamento"] != []


def test_evento_sem_custo_nao_exige_lancamento(conn: sqlite3.Connection, run: int) -> None:
    """Houve decisao, nao houve dinheiro. Nao e violacao."""
    registrar_custo_reflexao(
        conn, run_id=run, node="reflexao", kind="decisao",
        custo_usd_minor=0, fx_rate_micro=FX, fx_rate_date=DATA_FX,
    )
    assert conferir_vinculo_inferencia(conn)["eventos_com_custo_sem_lancamento"] == []


# ============================================================================
# Plano de contas e carteira
# ============================================================================


def test_plano_de_contas_e_idempotente(conn: sqlite3.Connection) -> None:
    antes = conn.execute("SELECT COUNT(*) AS n FROM account").fetchone()["n"]
    assert contas.garantir_plano(conn) == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM account").fetchone()["n"] == antes


def test_livro_e_moeda_andam_juntos(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO account (code, book, currency, kind, name)"
            " VALUES ('x','real','USD','ativo','conta impossivel')"
        )


def test_carteira_resume_os_dois_livros(conn: sqlite3.Connection, run: int) -> None:
    registrar_custo_reflexao(
        conn, run_id=run, node="reflexao", kind="decisao",
        custo_usd_minor=250, fx_rate_micro=FX, fx_rate_date=DATA_FX,
    )
    c = carteira(conn)
    assert c["simulado_usd"]["caixa_minor"] == SEMENTE_USD - 250
    assert c["simulado_usd"]["tesouraria_minor"] == 250
    assert c["real_brl"]["despesa_inferencia_minor"] == 1350


def test_conta_inexistente_e_recusada(conn: sqlite3.Connection) -> None:
    from app.ledger.livro import ContaDesconhecida

    with pytest.raises(ContaDesconhecida):
        registrar(
            conn,
            kind="operacao",
            lancamentos=[Lancamento("conta.que.nao.existe", 1)],
        )


def test_custo_zero_nao_cria_transacao_nenhuma(
    conn: sqlite3.Connection, run: int
) -> None:
    """Houve decisao, nao houve dinheiro.

    Uma transacao contabil de valor zero seria um evento economico que nao
    aconteceu - e ficaria pendurada em aberto para sempre, porque o banco (com
    razao) recusa fechar transacao sem lancamento.
    """
    antes = conn.execute("SELECT COUNT(*) AS n FROM ledger_transaction").fetchone()["n"]
    event_id, tx = registrar_custo_reflexao(
        conn, run_id=run, node="observar", kind="observacao",
        custo_usd_minor=0, fx_rate_micro=FX, fx_rate_date=DATA_FX,
    )
    assert tx is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM ledger_transaction"
    ).fetchone()["n"] == antes
    assert conn.execute(
        "SELECT ledger_transaction_id FROM agent_event WHERE id = ?", (event_id,)
    ).fetchone()["ledger_transaction_id"] is None


def test_nao_sobra_transacao_aberta_depois_de_operar(
    conn: sqlite3.Connection, run: int
) -> None:
    """Transacao aberta e trabalho em andamento; nenhuma deve sobreviver.

    Uma que sobrasse nao apareceria no saldo nem quebraria conferencia - ela
    simplesmente ficaria la, invisivel, ate alguem se perguntar por que a
    contagem de transacoes nao bate com a de eventos.
    """
    registrar_custo_reflexao(
        conn, run_id=run, node="n", kind="decisao",
        custo_usd_minor=42, fx_rate_micro=FX, fx_rate_date=DATA_FX,
    )
    registrar_custo_reflexao(
        conn, run_id=run, node="n", kind="observacao",
        custo_usd_minor=0, fx_rate_micro=FX, fx_rate_date=DATA_FX,
    )
    tx = registrar(
        conn, kind="operacao",
        lancamentos=[
            Lancamento(contas.CAIXA_SIM, -10), Lancamento(contas.DESPESA_TAXA, 10)
        ],
    )
    estornar(conn, tx)

    abertas = conn.execute(
        "SELECT id, kind FROM ledger_transaction WHERE posted_at IS NULL"
    ).fetchall()
    assert [dict(l) for l in abertas] == []


# ------------------------------------------------------------------ rotas


def test_rota_ledger_traz_saldo_e_conferencias(client: TestClient) -> None:
    """Saldo sem a prova de que o livro fecha e so um numero."""
    corpo = client.get("/api/ledger").json()
    c = corpo["conferencias"]
    assert c["partidas_dobradas_ok"] is True
    assert c["saldo_reconciliado_ok"] is True
    assert c["vinculos_ok"] is True
    assert c["sem_ponto_flutuante"] is True


def test_rota_abre_run_e_credita_semente(client: TestClient) -> None:
    resposta = client.post("/api/ledger/run", json={"author": "teste"})
    assert resposta.status_code == 201
    corpo = resposta.json()
    assert corpo["seed_capital_usd_cents"] == 100_000

    ledger = client.get("/api/ledger").json()
    assert ledger["carteira"]["simulado_usd"]["caixa_minor"] == 100_000
    assert ledger["run_ativo"] == corpo["run_id"]


def test_nao_abre_dois_runs_ativos(client: TestClient) -> None:
    client.post("/api/ledger/run", json={"author": "teste"})
    segunda = client.post("/api/ledger/run", json={"author": "teste"})
    assert segunda.status_code == 409


def test_run_ativo_congela_a_configuracao(client: TestClient) -> None:
    """A trava do ADR 0008 agora tem um run de verdade para exercita-la."""
    client.post("/api/ledger/run", json={"author": "teste"})
    resposta = client.post(
        "/api/config",
        json={"author": "teste", "changes": {"b3_fast": 5}},
    )
    assert resposta.status_code == 409


def test_encerrar_run_libera_a_configuracao(client: TestClient) -> None:
    run_id = client.post("/api/ledger/run", json={"author": "teste"}).json()["run_id"]
    client.post(f"/api/ledger/run/{run_id}/encerrar", json={"estado": "concluido"})
    assert client.get("/api/ledger").json()["run_ativo"] is None
    assert client.post(
        "/api/config", json={"author": "teste", "changes": {"b3_fast": 5}}
    ).status_code == 201


def test_historico_mostra_original_e_estorno(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    run_id = client.post("/api/ledger/run", json={"author": "teste"}).json()["run_id"]
    tx = registrar(
        conn, kind="operacao", run_id=run_id,
        lancamentos=[
            Lancamento(contas.CAIXA_SIM, -700), Lancamento(contas.DESPESA_TAXA, 700)
        ],
    )
    estorno = estornar(conn, tx)
    items = client.get("/api/ledger/transacoes").json()["items"]
    por_id = {i["id"]: i for i in items}
    assert tx in por_id and estorno in por_id
    assert por_id[estorno]["reverses_transaction_id"] == tx


# ============================================================================
# Isolamento entre runs
#
# Regressao de um defeito real: as contas sao globais, entao abrir um segundo
# run creditava capital semente POR CIMA do saldo do primeiro e os dois
# passavam a dividir a mesma carteira. So apareceu quando o simulador rodou
# varios runs em sequencia e o caixa CRESCEU com mais operacoes.
# ============================================================================


def test_cada_run_tem_carteira_propria(conn: sqlite3.Connection) -> None:
    from app.ledger.livro import saldo_da_conta

    run_a, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    registrar(
        conn, kind="operacao", run_id=run_a,
        lancamentos=[
            Lancamento(contas.CAIXA_SIM, -30_000),
            Lancamento(contas.DESPESA_TAXA, 30_000),
        ],
    )
    run_b, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)

    # O run B comeca com a semente inteira, sem herdar o gasto do A.
    assert saldo_da_conta(conn, contas.CAIXA_SIM, run_id=run_a) == SEMENTE_USD - 30_000
    assert saldo_da_conta(conn, contas.CAIXA_SIM, run_id=run_b) == SEMENTE_USD

    # E o livro inteiro continua somando os dois: sao perguntas diferentes,
    # e as duas tem resposta correta.
    assert saldo_da_conta(conn, contas.CAIXA_SIM) == 2 * SEMENTE_USD - 30_000


def test_carteira_de_run_novo_nao_tem_buraco(conn: sqlite3.Connection) -> None:
    """A view so traz conta com movimento; a carteira precisa das zeradas."""
    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    c = carteira(conn, run_id=run_id)
    assert c["simulado_usd"]["caixa_minor"] == SEMENTE_USD
    assert c["simulado_usd"]["posicao_btc_minor"] == 0
    assert c["real_brl"]["caixa_minor"] == 0


def test_transacao_sem_run_nao_entra_em_run_nenhum(conn: sqlite3.Connection) -> None:
    """Atribui-la a algum run seria inventar procedencia."""
    from app.ledger.livro import saldo_da_conta

    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    registrar(
        conn, kind="operacao",  # sem run_id
        lancamentos=[
            Lancamento(contas.CAIXA_SIM, -5_000),
            Lancamento(contas.DESPESA_TAXA, 5_000),
        ],
    )
    assert saldo_da_conta(conn, contas.CAIXA_SIM, run_id=run_id) == SEMENTE_USD
    assert saldo_da_conta(conn, contas.CAIXA_SIM) == SEMENTE_USD - 5_000


def test_a_rota_declara_QUAL_carteira_esta_mostrando(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Sem run ativo vem o livro inteiro, e isso tem de estar dito.

    Regressao de leitura: depois de duas comparacoes, o "caixa da carteira"
    global era a soma de sete runs. Numero legitimo, rotulo enganoso.
    """
    corpo = client.get("/api/ledger").json()
    assert corpo["escopo"] == "livro_inteiro"

    run_id = client.post("/api/ledger/run", json={"author": "t"}).json()["run_id"]
    corpo = client.get("/api/ledger").json()
    assert corpo["escopo"] == "run"
    assert corpo["runs_somados"] == 1
    assert corpo["carteira"]["simulado_usd"]["caixa_minor"] == SEMENTE_USD

    from app.ledger.livro import encerrar_run

    encerrar_run(conn, run_id, "concluido")
    abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    encerrar_run(conn, 2, "concluido")

    corpo = client.get("/api/ledger").json()
    assert corpo["escopo"] == "livro_inteiro"
    assert corpo["runs_somados"] == 2
    # O livro inteiro soma os dois: e o que "livro_inteiro" quer dizer.
    assert corpo["carteira"]["simulado_usd"]["caixa_minor"] == 2 * SEMENTE_USD


def test_na_0a_nenhum_caminho_leva_ao_estado_pausado(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """ADR 0018: o run e atomico, e a ausencia da pausa foi escolhida.

    `pausado` sobrevive no CHECK de `run.state` desde a migracao 1, e
    `ESTADOS_ATIVOS` o trata como ativo para travar a config. O ADR afirma que
    ninguem alcanca esse estado - e afirmacao de ADR que ninguem confere e
    justamente a que para de valer sem avisar. Este teste confere.
    """
    import ast
    from pathlib import Path

    from app.ledger.livro import encerrar_run

    run_id = client.post("/api/ledger/run", json={"author": "teste"}).json()["run_id"]

    with pytest.raises(TransacaoInvalida):
        encerrar_run(conn, run_id, "pausado")

    estado = conn.execute("SELECT state FROM run WHERE id = ?", (run_id,)).fetchone()[0]
    assert estado == "executando"

    # Nenhum modulo fora da migracao escreve o estado. Quem abrir a transicao
    # um dia cai aqui - e vai reler o ADR em vez de descobrir mais tarde que
    # nada no sistema sabe o que fazer com um run pausado.
    #
    # A varredura e de CONSTANTE DE STRING, via AST, e nao de texto: um
    # comentario explicando que a pausa nao existe e legitimo, ate desejavel.
    # O que nao pode e o valor entrar no codigo. (Mesma licao do teste de nome
    # de provedor, que comecou como grep e acusava a propria explicacao.)
    raiz = Path(__file__).resolve().parents[1] / "app"
    mencionam = sorted(
        caminho.relative_to(raiz).as_posix()
        for caminho in raiz.rglob("*.py")
        if any(
            isinstance(no, ast.Constant)
            and isinstance(no.value, str)
            and "pausado" in no.value
            for no in ast.walk(ast.parse(caminho.read_text(encoding="utf-8")))
        )
    )
    assert mencionam == ["migrations.py"]
