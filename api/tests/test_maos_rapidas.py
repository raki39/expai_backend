"""Testes do incremento 4: maos rapidas e os baselines B1, B2 e B3.

O criterio 1 e o que mais importa e o mais facil de satisfazer por acidente:
neste ponto o sistema produz uma comparacao completa **sem nenhum LLM
envolvido**. Isso e deliberado - se o encanamento nao fecha sem o modelo, o
problema nao e o modelo.
"""

from __future__ import annotations

import pathlib
import random
import socket
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

from app.config.schema import ExperimentConfig
from app.dataset.loader import BarraCarregada
from app.ledger.livro import abrir_run, conferir_partidas_dobradas, encerrar_run
from app.maos_rapidas import baselines, executor
from app.regra import registro
from app.regra.schema import (
    BandaDesvio,
    BreakoutCanal,
    CondicoesValidade,
    CruzamentoMedias,
    Regra,
)
from app.regra.sinais import Sinal, avaliar
from app.simulador import execucao as simulador
from tests.test_simulador import INICIO, INTERVALO, criar_dataset

SEMENTE_USD = 100_000


def precos_passeio(n: int, seed: int = 7, inicio: int = 50_000) -> list[int]:
    """Passeio aleatorio: serie sem tendencia para o acaso explorar."""
    rng = random.Random(seed)
    precos = [inicio]
    for _ in range(n - 1):
        precos.append(max(1_000, precos[-1] + rng.randint(-120, 120)))
    return precos


@pytest.fixture
def cenario(conn: sqlite3.Connection):
    dataset_id = criar_dataset(conn, precos_passeio(2_500))
    return dataset_id, ExperimentConfig()


def _condicoes(cfg: ExperimentConfig) -> CondicoesValidade:
    return baselines.condicoes(cfg)


# ============================================================================
# CRITERIO 1 - fronteira cerebro / maos rapidas
# ============================================================================


def test_nenhum_modulo_de_llm_e_importado_pelas_maos_rapidas() -> None:
    """Le o FONTE: import dentro de funcao escaparia de inspecao em runtime."""
    proibidos = ("langgraph", "anthropic", "openai", "langchain")
    raiz = pathlib.Path(__file__).resolve().parents[1] / "app"
    for pacote in ("maos_rapidas", "regra", "simulador", "dataset", "ledger"):
        for arquivo in (raiz / pacote).rglob("*.py"):
            for linha in arquivo.read_text(encoding="utf-8").splitlines():
                despido = linha.strip().lower()
                if despido.startswith(("import ", "from ")):
                    assert not any(p in despido for p in proibidos), (
                        f"{arquivo.relative_to(raiz)}: {linha.strip()}"
                    )


def test_o_laco_por_barra_nao_abre_socket(
    conn: sqlite3.Connection, cenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero chamadas de rede dentro do laco (secao 3.2).

    Bloqueia no nivel do socket, e nao contando chamadas a um cliente: um
    contador so pega o cliente que se conhece, e o socket pega qualquer um.
    """
    dataset_id, cfg = cenario

    class SemRede(socket.socket):
        def __init__(self, *a, **k):
            raise AssertionError("o laco por barra tentou abrir um socket")

    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    barras = executor.carregar_janela(conn, dataset_id)

    monkeypatch.setattr(socket, "socket", SemRede)
    resultado = baselines.rodar_b3(
        conn, run_id=run_id, dataset_id=dataset_id, config=cfg, barras=barras
    )
    assert resultado.execucoes > 0


def test_provedor_de_llm_ausente_do_ambiente(conn, cenario) -> None:
    """Nenhum modulo de provedor sequer carregado depois de rodar a comparacao."""
    dataset_id, cfg = cenario
    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    baselines.rodar_b3(conn, run_id=run_id, dataset_id=dataset_id, config=cfg)
    carregados = set(sys.modules)
    assert not {"langgraph", "anthropic", "openai", "langchain"} & carregados


# ============================================================================
# CRITERIO 2 - determinismo e digest
# ============================================================================


def test_mesma_semente_mesmo_digest(conn: sqlite3.Connection, cenario) -> None:
    dataset_id, cfg = cenario
    primeira = baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1, semente=42
    )
    segunda = baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1, semente=42
    )
    assert primeira["B3"]["digest"] == segunda["B3"]["digest"]
    assert (
        primeira["B1"]["representativa"]["digest"]
        == segunda["B1"]["representativa"]["digest"]
    )
    assert primeira["B2"]["digest"] == segunda["B2"]["digest"]


def test_semente_diferente_muda_o_aleatorio_e_nao_o_determinista(
    conn: sqlite3.Connection, cenario
) -> None:
    """B3 nao depende de semente, e isso esta certo: ele e determinista.

    Quem carrega a semente para dentro do ledger e a repeticao representativa
    do B1 - e e la que o digest tem de mudar.
    """
    dataset_id, cfg = cenario
    a = baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1, semente=1
    )
    b = baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1, semente=2
    )
    assert a["B3"]["digest"] == b["B3"]["digest"]
    assert (
        a["B1"]["representativa"]["digest"] != b["B1"]["representativa"]["digest"]
    )
    assert a["B1"]["p50"] != b["B1"]["p50"]


def test_digest_ignora_id_de_linha(conn: sqlite3.Connection, cenario) -> None:
    """Dois runs economicamente identicos tem ids diferentes e mesmo digest."""
    dataset_id, cfg = cenario
    digests = []
    for _ in range(2):
        run_id, _ = abrir_run(
            conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD
        )
        r = baselines.rodar_b3(
            conn, run_id=run_id, dataset_id=dataset_id, config=cfg
        )
        digests.append(r.digest)
        encerrar_run(conn, run_id, "concluido")
    assert digests[0] == digests[1]


def test_regra_diferente_muda_o_digest(conn: sqlite3.Connection, cenario) -> None:
    """Um digest que nao distingue estrategias nao serve para reproduzir nada."""
    dataset_id, cfg = cenario
    digests = []
    for rapida, lenta in ((20, 50), (10, 30)):
        run_id, _ = abrir_run(
            conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD
        )
        regra = Regra(
            params=CruzamentoMedias(rapida=rapida, lenta=lenta),
            condicoes_validade=_condicoes(cfg),
        )
        rule_id = registro.registrar(conn, regra)
        digests.append(
            executor.rodar(
                conn, run_id=run_id, dataset_id=dataset_id, regra=regra,
                rule_id=rule_id, config=cfg,
            ).digest
        )
        encerrar_run(conn, run_id, "concluido")
    assert digests[0] != digests[1]


# ============================================================================
# CRITERIO 3 - B2 conferivel a mao
# ============================================================================


def test_b2_e_exatamente_uma_ida_e_volta_conferida_a_mao(
    conn: sqlite3.Connection, cenario
) -> None:
    """Comprar no primeiro preco e vender no ultimo, menos UM par de custos."""
    dataset_id, cfg = cenario
    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    barras = executor.carregar_janela(conn, dataset_id)
    resultado = baselines.rodar_b2(
        conn, run_id=run_id, dataset_id=dataset_id, config=cfg, barras=barras
    )

    # --- a conta, feita a mao -------------------------------------------
    lat = cfg.latency_bars
    ultima_decidivel = len(barras) - 1 - lat
    barra_compra = barras[0 + lat]
    barra_venda = barras[ultima_decidivel + lat]

    ref_c = barra_compra.high              # limite adverso na compra
    exec_c = simulador.preco_executado(ref_c, "compra", cfg)
    qty = simulador.dimensionar(SEMENTE_USD, exec_c, cfg)
    nocional_c, custos_c = simulador.custear(qty, ref_c, exec_c, "compra", cfg)

    ref_v = barra_venda.low                # limite adverso na venda
    exec_v = simulador.preco_executado(ref_v, "venda", cfg)
    nocional_v, custos_v = simulador.custear(qty, ref_v, exec_v, "venda", cfg)

    esperado = (
        SEMENTE_USD
        - nocional_c - custos_c.de_preco - custos_c.fee
        + nocional_v - custos_v.de_preco - custos_v.fee
    )
    assert simulador.caixa_cents(conn, run_id) == esperado

    # Um par de custos, e nao mais: B2 nao gira.
    assert resultado["operacoes"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM execution WHERE run_id = ?", (run_id,)
    ).fetchone()["n"] == 2


def test_b2_compra_na_primeira_e_vende_na_ultima(
    conn: sqlite3.Connection, cenario
) -> None:
    dataset_id, cfg = cenario
    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    barras = executor.carregar_janela(conn, dataset_id)
    baselines.rodar_b2(
        conn, run_id=run_id, dataset_id=dataset_id, config=cfg, barras=barras
    )
    linhas = conn.execute(
        "SELECT side, execution_bar_ms FROM execution WHERE run_id = ?"
        " ORDER BY id", (run_id,)
    ).fetchall()
    assert linhas[0]["side"] == "compra"
    assert linhas[0]["execution_bar_ms"] == barras[cfg.latency_bars].open_time_ms
    assert linhas[1]["side"] == "venda"
    assert linhas[1]["execution_bar_ms"] == barras[-1].open_time_ms


# ============================================================================
# CRITERIO 4 - B1 como distribuicao
# ============================================================================


def test_b1_exige_o_minimo_de_mil_repeticoes(conn, cenario) -> None:
    """Um numero unico nao satisfaz a secao 14.3."""
    dataset_id, cfg = cenario
    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    with pytest.raises(ValueError, match="minimo"):
        baselines.rodar_b1(
            conn, run_id=run_id, dataset_id=dataset_id, config=cfg,
            operacoes_alvo=5, semente=1, repeticoes=999,
        )


def test_b1_devolve_distribuicao_ordenada(conn, cenario) -> None:
    dataset_id, cfg = cenario
    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    r = baselines.rodar_b1(
        conn, run_id=run_id, dataset_id=dataset_id, config=cfg,
        operacoes_alvo=10, semente=42,
    )
    assert r["minimo"] <= r["p5"] <= r["p50"] <= r["p95"] <= r["maximo"]
    assert r["repeticoes"] == 1_000
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM baseline_result WHERE baseline = 'B1'"
    ).fetchone()["n"] == 1_000


def test_sementes_sao_derivadas_deterministicamente() -> None:
    """A distribuicao inteira e reproduzivel a partir da semente do run."""
    a = [baselines.derivar_semente(42, i) for i in range(5)]
    b = [baselines.derivar_semente(42, i) for i in range(5)]
    assert a == b
    assert len(set(a)) == 5, "sementes derivadas nao podem colidir"
    assert a != [baselines.derivar_semente(43, i) for i in range(5)]
    # Precisa caber num INTEGER assinado do SQLite, onde ela vai ser gravada.
    assert all(0 <= s < 2**63 for s in a)


def test_b1_perde_dinheiro_e_a_perda_cresce_com_o_giro(conn, cenario) -> None:
    """Secao 8.4.1.3: o prejuizo do aleatorio e proporcional ao numero de
    operacoes. Se nao for, o simulador esta generoso."""
    dataset_id, cfg = cenario
    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    barras = executor.carregar_janela(conn, dataset_id)
    medianas = {}
    for ops in (5, 20, 60):
        r = baselines.rodar_b1(
            conn, run_id=run_id, dataset_id=dataset_id, config=cfg,
            operacoes_alvo=ops, semente=42, barras=barras, persistir=False,
        )
        medianas[ops] = r["p50"]
    assert medianas[5] > medianas[20] > medianas[60]
    assert medianas[60] < SEMENTE_USD


def test_b1_casa_o_giro_com_o_b3(conn: sqlite3.Connection, cenario) -> None:
    """D19: se B1 girasse menos, a diferenca mediria custo e nao timing."""
    dataset_id, cfg = cenario
    r = baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1, semente=42
    )
    assert r["B1"]["operacoes_alvo"] == r["B3"]["operacoes"]
    assert r["B3"]["operacoes"] > 0


def test_percentil_nao_interpola() -> None:
    """Interpolar inventaria um valor que nenhuma repeticao produziu."""
    valores = [10, 20, 30, 40]
    assert baselines.percentil(valores, 50) in valores
    assert baselines.percentil(valores, 95) in valores
    assert baselines.percentil(valores, 5) in valores


# ============================================================================
# CRITERIO 5 - B3 congelado
# ============================================================================


def test_b3_e_congelado_com_data(conn: sqlite3.Connection, cenario) -> None:
    dataset_id, cfg = cenario
    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    r = baselines.rodar_b3(conn, run_id=run_id, dataset_id=dataset_id, config=cfg)
    linha = conn.execute(
        "SELECT frozen_at, params_json FROM rule WHERE id = ?", (r.rule_id,)
    ).fetchone()
    assert linha["frozen_at"], "sem timestamp, 'congelado' e so intencao"
    assert '"rapida":20' in linha["params_json"].replace(" ", "")
    assert '"lenta":50' in linha["params_json"].replace(" ", "")


def test_o_BANCO_recusa_tunar_b3_depois_de_congelado(
    conn: sqlite3.Connection, cenario
) -> None:
    """Retunar depois de ver o resultado destroi o grupo de controle."""
    dataset_id, cfg = cenario
    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    r = baselines.rodar_b3(conn, run_id=run_id, dataset_id=dataset_id, config=cfg)
    with pytest.raises(sqlite3.IntegrityError, match="congelada"):
        conn.execute(
            "UPDATE rule SET params_json = '{\"rapida\":5,\"lenta\":9}' WHERE id = ?",
            (r.rule_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="acrescimo"):
        conn.execute("DELETE FROM rule WHERE id = ?", (r.rule_id,))


def test_lista_de_operacoes_do_b3_e_reproduzivel(
    conn: sqlite3.Connection, cenario
) -> None:
    dataset_id, cfg = cenario
    listas = []
    for _ in range(2):
        run_id, _ = abrir_run(
            conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD
        )
        baselines.rodar_b3(conn, run_id=run_id, dataset_id=dataset_id, config=cfg)
        listas.append([
            (l["side"], l["execution_bar_ms"])
            for l in conn.execute(
                "SELECT side, execution_bar_ms FROM execution WHERE run_id = ?"
                " ORDER BY id", (run_id,)
            )
        ])
        encerrar_run(conn, run_id, "concluido")
    assert listas[0] == listas[1]
    assert len(listas[0]) > 0


# ============================================================================
# CRITERIO 6 - paridade de tratamento
# ============================================================================


def test_o_caminho_em_memoria_e_o_persistido_dao_o_MESMO_centavo(
    conn: sqlite3.Connection, cenario
) -> None:
    """A prova de que "mesmo simulador" e fato, e nao afirmacao.

    A mesma repeticao de B1, rodada pelos dois caminhos, tem de chegar ao
    mesmo centavo. Se divergir, as mil repeticoes em memoria nao estao
    medindo o que o ledger mediria.
    """
    dataset_id, cfg = cenario
    barras = executor.carregar_janela(conn, dataset_id)
    ultima_decidivel = len(barras) - 1 - cfg.latency_bars
    pares = baselines.sortear_pares(
        baselines.derivar_semente(42, 0), ultima_decidivel, 0, 12
    )

    em_memoria = baselines.simular_pares(
        barras, pares, caixa_inicial_cents=SEMENTE_USD, config=cfg,
        fracao_bps=10_000,
    )

    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    rule_id = registro.registrar_baseline(
        conn, "aleatorio", {"teste": True}, _condicoes(cfg)
    )
    persistido = baselines.rodar_b1_representativa(
        conn, run_id=run_id, dataset_id=dataset_id, config=cfg, pares=pares,
        rule_id=rule_id, barras=barras,
    )

    assert persistido["equity_final_cents"] == em_memoria.equity_final_cents


def test_os_tres_baselines_usam_os_mesmos_parametros_efetivos(
    conn: sqlite3.Connection, cenario
) -> None:
    """Compara os parametros efetivos e falha se divergirem (criterio 6)."""
    dataset_id, cfg = cenario
    baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1, semente=42
    )
    linhas = conn.execute(
        "SELECT DISTINCT fidelity_level FROM execution"
    ).fetchall()
    assert len(linhas) == 1, "fidelidade divergente entre baselines"

    # Mesmo capital semente para os tres.
    capitais = conn.execute(
        "SELECT DISTINCT e.amount_minor FROM ledger_entry e"
        " JOIN ledger_transaction t ON t.id = e.transaction_id"
        " JOIN account a ON a.id = e.account_id"
        " WHERE t.kind = 'abertura' AND a.code = 'sim.carteira.caixa'"
    ).fetchall()
    assert len(capitais) == 1

    # Toda execucao, de qualquer baseline, tem de ser reproduzivel pelas
    # MESMAS funcoes de precificacao. Comparar bps efetivo nao serviria: o
    # arredondamento e por operacao, entao ele varia legitimamente.
    linhas = conn.execute("SELECT * FROM execution").fetchall()
    assert linhas
    for e in linhas:
        assert e["price_exec"] == simulador.preco_executado(
            e["price_ref"], e["side"], cfg
        ), "preco executado nao veio da formula do simulador"
        nocional, custos = simulador.custear(
            e["quantity_sats"], e["price_ref"], e["price_exec"], e["side"], cfg
        )
        assert e["notional_ref_cents"] == nocional
        assert e["fee_cents"] == custos.fee
        assert e["spread_cents"] == custos.spread
        assert e["slippage_cents"] == custos.slippage
        assert e["penalty_cents"] == custos.penalty


def test_todo_baseline_deixa_o_livro_equilibrado(
    conn: sqlite3.Connection, cenario
) -> None:
    dataset_id, cfg = cenario
    baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1, semente=42
    )
    assert conferir_partidas_dobradas(conn) == []


def test_cada_baseline_tem_carteira_propria(
    conn: sqlite3.Connection, cenario
) -> None:
    """Rodar os tres na mesma carteira faria o segundo herdar o primeiro."""
    dataset_id, cfg = cenario
    r = baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1, semente=42
    )
    assert r["B2"]["run_id"] != r["B3"]["run_id"] != r["B1"]["run_id"]
    for chave in ("B2", "B3"):
        assert r[chave]["equity_final_cents"] != SEMENTE_USD


# ============================================================================
# CRITERIO 7 - vinculo com a regra (R25.5)
# ============================================================================


def test_toda_execucao_aponta_para_a_regra_que_a_autorizou(
    conn: sqlite3.Connection, cenario
) -> None:
    dataset_id, cfg = cenario
    r = baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1, semente=42
    )
    for chave in ("B1", "B2", "B3"):
        run_id = r[chave]["run_id"]
        assert registro.execucoes_sem_regra(conn, run_id) == [], (
            f"{chave} tem execucao sem regra"
        )


def test_da_execucao_se_chega_a_regra(conn: sqlite3.Connection, cenario) -> None:
    """A consulta que o criterio 7 pede, feita de verdade."""
    dataset_id, cfg = cenario
    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    resultado = baselines.rodar_b3(
        conn, run_id=run_id, dataset_id=dataset_id, config=cfg
    )
    execution_id = conn.execute(
        "SELECT id FROM execution WHERE run_id = ? LIMIT 1", (run_id,)
    ).fetchone()["id"]

    regra = registro.regra_da_execucao(conn, execution_id)
    assert regra is not None
    assert regra["hash"] == resultado.regra_hash
    assert regra["family"] == "cruzamento_medias"
    assert regra["frozen_at"]
    # A procedencia viaja junto: mercado, instrumento, timeframe, fidelidade.
    assert '"symbol":"BTCUSDT"' in regra["condicoes_validade_json"].replace(" ", "")


def test_a_conferencia_DETECTA_execucao_sem_regra(
    conn: sqlite3.Connection, cenario
) -> None:
    """Uma conferencia que nunca acusa nada nao esta conferindo."""
    dataset_id, cfg = cenario
    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    simulador.comprar(
        conn, run_id=run_id, dataset_id=dataset_id,
        decision_bar_ms=INICIO, config=cfg,  # sem rule_id
    )
    assert registro.execucoes_sem_regra(conn, run_id) != []


# ============================================================================
# Regra: formato e avaliacao
# ============================================================================


def test_catalogo_e_fechado() -> None:
    """Familia fora do catalogo nao e "regra ruim": e regra invalida."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Regra.model_validate({
            "params": {"familia": "rede_neural", "camadas": 3},
            "condicoes_validade": {
                "venue": "binance", "symbol": "BTCUSDT",
                "timeframe": "15m", "fidelity_level": 1,
            },
        })


def test_regra_exige_condicoes_de_validade() -> None:
    """Regra sem escopo e afirmacao sem condicoes de validade."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Regra.model_validate({"params": {"familia": "breakout_canal", "periodo": 20}})


def test_hash_e_de_conteudo_e_nao_de_momento() -> None:
    cv = CondicoesValidade(
        venue="binance", symbol="BTCUSDT", timeframe="15m", fidelity_level=1
    )
    a = Regra(params=CruzamentoMedias(rapida=20, lenta=50), condicoes_validade=cv)
    b = Regra(params=CruzamentoMedias(lenta=50, rapida=20), condicoes_validade=cv)
    c = Regra(params=CruzamentoMedias(rapida=21, lenta=50), condicoes_validade=cv)
    assert a.hash() == b.hash()
    assert a.hash() != c.hash()


def test_registrar_a_mesma_regra_duas_vezes_devolve_a_mesma_linha(conn) -> None:
    cv = CondicoesValidade(
        venue="binance", symbol="BTCUSDT", timeframe="15m", fidelity_level=1
    )
    regra = Regra(params=BreakoutCanal(periodo=20), condicoes_validade=cv)
    assert registro.registrar(conn, regra) == registro.registrar(conn, regra)


def _barras(precos: list[int]) -> list[BarraCarregada]:
    e = 10**8
    return [
        BarraCarregada(
            INICIO + i * INTERVALO, INICIO + (i + 1) * INTERVALO,
            p * e, (p + 1) * e, (p - 1) * e, p * e, e, e, 1,
        )
        for i, p in enumerate(precos)
    ]


def test_cruzamento_emite_evento_e_nao_estado() -> None:
    """"Esta acima" vale centenas de barras; "acabou de cruzar" acontece uma vez."""
    cv = CondicoesValidade(
        venue="b", symbol="s", timeframe="15m", fidelity_level=1
    )
    regra = Regra(
        params=CruzamentoMedias(rapida=2, lenta=4), condicoes_validade=cv
    )
    # Sobe firme e depois cai firme: um cruzamento para cada lado.
    precos = [100] * 6 + list(range(100, 130)) + list(range(130, 100, -1))
    sinais = avaliar(_barras(precos), regra)
    assert sinais.count(Sinal.ENTRAR) == 1
    assert sinais.count(Sinal.SAIR) == 1


def test_nao_ha_sinal_antes_da_janela_minima() -> None:
    """Antes disso o indicador nao existe - o que e diferente de sinal neutro."""
    cv = CondicoesValidade(venue="b", symbol="s", timeframe="15m", fidelity_level=1)
    regra = Regra(params=CruzamentoMedias(rapida=5, lenta=20), condicoes_validade=cv)
    sinais = avaliar(_barras(list(range(100, 160))), regra)
    assert all(s == Sinal.NADA for s in sinais[:19])


def test_breakout_usa_o_canal_das_barras_ANTERIORES() -> None:
    """Um canal que inclui a propria barra nunca e rompido por ela."""
    cv = CondicoesValidade(venue="b", symbol="s", timeframe="15m", fidelity_level=1)
    regra = Regra(params=BreakoutCanal(periodo=5), condicoes_validade=cv)
    precos = [100] * 10 + [200]
    sinais = avaliar(_barras(precos), regra)
    assert sinais[10] == Sinal.ENTRAR


def test_banda_de_desvio_entra_abaixo_da_banda() -> None:
    cv = CondicoesValidade(venue="b", symbol="s", timeframe="15m", fidelity_level=1)
    regra = Regra(
        params=BandaDesvio(periodo=10, desvios_milesimos=2_000),
        condicoes_validade=cv,
    )
    precos = [100] * 15 + [70] + [100] * 5
    sinais = avaliar(_barras(precos), regra)
    assert Sinal.ENTRAR in sinais


def test_avaliacao_nao_usa_ponto_flutuante() -> None:
    """Comparar medias dividindo trunca os dois lados de formas diferentes."""
    fonte = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "regra" / "sinais.py"
    ).read_text(encoding="utf-8")
    assert "float(" not in fonte
    assert "Decimal" not in fonte


# ============================================================================
# Executor
# ============================================================================


def test_executor_fecha_posicao_aberta_no_fim(
    conn: sqlite3.Connection, cenario
) -> None:
    """Posicao aberta no fim tornaria o resultado incomparavel."""
    dataset_id, cfg = cenario
    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    # Cai e depois sobe ate o fim: o cruzamento para cima acontece durante a
    # subida e nunca se desfaz. Serie ESTRITAMENTE crescente nao serviria -
    # nela a media rapida ja nasce acima da lenta e nunca CRUZA, e o sinal e
    # evento, nao estado.
    subindo = criar_dataset(
        conn, list(range(41_000, 40_000, -1)) + list(range(40_000, 42_000))
    )
    regra = Regra(
        params=CruzamentoMedias(rapida=5, lenta=20),
        condicoes_validade=_condicoes(cfg),
    )
    rule_id = registro.registrar(conn, regra)
    r = executor.rodar(
        conn, run_id=run_id, dataset_id=subindo, regra=regra, rule_id=rule_id,
        config=cfg,
    )
    assert r.fechou_no_fim is True
    assert simulador.posicao_sats(conn, run_id) == 0


def test_executor_nunca_decide_alem_da_ultima_barra_executavel(
    conn: sqlite3.Connection, cenario
) -> None:
    dataset_id, cfg = cenario
    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    barras = executor.carregar_janela(conn, dataset_id)
    baselines.rodar_b3(
        conn, run_id=run_id, dataset_id=dataset_id, config=cfg, barras=barras
    )
    maior = conn.execute(
        "SELECT MAX(decision_bar_ms) AS m FROM execution WHERE run_id = ?",
        (run_id,),
    ).fetchone()["m"]
    assert maior <= barras[len(barras) - 1 - cfg.latency_bars].open_time_ms


def test_janela_curta_demais_falha_alto(conn: sqlite3.Connection) -> None:
    cfg = ExperimentConfig()
    curto = criar_dataset(conn, [50_000] * 60)
    run_id, _ = abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    regra = Regra(
        params=CruzamentoMedias(rapida=20, lenta=50),
        condicoes_validade=_condicoes(cfg),
    )
    rule_id = registro.registrar(conn, regra)
    with pytest.raises(ValueError, match="nao comporta"):
        executor.rodar(
            conn, run_id=run_id, dataset_id=curto, regra=regra,
            rule_id=rule_id, config=cfg,
        )


# ============================================================================
# Rotas
# ============================================================================


def test_rota_comparacao_antes_de_rodar(client: TestClient) -> None:
    assert client.get("/api/comparacao").json()["existe"] is False


def test_rota_recusa_comparacao_sem_dataset(client: TestClient) -> None:
    """Melhor recusar que comparar contra nada."""
    resposta = client.post("/api/comparacao", json={"author": "t"})
    assert resposta.status_code == 409
    assert "dataset" in resposta.json()["detail"]


def test_rota_recusa_comparacao_com_run_ativo(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    criar_dataset(conn, precos_passeio(2_500))
    client.post("/api/run", json={"author": "t"})
    resposta = client.post("/api/comparacao", json={"author": "t"})
    assert resposta.status_code == 409


def test_rota_roda_a_comparacao_completa(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    criar_dataset(conn, precos_passeio(2_500))
    resposta = client.post("/api/comparacao", json={"author": "t", "semente": 42})
    assert resposta.status_code == 201
    corpo = resposta.json()
    assert corpo["B1"]["repeticoes"] == 1_000
    assert corpo["B1"]["operacoes_alvo"] == corpo["B3"]["operacoes"]
    assert corpo["B2"]["operacoes"] == 1

    resumo = client.get("/api/comparacao").json()
    assert resumo["existe"] is True
    assert resumo["B2"]["equity_final_cents"] == corpo["B2"]["equity_final_cents"]
    assert resumo["B3"]["digest"] == corpo["B3"]["digest"]
    assert resumo["B1"]["p50"] == corpo["B1"]["p50"]
    # O aviso da fase acompanha o resultado, sempre.
    assert "nenhuma conclusao estatistica" in resumo["aviso"].lower()
    assert "sem nenhum llm" in resumo["aviso"].lower()


def test_resumo_e_derivado_e_nao_guardado_em_duplicata(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Um resumo guardado a parte diverge no dia em que alguem esquecer."""
    criar_dataset(conn, precos_passeio(2_500))
    client.post("/api/comparacao", json={"author": "t", "semente": 42})
    tabelas = {
        l["name"]
        for l in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "comparacao" not in tabelas
    assert "comparison_summary" not in tabelas


def test_reexecutar_nao_mistura_duas_distribuicoes(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """O p50 de duas comparacoes somadas nao descreve experimento nenhum."""
    criar_dataset(conn, precos_passeio(2_500))
    client.post("/api/comparacao", json={"author": "t", "semente": 1})
    segunda = client.post("/api/comparacao", json={"author": "t", "semente": 2}).json()

    # Ha 2000 linhas de B1 no banco, de duas comparacoes distintas...
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM baseline_result WHERE baseline = 'B1'"
    ).fetchone()["n"] == 2_000

    # ...e o resumo descreve SO a ultima.
    resumo = client.get("/api/comparacao").json()
    assert resumo["B1"]["repeticoes"] == 1_000
    assert resumo["B1"]["p50"] == segunda["B1"]["p50"]
    assert resumo["B3"]["digest"] == segunda["B3"]["digest"]
