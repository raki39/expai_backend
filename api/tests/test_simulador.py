"""Testes do incremento 3: simulador pessimista com fidelidade declarada.

O criterio que governa todos os outros e o 1: o simulador nunca pode ser
generoso. Um simulador otimista transforma qualquer coisa construida sobre ele
em ficcao, e o pior e que ficcao com aparencia de resultado.

Por isso o criterio 4 existe: operar ao acaso TEM de perder dinheiro aqui. Se
der lucro, o simulador esta errado e nada acima dele significa nada
(secao 8.4.1.3).
"""

from __future__ import annotations

import random
import sqlite3
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config.schema import ExperimentConfig
from app.ledger import contas
from app.ledger.livro import abrir_run, conferir_partidas_dobradas, reconciliar
from app.simulador import execucao as sim
from app.simulador.execucao import (
    CaixaInsuficiente,
    PosicaoInvalida,
    SemBarraParaExecutar,
    comprar,
    posicao_sats,
    preco_adverso,
    preco_executado,
    resumo,
    vender,
)

INTERVALO = 900_000
INICIO = 1_722_470_400_000  # 2024-08-01T00:00:00Z
ESCALA = 10**8
SEMENTE_USD = 100_000  # US$ 1.000,00


def criar_dataset(
    conn: sqlite3.Connection,
    precos: list[int],
    *,
    fracao_reservada: float = 0.2,
    fidelity_level: int = 1,
) -> int:
    """Dataset sintetico gravado direto, sem passar pela ingestao.

    `precos` em dolares inteiros. Cada barra abre e fecha no preco, com
    maxima um dolar acima e minima um dolar abaixo - suficiente para que o
    limite adverso seja distinguivel do fechamento.
    """
    corte = int(len(precos) * (1 - fracao_reservada))
    # Simbolo distinto por dataset: a janela e a mesma em todos, e o UNIQUE
    # da tabela (que esta certo) recusaria o segundo.
    n = conn.execute("SELECT COUNT(*) AS n FROM dataset").fetchone()["n"]
    cur = conn.execute(
        "INSERT INTO dataset (venue, symbol, timeframe, interval_ms, start_ms,"
        " end_ms, reserved_from_ms, bars, sha256, source, source_files_json,"
        " fetched_at, fidelity_level, price_scale_exp, volume_scale_exp)"
        " VALUES ('binance',?,'15m',?,?,?,?,?,'x','teste','[]','x',?,8,8)",
        (
            f"BTCUSDT{'' if n == 0 else n}",
            INTERVALO,
            INICIO,
            INICIO + (len(precos) - 1) * INTERVALO,
            INICIO + corte * INTERVALO,
            len(precos),
            fidelity_level,
        ),
    )
    dataset_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO bar (dataset_id, open_time_ms, open, high, low, close,"
        " volume, quote_volume, trades) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                dataset_id,
                INICIO + i * INTERVALO,
                p * ESCALA,
                (p + 1) * ESCALA,
                (p - 1) * ESCALA,
                p * ESCALA,
                10 * ESCALA,
                10 * p * ESCALA,
                100,
            )
            for i, p in enumerate(precos)
        ],
    )
    return dataset_id


@pytest.fixture
def cenario(conn: sqlite3.Connection):
    """Run aberto com capital semente e um dataset de 100 barras planas."""
    run_id, _ = abrir_run(
        conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD
    )
    dataset_id = criar_dataset(conn, [50_000] * 100)
    return run_id, dataset_id, ExperimentConfig()


def barra(i: int) -> int:
    return INICIO + i * INTERVALO


# ============================================================================
# CRITERIO 1 - nunca favoravel
# ============================================================================


def test_referencia_e_a_abertura_da_barra_de_execucao(conn, cenario) -> None:
    """ADR 0015: o preco no INSTANTE da execucao, sem olhar o resto da barra.

    A ordem entra no inicio da barra i+1 e e isso que ela encontra. Escolher
    o pior preco da barra exigiria conhecer a barra inteira - retrospectiva,
    ainda que usada contra nos.
    """
    run_id, dataset_id, cfg = cenario
    e = comprar(
        conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0),
        config=cfg, fracao_do_caixa=Decimal("0.5"),
    )
    b = conn.execute(
        "SELECT open FROM bar WHERE dataset_id = ? AND open_time_ms = ?",
        (dataset_id, e.execution_bar_ms),
    ).fetchone()
    assert e.price_ref == b["open"]


def test_executado_nunca_melhor_que_a_referencia_nos_DOIS_modelos(
    conn, cenario
) -> None:
    """O pessimismo nao depende do modelo: ele esta em `preco_executado`."""
    run_id, dataset_id, _ = cenario
    for modelo in ("abertura", "limite_adverso"):
        cfg = ExperimentConfig(execution_reference=modelo)
        c = comprar(
            conn, run_id=run_id, dataset_id=dataset_id,
            decision_bar_ms=barra(0), config=cfg, fracao_do_caixa=Decimal("0.1"),
        )
        assert c.price_exec > c.price_ref, f"{modelo}: compra nao piorou"
        v = vender(
            conn, run_id=run_id, dataset_id=dataset_id,
            decision_bar_ms=barra(5), config=cfg,
        )
        assert v.price_exec < v.price_ref, f"{modelo}: venda nao piorou"


def test_modelo_limite_adverso_continua_reproduzivel(conn, cenario) -> None:
    """O campo e versionado: uma comparacao antiga precisa poder ser refeita
    sob o modelo em que foi feita."""
    run_id, dataset_id, _ = cenario
    cfg = ExperimentConfig(execution_reference="limite_adverso")
    e = comprar(
        conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0),
        config=cfg, fracao_do_caixa=Decimal("0.5"),
    )
    b = conn.execute(
        "SELECT high FROM bar WHERE dataset_id = ? AND open_time_ms = ?",
        (dataset_id, e.execution_bar_ms),
    ).fetchone()
    assert e.price_ref == b["high"]


def test_o_modelo_de_execucao_e_material(conn) -> None:
    """Trocar o modelo tem de mudar o config_hash.

    Se nao mudasse, dois runs reportariam a mesma identidade de configuracao
    com semanticas de execucao diferentes - e a comparacao entre eles mentiria
    sem nada acusar.
    """
    a = ExperimentConfig(execution_reference="abertura")
    b = ExperimentConfig(execution_reference="limite_adverso")
    assert a.config_hash() != b.config_hash()


def test_o_BANCO_recusa_execucao_generosa(conn, cenario) -> None:
    """SQL cru: a regra tem de estar no schema, nao so no modulo."""
    run_id, dataset_id, _ = cenario
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO execution (run_id, dataset_id, decision_bar_ms,"
            " execution_bar_ms, side, quantity_sats, price_ref, price_exec,"
            " notional_ref_cents, fee_cents, spread_cents, slippage_cents,"
            " penalty_cents, fidelity_level, ledger_transaction_id)"
            # compra executada ABAIXO da referencia: barganha que nao existiu
            " VALUES (?,?,?,?,'compra',1,1000,900,1,0,0,0,0,1,1)",
            (run_id, dataset_id, barra(0), barra(1)),
        )


def test_preco_adverso_escolhe_o_pior_lado() -> None:
    from app.dataset.loader import BarraCarregada

    b = BarraCarregada(0, 0, 100, 120, 80, 110, 0, 0, 0)
    assert preco_adverso(b, "compra") == 120
    assert preco_adverso(b, "venda") == 80


def test_pessimismo_nao_depende_de_ponto_flutuante() -> None:
    cfg = ExperimentConfig()
    ref = 100_000 * ESCALA
    assert preco_executado(ref, "compra", cfg) == 100_035 * ESCALA
    assert preco_executado(ref, "venda", cfg) == 99_965 * ESCALA


# ============================================================================
# CRITERIO 2 - latencia real
# ============================================================================


def test_execucao_nunca_na_barra_da_decisao(conn, cenario) -> None:
    run_id, dataset_id, cfg = cenario
    e = comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(10), config=cfg)
    assert e.execution_bar_ms > e.decision_bar_ms
    assert e.execution_bar_ms == barra(11)  # latency_bars = 1


def test_latencia_maior_anda_mais_barras(conn, cenario) -> None:
    run_id, dataset_id, _ = cenario
    cfg = ExperimentConfig(latency_bars=3)
    e = comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(10), config=cfg)
    assert e.execution_bar_ms == barra(13)


def test_o_BANCO_recusa_execucao_na_mesma_barra(conn, cenario) -> None:
    run_id, dataset_id, _ = cenario
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO execution (run_id, dataset_id, decision_bar_ms,"
            " execution_bar_ms, side, quantity_sats, price_ref, price_exec,"
            " notional_ref_cents, fee_cents, spread_cents, slippage_cents,"
            " penalty_cents, fidelity_level, ledger_transaction_id)"
            " VALUES (?,?,?,?,'compra',1,1000,1000,1,0,0,0,0,1,1)",
            (run_id, dataset_id, barra(0), barra(0)),
        )


def test_latencia_alem_do_fim_da_janela_falha(conn, cenario) -> None:
    """Melhor recusar que executar numa barra reservada ou inexistente."""
    run_id, dataset_id, cfg = cenario
    # A barra 79 e a ultima disponivel (100 barras, 20% reservado).
    with pytest.raises(SemBarraParaExecutar):
        comprar(conn, run_id=run_id, dataset_id=dataset_id,
                decision_bar_ms=barra(79), config=cfg)


def test_execucao_nunca_alcanca_o_periodo_reservado(conn, cenario) -> None:
    """A latencia nao pode ser uma porta dos fundos para o holdout."""
    run_id, dataset_id, cfg = cenario
    reservado = conn.execute(
        "SELECT reserved_from_ms FROM dataset WHERE id = ?", (dataset_id,)
    ).fetchone()["reserved_from_ms"]

    comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(70), config=cfg)
    vender(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(75), config=cfg)

    maximo = conn.execute(
        "SELECT MAX(execution_bar_ms) AS m FROM execution"
    ).fetchone()["m"]
    assert maximo < reservado


# ============================================================================
# CRITERIO 3 - custos decompostos
# ============================================================================


def test_cada_custo_tem_lancamento_proprio(conn, cenario) -> None:
    """Um campo "custo" agregado nao passa."""
    run_id, dataset_id, cfg = cenario
    e = comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0), config=cfg)

    linhas = conn.execute(
        "SELECT a.code AS code, e.amount_minor AS valor"
        " FROM ledger_entry e JOIN account a ON a.id = e.account_id"
        " WHERE e.transaction_id = ?",
        (e.ledger_transaction_id,),
    ).fetchall()
    por_conta = {l["code"]: int(l["valor"]) for l in linhas}

    assert por_conta[contas.DESPESA_TAXA] == e.fee_cents
    assert por_conta[contas.DESPESA_SPREAD] == e.spread_cents
    assert por_conta[contas.DESPESA_SLIPPAGE] == e.slippage_cents
    assert por_conta[contas.DESPESA_PENALIDADE] == e.penalty_cents

    # Quatro contas distintas, nao uma soma.
    assert len({contas.DESPESA_TAXA, contas.DESPESA_SPREAD,
                contas.DESPESA_SLIPPAGE, contas.DESPESA_PENALIDADE}
               & set(por_conta)) == 4


def test_os_custos_somam_o_total(conn, cenario) -> None:
    run_id, dataset_id, cfg = cenario
    e = comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0), config=cfg)
    assert e.custo_total_cents == (
        e.fee_cents + e.spread_cents + e.slippage_cents + e.penalty_cents
    )
    r = resumo(conn, run_id)["custos_cents"]
    assert r["total"] == r["taxa"] + r["spread"] + r["slippage"] + r["penalidade"]
    assert r["total"] == e.custo_total_cents


def test_execucao_mantem_o_livro_equilibrado(conn, cenario) -> None:
    run_id, dataset_id, cfg = cenario
    comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0), config=cfg)
    vender(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(10), config=cfg)
    assert conferir_partidas_dobradas(conn) == []
    assert reconciliar(conn) == []


def test_posicao_zera_de_verdade_na_venda(conn, cenario) -> None:
    run_id, dataset_id, cfg = cenario
    comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0), config=cfg)
    assert posicao_sats(conn, run_id) > 0
    vender(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(10), config=cfg)
    assert posicao_sats(conn, run_id) == 0
    # A conta de posicao volta a zero: nao sobra custo escondido no ativo.
    assert sim.custo_da_posicao_cents(conn, run_id) == 0


# ============================================================================
# CRITERIO 4 - sanidade: operar ao acaso PERDE dinheiro
# ============================================================================


def _girar(conn, run_id, dataset_id, cfg, rodadas: int, seed: int) -> int:
    """Compra e vende ao acaso. Devolve o caixa final."""
    rng = random.Random(seed)
    i = 0
    for _ in range(rodadas):
        if i + 8 >= 78:
            break
        comprar(conn, run_id=run_id, dataset_id=dataset_id,
                decision_bar_ms=barra(i), config=cfg, fracao_do_caixa=Decimal("0.9"))
        i += rng.randint(1, 3)
        vender(conn, run_id=run_id, dataset_id=dataset_id,
               decision_bar_ms=barra(i), config=cfg)
        i += rng.randint(1, 3)
    return sim.caixa_cents(conn, run_id)


def test_operar_ao_acaso_perde_dinheiro(conn) -> None:
    """Se der lucro aqui, o simulador esta errado (secao 8.4.1.3).

    Nota de escopo: observacao de sanidade, NAO o criterio A2 do Portao A.
    Portao A e 0B; a Fase 0A nao tem portoes (secao 14.4).
    """
    cfg = ExperimentConfig()
    resultados = []
    for seed in range(5):
        run_id, _ = abrir_run(conn, config_version_id=1,
                              seed_capital_usd_cents=SEMENTE_USD)
        # Preco em passeio aleatorio: sem tendencia para o acaso explorar.
        rng = random.Random(1000 + seed)
        precos = [50_000]
        for _ in range(99):
            precos.append(max(1000, precos[-1] + rng.randint(-50, 50)))
        dataset_id = criar_dataset(conn, precos)
        resultados.append(_girar(conn, run_id, dataset_id, cfg, 6, seed))

    media = sum(resultados) / len(resultados)
    assert media < SEMENTE_USD, (
        f"operar ao acaso deu caixa medio {media} contra semente "
        f"{SEMENTE_USD}: o simulador esta generoso"
    )


def test_a_perda_cresce_com_o_numero_de_operacoes(conn) -> None:
    """Mais giro, mais custo. E a assinatura de um simulador honesto."""
    cfg = ExperimentConfig()
    caixas = {}
    for rodadas in (1, 3, 6):
        run_id, _ = abrir_run(conn, config_version_id=1,
                              seed_capital_usd_cents=SEMENTE_USD)
        dataset_id = criar_dataset(conn, [50_000] * 100)  # preco plano
        caixas[rodadas] = _girar(conn, run_id, dataset_id, cfg, rodadas, seed=7)

    assert caixas[1] > caixas[3] > caixas[6], (
        f"com preco plano a perda tem de crescer com o giro: {caixas}"
    )


def test_ida_e_volta_imediata_perde_mesmo_com_preco_parado(conn, cenario) -> None:
    """O caso mais limpo: nada mudou no mercado, e mesmo assim custou."""
    run_id, dataset_id, cfg = cenario
    antes = sim.caixa_cents(conn, run_id)
    comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0), config=cfg)
    vender(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(2), config=cfg)
    assert sim.caixa_cents(conn, run_id) < antes


# ============================================================================
# CRITERIO 5 - fidelidade declarada e propagada
# ============================================================================


def test_fidelidade_gravada_em_cada_execucao(conn, cenario) -> None:
    run_id, dataset_id, cfg = cenario
    e = comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0), config=cfg)
    assert e.fidelity_level == 1
    assert conn.execute(
        "SELECT fidelity_level FROM execution WHERE id = ?", (e.id,)
    ).fetchone()["fidelity_level"] == 1


def test_fidelidade_no_agregado(conn, cenario) -> None:
    run_id, dataset_id, cfg = cenario
    comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0), config=cfg)
    r = resumo(conn, run_id)
    assert r["fidelity_level"] == 1
    assert r["fidelidade_homogenea"] is True


def test_agregado_recusa_declarar_fidelidade_mista(conn, cenario) -> None:
    """Escolher uma das duas seria afirmar mais do que se sabe."""
    run_id, dataset_id, cfg = cenario
    comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0), config=cfg)
    conn.execute(
        "INSERT INTO execution (run_id, dataset_id, decision_bar_ms,"
        " execution_bar_ms, side, quantity_sats, price_ref, price_exec,"
        " notional_ref_cents, fee_cents, spread_cents, slippage_cents,"
        " penalty_cents, fidelity_level, ledger_transaction_id)"
        " VALUES (?,?,?,?,'compra',1,1000,1000,1,0,0,0,0,2,1)",
        (run_id, dataset_id, barra(0), barra(1)),
    )
    r = resumo(conn, run_id)
    assert r["fidelidade_homogenea"] is False
    assert r["fidelity_level"] is None


# ============================================================================
# CRITERIO 6 - proibido afirmar fidelidade de book
# ============================================================================


def test_agregado_declara_o_que_nao_pode_ser_afirmado(conn, cenario) -> None:
    run_id, _, _ = cenario
    texto = resumo(conn, run_id)["condicoes_validade"].lower()
    assert "fidelidade 1" in texto
    assert "book" in texto and "fila" in texto and "maker" in texto
    assert "nenhuma conclusao estatistica" in texto


def test_nao_existe_metrica_de_book_em_lugar_nenhum(conn, cenario) -> None:
    """Nem coluna, nem campo do resumo. O caminho para violar isso e sempre
    comecar a estimar "so para ter uma ideia" - entao nao ha onde guardar."""
    proibidos = ("bid", "ask", "queue", "fila", "maker_fill", "book_depth",
                 "spread_real", "fill_prob")
    for coluna in conn.execute("PRAGMA table_info(execution)"):
        assert not any(p in coluna["name"].lower() for p in proibidos)

    run_id, _, _ = cenario
    chaves = " ".join(resumo(conn, run_id)["custos_cents"].keys()).lower()
    assert not any(p in chaves for p in proibidos)


# ============================================================================
# Long/flat e guardas de posicao
# ============================================================================


def test_nao_acumula_posicao(conn, cenario) -> None:
    run_id, dataset_id, cfg = cenario
    comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0), config=cfg)
    with pytest.raises(PosicaoInvalida, match="long/flat"):
        comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(5), config=cfg)


def test_nao_vende_a_descoberto(conn, cenario) -> None:
    run_id, dataset_id, cfg = cenario
    with pytest.raises(PosicaoInvalida, match="descoberto"):
        vender(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0), config=cfg)


def test_caixa_insuficiente_recusa(conn) -> None:
    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=1)
    dataset_id = criar_dataset(conn, [50_000] * 100)
    with pytest.raises(CaixaInsuficiente):
        comprar(conn, run_id=run_id, dataset_id=dataset_id,
                decision_bar_ms=barra(0), config=ExperimentConfig())


def test_compra_nunca_estoura_o_caixa(conn, cenario) -> None:
    run_id, dataset_id, cfg = cenario
    comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0),
            config=cfg, fracao_do_caixa=Decimal("1.0"))
    assert sim.caixa_cents(conn, run_id) >= 0, "caixa negativo significa comprar fiado"


def test_execucao_e_imutavel(conn, cenario) -> None:
    run_id, dataset_id, cfg = cenario
    e = comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0), config=cfg)
    with pytest.raises(sqlite3.IntegrityError, match="imutavel"):
        conn.execute("UPDATE execution SET price_exec = 1 WHERE id = ?", (e.id,))
    with pytest.raises(sqlite3.IntegrityError, match="acrescimo"):
        conn.execute("DELETE FROM execution WHERE id = ?", (e.id,))


# ============================================================================
# Regra 3 - fronteira de importacao
# ============================================================================


def test_maos_rapidas_nao_importam_cerebro_lento() -> None:
    """A separacao da secao 3.2 e verificavel, nao convencao.

    Le o FONTE, e nao os modulos ja carregados: import dentro de funcao
    escaparia de qualquer inspecao em tempo de execucao.
    """
    import pathlib

    proibidos = ("langgraph", "anthropic", "openai", "langchain")
    raiz = pathlib.Path(__file__).resolve().parents[1] / "app"
    for pacote in ("simulador", "dataset", "ledger"):
        for arquivo in (raiz / pacote).rglob("*.py"):
            fonte = arquivo.read_text(encoding="utf-8").lower()
            for linha in fonte.splitlines():
                despido = linha.strip()
                if despido.startswith(("import ", "from ")):
                    assert not any(p in despido for p in proibidos), (
                        f"{arquivo.relative_to(raiz)}: {despido}"
                    )


# ============================================================================
# Rotas
# ============================================================================


def test_rota_simulador_sem_run(client: TestClient) -> None:
    corpo = client.get("/api/simulador").json()
    assert corpo["run_ativo"] is None
    # As condicoes de validade aparecem mesmo sem run: elas nao dependem de
    # ter havido execucao, e sim do nivel de fidelidade do desenho.
    assert "fidelidade 1" in corpo["condicoes_validade"].lower()


def test_rota_simulador_com_execucoes(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    run_id = client.post("/api/run", json={"author": "teste"}).json()["run_id"]
    dataset_id = criar_dataset(conn, [50_000] * 100)
    cfg = ExperimentConfig()
    comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0), config=cfg)
    vender(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(4), config=cfg)

    corpo = client.get("/api/simulador").json()
    assert corpo["execucoes"] == 2
    assert corpo["posicao_sats"] == 0
    assert corpo["fidelity_level"] == 1
    c = corpo["custos_cents"]
    assert c["total"] == c["taxa"] + c["spread"] + c["slippage"] + c["penalidade"]
    assert all(c[k] > 0 for k in ("taxa", "spread", "slippage", "penalidade"))


def test_rota_execucoes_lista_com_a_decomposicao(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    run_id = client.post("/api/run", json={"author": "teste"}).json()["run_id"]
    dataset_id = criar_dataset(conn, [50_000] * 100)
    comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0),
            config=ExperimentConfig())

    corpo = client.get("/api/execucoes").json()
    item = corpo["items"][0]
    assert item["side"] == "compra"
    assert item["execution_bar_ms"] > item["decision_bar_ms"]
    assert item["price_exec"] >= item["price_ref"]
    assert item["fidelity_level"] == 1
    for campo in ("fee_cents", "spread_cents", "slippage_cents", "penalty_cents"):
        assert campo in item, "a decomposicao tem de chegar ao painel"


def test_carteira_do_painel_e_a_do_run_ativo(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Regressao: contas globais fariam o segundo run herdar o primeiro."""
    primeiro = client.post("/api/run", json={"author": "t"}).json()["run_id"]
    dataset_id = criar_dataset(conn, [50_000] * 100)
    comprar(conn, run_id=primeiro, dataset_id=dataset_id, decision_bar_ms=barra(0),
            config=ExperimentConfig())
    client.post(f"/api/run/{primeiro}/encerrar", json={"estado": "concluido"})

    client.post("/api/run", json={"author": "t"})
    carteira = client.get("/api/ledger").json()["carteira"]
    assert carteira["simulado_usd"]["caixa_minor"] == SEMENTE_USD
    assert carteira["simulado_usd"]["posicao_btc_minor"] == 0


def test_custo_de_execucao_da_carteira_inclui_o_spread(
    conn: sqlite3.Connection, cenario
) -> None:
    """Regressao: `sim.despesa.spread` nasceu depois das outras tres contas de
    custo e ficou de fora da soma, subnotificando o custo em silencio."""
    from app.ledger.livro import carteira

    run_id, dataset_id, cfg = cenario
    e = comprar(conn, run_id=run_id, dataset_id=dataset_id,
                decision_bar_ms=barra(0), config=cfg)
    assert e.spread_cents > 0
    c = carteira(conn, run_id=run_id)
    assert c["simulado_usd"]["custo_execucao_minor"] == e.custo_total_cents


def test_rota_simulador_mostra_o_ultimo_run_quando_nao_ha_ativo(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """"Nao ha run ativo" nao e "nao houve execucao nenhuma"."""
    from app.ledger.livro import encerrar_run

    run_id = client.post("/api/run", json={"author": "t"}).json()["run_id"]
    dataset_id = criar_dataset(conn, [50_000] * 100)
    comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0),
            config=ExperimentConfig())
    encerrar_run(conn, run_id, "concluido")

    corpo = client.get("/api/simulador").json()
    assert corpo["run_ativo"] is None
    assert corpo["run_exibido"] == run_id
    assert corpo["encerrado"] is True
    assert corpo["execucoes"] == 1
    assert corpo["custos_cents"]["total"] > 0


# ============================================================================
# As condicoes de validade nao podem envelhecer
#
# Regressao da D20: `condicoes_validade` era constante e dizia "limite adverso
# da barra". Quando o modelo passou a ser `abertura`, o texto continuou o
# mesmo e passou a MENTIR sobre o modelo que produziu os numeros que ele
# acompanhava. Um campo cuja unica funcao e declarar sob que condicoes um
# resultado vale nao pode ser a coisa que mais facilmente envelhece.
# ============================================================================


def test_condicoes_descrevem_o_modelo_de_execucao_vigente() -> None:
    abertura = sim.condicoes_de_validade(
        ExperimentConfig(execution_reference="abertura")
    )
    adverso = sim.condicoes_de_validade(
        ExperimentConfig(execution_reference="limite_adverso")
    )
    assert "abertura da barra" in abertura
    assert "limite adverso" not in abertura
    assert "limite adverso" in adverso
    assert abertura != adverso


def test_condicoes_declaram_fidelidade_e_latencia() -> None:
    texto = sim.condicoes_de_validade(
        ExperimentConfig(fidelity_level=1, latency_bars=3)
    )
    assert "Fidelidade 1" in texto
    assert "3 barra" in texto
    assert "book" in texto and "fila" in texto and "maker" in texto
    assert "Nenhuma conclusao estatistica" in texto


def test_condicoes_do_run_vem_da_config_DELE_e_nao_da_vigente(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Um run antigo reporta as condicoes em que rodou, nao as de hoje.

    E o que torna um resultado comparavel muito depois de produzido - e o que
    impede que mudar a config reescreva o significado do que ja foi medido.
    """
    run_id = client.post("/api/run", json={"author": "t"}).json()["run_id"]
    dataset_id = criar_dataset(conn, [50_000] * 100)
    comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0),
            config=ExperimentConfig())

    antes = sim.condicoes_do_run(conn, run_id)
    assert "abertura da barra" in antes

    # Muda a config para o outro modelo, DEPOIS do run.
    from app.ledger.livro import encerrar_run

    encerrar_run(conn, run_id, "concluido")
    assert client.post(
        "/api/config",
        json={"author": "t", "changes": {"execution_reference": "limite_adverso"}},
    ).status_code == 201

    # O run antigo continua contando a verdade sobre si mesmo.
    assert sim.condicoes_do_run(conn, run_id) == antes
    assert "abertura da barra" in sim.condicoes_do_run(conn, run_id)


def test_resumo_carrega_as_condicoes_do_proprio_run(
    conn: sqlite3.Connection, cenario
) -> None:
    run_id, dataset_id, cfg = cenario
    comprar(conn, run_id=run_id, dataset_id=dataset_id, decision_bar_ms=barra(0),
            config=cfg)
    assert "abertura da barra" in resumo(conn, run_id)["condicoes_validade"]
