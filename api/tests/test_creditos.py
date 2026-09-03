"""Créditos de teste e o teto da família fechada (critérios 2, 5 e 6).

Tudo que é proibição está testado com **SQL cru**. §8.6.1 existe para que
"acabar signifique parar", e uma escassez que depende de o módulo Python ter
sido chamado corretamente não é escassez — é convenção.
"""

from __future__ import annotations

import sqlite3

import pytest

from app import creditos as creditos_mod
from app.validador import lote


def _config_version(conn: sqlite3.Connection, teto: int = 48) -> int:
    """Uma `config_version` com o teto da família que o teste quer."""
    import json

    conn.execute(
        "INSERT INTO config_version (created_at, author, payload_json,"
        " config_hash, material) VALUES ('t','teste',?,'h',1)",
        (json.dumps({"familia_max_hipoteses": teto}),),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])


def _run(conn: sqlite3.Connection, config_version_id: int) -> int:
    conn.execute(
        "INSERT INTO run (agent_id, state, config_version_id, created_at,"
        " updated_at) VALUES ('agent-0001','executando',?,'t','t')",
        (config_version_id,),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])


def _hipotese(
    conn: sqlite3.Connection, run_id: int, *, hash_: str = "h1"
) -> int:
    conn.execute(
        "INSERT INTO agent_event (run_id, occurred_at, node, kind,"
        " cost_usd_minor, cost_usd_micro)"
        " VALUES (?, 't', 'propor_regra', 'proposta', 0, 0)",
        (run_id,),
    )
    ev = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    conn.execute(
        "INSERT INTO hypothesis (run_id, agent_event_id, enunciado,"
        " agente_origem, timestamp_registro, metrica_primaria, efeito_minimo,"
        " n_minimo, sharpe_esperado_milesimos, criterio_parada,"
        " condicoes_validade_json, condicoes_falseamento_json, testavel,"
        " horizonte_barras, content_hash)"
        " VALUES (?,?,'x','transacao@0b','t','idas_e_voltas',0,10,3000,"
        "'fim_da_janela','{}',?,1,10000,?)",
        (
            run_id,
            ev,
            # Clausula de verdade, e nao um `[{}]` que passa no CHECK sem
            # refutar nada: o lote valida o pre-registro ao rejulgar.
            '[{"metrica":"idas_e_voltas","comparador":"maior_que","valor":9}]',
            hash_,
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])


# ===========================================================================
# CRITERIO 2 - familia fechada, e a hipotese 49 e RECUSADA
# ===========================================================================


def test_a_hipotese_que_estoura_a_familia_e_recusada(
    conn: sqlite3.Connection
) -> None:
    """§8.6: "número máximo fixado antes de começar, NÃO ajustável durante".

    Recusada, nunca truncada em silêncio. Truncar seria pior: o lote
    continuaria parecendo completo e a multiplicidade estaria subestimada, o
    que empurra o limiar de BY na direção de **promover**.
    """
    cv = _config_version(conn, teto=3)
    run_id = _run(conn, cv)
    for i in range(3):
        _hipotese(conn, run_id, hash_=f"h{i}")

    with pytest.raises(sqlite3.IntegrityError, match="familia fechada cheia"):
        _hipotese(conn, run_id, hash_="quarta")

    assert (
        int(conn.execute("SELECT COUNT(*) AS n FROM hypothesis").fetchone()["n"])
        == 3
    ), "nada foi truncado: a quarta simplesmente não entrou"


def test_o_teto_vem_da_config_que_abriu_o_run_e_nao_da_vigente(
    conn: sqlite3.Connection
) -> None:
    """Ler a vigente faria o teto mudar no meio do lote.

    O lote é definido pela config que o abriu. Uma config nova mais generosa
    não pode esticar um lote em andamento — é literalmente o que "não
    ajustável durante" proíbe.
    """
    apertada = _config_version(conn, teto=2)
    run_apertado = _run(conn, apertada)
    _hipotese(conn, run_apertado, hash_="a")
    _hipotese(conn, run_apertado, hash_="b")

    # Uma config nova, mais generosa, passa a ser a vigente.
    _config_version(conn, teto=99)

    # O run antigo continua preso ao teto DELE.
    with pytest.raises(sqlite3.IntegrityError, match="familia fechada cheia"):
        _hipotese(conn, run_apertado, hash_="c")


def test_familia_nova_por_config_nova_e_o_contador_global_nao_zera(
    conn: sqlite3.Connection
) -> None:
    """A brecha existe, é conhecida, e custa a comparação inteira.

    Trocar de config abre família nova — e §10.2.3 já invalida toda
    comparação que atravesse a mudança. Usar isso para comprar tentativas
    custa mais do que rende.

    E o contador global **não** reseta: ele soma tudo, e é ele que alimenta o
    DSR (§8.6). Então as tentativas extras continuam sendo descontadas.
    """
    from app.validador import contador

    primeira = _config_version(conn, teto=2)
    r1 = _run(conn, primeira)
    _hipotese(conn, r1, hash_="a")
    _hipotese(conn, r1, hash_="b")
    assert contador.total(conn) == 2

    segunda = _config_version(conn, teto=2)
    r2 = _run(conn, segunda)
    _hipotese(conn, r2, hash_="c")
    assert contador.total(conn) == 3, (
        "o contador global soma as duas famílias: é ele que o DSR desconta"
    )


# ===========================================================================
# CRITERIO 5 - creditos, com os pesos do documento
# ===========================================================================


def test_os_pesos_sao_os_da_secao_8_6_1() -> None:
    """1 in-sample · 3 reteste · 5 out-of-sample · 10 quarentena."""
    assert creditos_mod.PESOS == {
        "in_sample": 1,
        "reteste_parametro": 3,
        "out_of_sample": 5,
        "quarentena": 10,
    }


def test_o_banco_recusa_cobrar_o_peso_errado(conn: sqlite3.Connection) -> None:
    """Cobrar 1 por um out-of-sample seria vender o dado mais escasso barato.

    SQL cru: se a conferência morasse no Python, um defeito lá deixaria passar
    o preço errado — e preço errado gravado não se distingue de preço certo
    depois.
    """
    cv = _config_version(conn)
    run_id = _run(conn, cv)
    hid = _hipotese(conn, run_id)
    creditos_mod.conceder(conn, braco="agente", config_version_id=cv, creditos=60)

    for tipo, errado in (
        ("out_of_sample", 1),
        ("in_sample", 5),
        ("quarentena", 3),
        ("reteste_parametro", 10),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="peso errado"):
            conn.execute(
                "INSERT INTO test_credit_entry (braco, config_version_id,"
                " hypothesis_id, tipo, creditos, occurred_at,"
                " impacto_fdr_bps, cpu_micros, barras_reservadas)"
                " VALUES ('agente',?,?,?,?,'t',0,0,0)",
                (cv, hid, tipo, errado),
            )


def test_testar_sem_orcamento_e_recusado(conn: sqlite3.Connection) -> None:
    """Sem orçamento, testar seria testar de graça — e §8.6.1 existe por isso.

    Gatilho próprio, e não a comparação de saldo: com orçamento ausente a
    subtração daria `NULL`, `NULL` não dispara `WHEN`, e o teste passaria de
    graça exatamente no caso que a regra existe para pegar.
    """
    cv = _config_version(conn)
    run_id = _run(conn, cv)
    hid = _hipotese(conn, run_id)

    with pytest.raises(creditos_mod.OrcamentoAusente):
        creditos_mod.cobrar(
            conn,
            braco="agente",
            config_version_id=cv,
            hypothesis_id=hid,
            tipo="in_sample",
            cpu_micros=10,
            barras_reservadas=0,
            familia_max=48,
        )


def test_quando_o_orcamento_acaba_o_teste_para(
    conn: sqlite3.Connection
) -> None:
    """§8.6.1: "quando o orçamento de tentativas fica escasso, seu preço sobe".

    Na 0B o orçamento é fixo, então escasso significa **acabou**. Acabar tem
    de significar parar — senão a escassez é decorativa.
    """
    cv = _config_version(conn)
    run_id = _run(conn, cv)
    creditos_mod.conceder(conn, braco="agente", config_version_id=cv, creditos=4)

    hipoteses = [_hipotese(conn, run_id, hash_=f"h{i}") for i in range(6)]
    cobrados = 0
    for hid in hipoteses:
        try:
            cobrados += creditos_mod.cobrar(
                conn,
                braco="agente",
                config_version_id=cv,
                hypothesis_id=hid,
                tipo="in_sample",
                cpu_micros=1,
                barras_reservadas=0,
                familia_max=48,
            )
        except creditos_mod.SemCredito as erro:
            assert "orçamento esgotado" in str(erro)
            break
    assert cobrados == 4, "gastou exatamente o orçamento, e nem um a mais"

    saldo = creditos_mod.saldo(conn, braco="agente", config_version_id=cv)
    assert saldo.restante == 0 and saldo.consumido == 4


def test_o_saldo_e_derivado_e_nao_armazenado(conn: sqlite3.Connection) -> None:
    """Regra 16: duas fontes de verdade sobre quanto resta divergiriam."""
    colunas = {
        l["name"] for l in conn.execute("PRAGMA table_info(test_credit_entry)")
    }
    assert "saldo" not in colunas and "restante" not in colunas

    tabelas = {
        l["name"]
        for l in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "test_credit_balance" not in tabelas, (
        "o saldo é view; como tabela, poderia divergir do consumo"
    )


def test_consumo_de_credito_e_imutavel(conn: sqlite3.Connection) -> None:
    """Apagar consumo é devolver tentativa já gasta."""
    cv = _config_version(conn)
    run_id = _run(conn, cv)
    hid = _hipotese(conn, run_id)
    creditos_mod.conceder(conn, braco="agente", config_version_id=cv, creditos=60)
    creditos_mod.cobrar(
        conn, braco="agente", config_version_id=cv, hypothesis_id=hid,
        tipo="in_sample", cpu_micros=5, barras_reservadas=0, familia_max=48,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM test_credit_entry")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE test_credit_entry SET creditos = 0")


def test_aumentar_o_orcamento_no_meio_do_lote_e_recusado(
    conn: sqlite3.Connection
) -> None:
    """Seria comprar tentativas depois de ver resultado."""
    cv = _config_version(conn)
    creditos_mod.conceder(conn, braco="agente", config_version_id=cv, creditos=10)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE test_credit_budget SET creditos = 1000")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM test_credit_budget")


def test_conceder_duas_vezes_nao_dobra_o_orcamento(
    conn: sqlite3.Connection
) -> None:
    """Idempotente por (braço, config): senão bastaria reabrir run."""
    cv = _config_version(conn)
    for _ in range(3):
        creditos_mod.conceder(
            conn, braco="agente", config_version_id=cv, creditos=60
        )
    assert creditos_mod.saldo(
        conn, braco="agente", config_version_id=cv
    ).orcamento == 60


def test_os_dois_bracos_recebem_o_mesmo_orcamento(
    conn: sqlite3.Connection
) -> None:
    """§14.3 exige "mesmo orçamento de créditos de teste" para o B4.

    Um braço com orçamento maior mediria orçamento, não qualidade de
    hipótese — e a comparação de §14.3 é explicitamente "por crédito gasto".
    """
    cv = _config_version(conn)
    for braco in creditos_mod.BRACOS:
        creditos_mod.conceder(
            conn, braco=braco, config_version_id=cv, creditos=60
        )
    saldos = {
        b: creditos_mod.saldo(conn, braco=b, config_version_id=cv).orcamento
        for b in creditos_mod.BRACOS
    }
    assert len(set(saldos.values())) == 1, saldos


def test_reteste_da_mesma_hipotese_custa_o_triplo(
    conn: sqlite3.Connection
) -> None:
    """§8.6.1: "varredura de parâmetro é a principal fonte de sobreajuste".

    Reconhecido pelo `content_hash`, e não pela memória de quem propôs — o
    caso que a cobrança precisa pegar é justamente o de quem não sabia que
    estava repetindo.
    """
    cv = _config_version(conn)
    run_id = _run(conn, cv)
    primeira = _hipotese(conn, run_id, hash_="mesma")
    segunda = _hipotese(conn, run_id, hash_="mesma")
    outra = _hipotese(conn, run_id, hash_="diferente")

    assert creditos_mod.tipo_do_teste(
        conn, primeira, etapa="in_sample"
    ) == "in_sample"
    assert creditos_mod.tipo_do_teste(
        conn, segunda, etapa="in_sample"
    ) == "reteste_parametro"
    assert creditos_mod.tipo_do_teste(
        conn, outra, etapa="in_sample"
    ) == "in_sample"


# ===========================================================================
# CRITERIO 6 - os quatro numeros de calibracao de §8.6.1, gravados POR TESTE
# ===========================================================================


def test_os_quatro_numeros_de_calibracao_sao_gravados(
    conn: sqlite3.Connection
) -> None:
    """R43. Medidos por teste, e não estimados no fim.

    Estimar depois exigiria supor quantos testes de cada tipo houve e quanto
    cada um custou — que é exatamente o que a calibração existe para
    descobrir.
    """
    cv = _config_version(conn)
    run_id = _run(conn, cv)
    hid = _hipotese(conn, run_id)
    creditos_mod.conceder(conn, braco="agente", config_version_id=cv, creditos=60)
    creditos_mod.cobrar(
        conn, braco="agente", config_version_id=cv, hypothesis_id=hid,
        tipo="out_of_sample", cpu_micros=4_321, barras_reservadas=14_016,
        familia_max=48,
    )
    linha = conn.execute("SELECT * FROM test_credit_entry").fetchone()

    # 1. consumo por tipo
    assert linha["tipo"] == "out_of_sample" and linha["creditos"] == 5
    # 2. impacto no orçamento estatístico: 1/48 da multiplicidade
    assert linha["impacto_fdr_bps"] == 10_000 // 48
    # 3. custo computacional real, medido
    assert linha["cpu_micros"] == 4_321
    # 4. custo de oportunidade do dado reservado
    assert linha["barras_reservadas"] == 14_016


def test_a_calibracao_agrega_sem_recalibrar(conn: sqlite3.Connection) -> None:
    """§8.6.1 manda MEDIR durante a fase. Mudar o preço agora seria escolher
    a régua depois de ver o consumo."""
    cv = _config_version(conn)
    run_id = _run(conn, cv)
    creditos_mod.conceder(conn, braco="agente", config_version_id=cv, creditos=60)
    for i, tipo in enumerate(("in_sample", "in_sample", "out_of_sample")):
        creditos_mod.cobrar(
            conn, braco="agente", config_version_id=cv,
            hypothesis_id=_hipotese(conn, run_id, hash_=f"h{i}"),
            tipo=tipo, cpu_micros=100 * (i + 1), barras_reservadas=0,
            familia_max=48,
        )
    c = creditos_mod.calibracao(conn)
    por_tipo = {e["tipo"]: e for e in c["por_tipo"]}
    assert por_tipo["in_sample"]["testes"] == 2
    assert por_tipo["in_sample"]["creditos"] == 2
    assert por_tipo["out_of_sample"]["creditos"] == 5
    assert por_tipo["in_sample"]["cpu_micros_medio"] == 150
    assert c["pesos_do_documento"] == creditos_mod.PESOS


# ===========================================================================
# O lote fechado
# ===========================================================================


def test_o_lote_nao_move_estado_nenhum(conn: sqlite3.Connection) -> None:
    """Fechar o lote é PARECER sobre o conjunto, e é repetível.

    Quem move cada hipótese continua sendo `promocao`, uma a uma, com a
    evidência dela. Isso permite olhar o lote antes de decidir — e olhar duas
    vezes sem consequência.
    """
    cv = _config_version(conn)
    run_id = _run(conn, cv)
    _hipotese(conn, run_id)

    antes = int(
        conn.execute("SELECT COUNT(*) AS n FROM hypothesis_state").fetchone()["n"]
    )
    for _ in range(2):
        f = lote.fechar(
            conn,
            config_version_id=cv,
            familia_max=48,
            procedimento="BY",
            alfa_bps=1_000,
            dsr_minimo_milesimos=950,
        )
    depois = int(
        conn.execute("SELECT COUNT(*) AS n FROM hypothesis_state").fetchone()["n"]
    )
    assert antes == depois
    assert f.familia_max == 48
    assert f.fdr["procedimento"] == "BY"


def test_o_lote_usa_o_teto_da_familia_como_multiplicidade(
    conn: sqlite3.Connection
) -> None:
    """`m` é 48 mesmo com uma hipótese testada. §8.6, e é o ponto do lote."""
    cv = _config_version(conn)
    run_id = _run(conn, cv)
    _hipotese(conn, run_id)
    f = lote.fechar(
        conn, config_version_id=cv, familia_max=48, procedimento="BY",
        alfa_bps=1_000, dsr_minimo_milesimos=950,
    )
    assert f.fdr["m"] == 48
    assert f.fdr["correcao_harmonica_milesimos"] == 4_458
    assert f.fdr["limiar_efetivo_ppm"] == 22_427
