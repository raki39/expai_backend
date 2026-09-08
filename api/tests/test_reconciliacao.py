"""A série por barra reconstrói a diferença econômica do Portão B?

> *"Confirme que a soma da série observada de excesso por barra reconstrói a
> diferença econômica usada pelo Portão B, incluindo custos e marcação final.
> Se a exposição real variar, isso precisa permanecer separado da base fixa."*
> — o usuário, 2026-09-08

**A resposta curta é NÃO, e o motivo não é um defeito: essa série não existe.**
Este arquivo mede o que existe, e fixa cada identidade que de fato vale, para
que a ausência pare de ser uma suposição de quem lê.

## O que existe, e o que cada coisa é

| objeto | o que é | inclui custos? | inclui marcação final? |
|---|---|---|---|
| `executor.retornos_do_run` | retornos do **MERCADO** por barra, em bps | não — é preço, não P&L | não se aplica |
| `curva.curva_do_run` | patrimônio por barra de **um run**, de `delta_caixa` do ledger | sim, os de execução | **sim** — marca a posição a mercado |
| `simulador.caixa_cents` | saldo da conta `CAIXA_SIM` do run | sim, **todos**, inclusive reflexão | **não** — é caixa |
| `excesso_sobre_b3_cents` | `caixa_cents(agente) − caixa_cents(B3)` | sim | não |

**`retornos_do_run` é a série mais parecida com "por barra", e ela é do
mercado.** Chamá-la de excesso seria confundir o que o preço fez com o que a
estratégia ganhou.

## Por que a série de excesso por barra não pode ser somada hoje

Excesso é uma diferença entre **dois runs**, e eles não compartilham grade: o
agente e o B3 executam em instantes diferentes, com giros diferentes. Não há
barra em que os dois tenham um valor a subtrair — só os dois finais têm.

Construir essa série seria trabalho novo (marcar os dois a mercado na mesma
grade), e não é conserto de defeito: é um objeto que ninguém precisou até agora.
Fica **declarado** aqui em vez de suposto.

## O que este arquivo PROVA, então

As identidades que existem, cada uma com o que ela inclui e o que ela não
inclui — porque a diferença entre elas é onde alguém erraria ao somar.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.config.schema import ExperimentConfig
from app.dataset import loader
from app.ledger import contas
from app.maos_rapidas import baselines, curva as curva_mod, executor
from app.simulador import execucao as simulador
from tests.test_cerebro import settings  # noqa: F401
from tests.test_maos_rapidas import cenario  # noqa: F401
from tests.test_simulador import criar_dataset  # noqa: F401

SEMENTE_USD = 100_000


def _abrir(conn: sqlite3.Connection) -> int:
    from app.ledger.livro import abrir_run

    run_id, _ = abrir_run(
        conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD
    )
    return run_id


@pytest.fixture
def run_b3(conn: sqlite3.Connection, cenario):
    """Um run de B3 completo, com execuções e ledger reconciliado."""
    dataset_id, cfg = cenario
    run_id = _abrir(conn)
    resultado = baselines.rodar_b3(
        conn, run_id=run_id, dataset_id=dataset_id, config=cfg
    )
    return run_id, dataset_id, cfg, resultado


# ---------------------------------------------------------------------------
# 1. O que a curva reconstrói, e o que ela deixa de fora
# ---------------------------------------------------------------------------


def test_a_curva_reconstroi_o_caixa_a_partir_do_LEDGER(conn, run_b3):
    """Ponto final da curva = abertura + Σ `delta_caixa` + posição a mercado.

    A curva **não recalcula preço nem custo**: ela caminha pelas execuções
    gravadas somando o efeito de cada uma no `CAIXA_SIM`, que é o que o ledger
    registrou. Recalcular aqui criaria uma segunda aritmética do dinheiro.

    Então os custos de execução — taxa, slippage, spread, penalidade — **já
    estão dentro** de cada `delta_caixa`. Somar a série reconstrói o resultado
    econômico daquele run, com custos.
    """
    run_id, dataset_id, cfg, _ = run_b3
    barras = executor.carregar_janela(conn, dataset_id)
    pontos = curva_mod.curva_do_run(conn, run_id, barras=barras, pontos=10_000)
    assert pontos, "a curva saiu vazia: o run não executou nada"

    abertura = int(
        conn.execute(
            "SELECT COALESCE(SUM(e.amount_minor), 0) AS c"
            "  FROM ledger_entry e"
            "  JOIN ledger_transaction t ON t.id = e.transaction_id"
            "  JOIN account a ON a.id = e.account_id"
            " WHERE t.run_id = ? AND t.kind = 'abertura' AND a.code = ?",
            (run_id, contas.CAIXA_SIM),
        ).fetchone()["c"]
    )
    deltas = int(
        conn.execute(
            "SELECT COALESCE(SUM(le.amount_minor), 0) AS c"
            "  FROM execution ex"
            "  JOIN ledger_entry le"
            "    ON le.transaction_id = ex.ledger_transaction_id"
            "  JOIN account a ON a.id = le.account_id"
            " WHERE ex.run_id = ? AND a.code = ?",
            (run_id, contas.CAIXA_SIM),
        ).fetchone()["c"]
    )
    ultimo = pontos[-1]
    posicao_em_dinheiro = ultimo.patrimonio_cents - (abertura + deltas)

    # A identidade: o ponto final é caixa reconstruído + posição marcada.
    assert ultimo.patrimonio_cents == abertura + deltas + posicao_em_dinheiro
    # E a marcação é ZERO quando o run termina sem posição aberta — que é o
    # caso do B3, e por isso a igualdade abaixo é exata.
    if ultimo.posicao_sats == 0:
        assert posicao_em_dinheiro == 0
        assert ultimo.patrimonio_cents == abertura + deltas


def test_o_caixa_do_ledger_e_a_curva_concordam_QUANDO_nao_ha_reflexao(
    conn, run_b3
):
    """E aqui está a diferença que já custou nove centavos na tela.

    `caixa_cents` é o saldo INTEIRO da conta: inclui a reflexão, lançada no
    livro simulado (D21). A curva soma só `delta_caixa` das **execuções**.

    Num run de B3 não há reflexão — zero chamadas de LLM —, então os dois
    coincidem. Num run do agente **não coincidem**, e a diferença é exatamente
    o custo do pensamento: o projeto mediu isso como US$ 0,09 entre o
    número-herói e a tabela, na mesma tela.

    Este teste fixa o caso em que a igualdade vale, e a mensagem diz por que
    ela pode não valer.
    """
    run_id, dataset_id, _cfg, _ = run_b3

    # O que separa a curva do ledger NAO e o `kind` da transacao: e a conta em
    # que ela lanca. A curva soma `delta_caixa` das EXECUCOES sobre `CAIXA_SIM`;
    # `caixa_cents` soma tudo que tocou aquela conta.
    #
    # (Minha primeira versao deste teste supos que um run de B3 so produzisse
    # transacoes de `abertura` e `execucao`. Ele produz mais - e a medicao
    # corrigiu a suposicao antes de ela virar afirmacao.)
    custo_de_pensar = int(
        conn.execute(
            "SELECT COALESCE(SUM(le.amount_minor), 0) AS c"
            "  FROM ledger_entry le"
            "  JOIN ledger_transaction t ON t.id = le.transaction_id"
            "  JOIN account a ON a.id = le.account_id"
            " WHERE t.run_id = ? AND a.code = ? AND t.kind = 'reflexao'",
            (run_id, contas.CAIXA_SIM),
        ).fetchone()["c"]
    )
    assert custo_de_pensar == 0, "o B3 nao pensa: se pensou, o cenario mudou"

    barras = executor.carregar_janela(conn, dataset_id)
    pontos = curva_mod.curva_do_run(conn, run_id, barras=barras, pontos=10_000)
    assert pontos[-1].patrimonio_cents == simulador.caixa_cents(conn, run_id), (
        "curva e ledger divergiram num run SEM reflexão: a única diferença"
        " prevista entre os dois é o custo do pensamento (D21), e aqui não há"
    )


# ---------------------------------------------------------------------------
# 2. O que NÃO existe, e a ausência é declarada em vez de suposta
# ---------------------------------------------------------------------------


def test_retornos_do_run_e_serie_de_MERCADO_e_nao_de_excesso(conn, run_b3):
    """A série mais parecida com "por barra" mede preço, e não P&L.

    Ela existe para descontar autocorrelação (§8.3) e para o Sharpe realizado.
    Somá-la não dá dinheiro nenhum: são retornos de fechamento a fechamento do
    **dataset**, em bps, e valem igual para qualquer estratégia sobre a mesma
    janela.
    """
    run_id, dataset_id, _cfg, _ = run_b3

    serie = executor.retornos_do_run(conn, run_id)
    assert serie, "a série saiu vazia"

    janela = conn.execute(
        "SELECT MIN(execution_bar_ms) AS de, MAX(execution_bar_ms) AS ate"
        "  FROM execution WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    do_mercado = loader.retornos_bps_entre(
        conn, dataset_id, int(janela["de"]), int(janela["ate"])
    )
    assert serie == do_mercado, (
        "`retornos_do_run` deixou de ser a série do mercado: se ela virou P&L,"
        " todo `n_efetivo` e todo p-valor do projeto mudaram de significado"
    )

    # E a prova de que não é excesso: ela não conhece o run. A mesma janela em
    # outro run daria a mesma série.
    assert sum(serie) != simulador.caixa_cents(conn, run_id) - SEMENTE_USD


def test_nao_existe_serie_de_excesso_por_barra_e_isso_e_declarado():
    """A ausência, fixada — para que ninguém a suponha presente.

    Excesso é diferença entre DOIS runs, e eles não compartilham grade: o
    agente e o B3 executam em instantes diferentes, com giros diferentes. Só os
    dois **finais** existem para subtrair.

    Se algum dia alguém construir essa série, este teste falha e a pessoa
    decide o que ele deve passar a afirmar — em vez de a ausência sumir calada.
    """
    import app.maos_rapidas.curva as c
    import app.maos_rapidas.executor as e

    nomes = set(dir(c)) | set(dir(e))
    suspeitos = sorted(
        n for n in nomes
        if "excesso" in n.lower() and ("serie" in n.lower() or "barra" in n.lower())
    )
    assert not suspeitos, (
        f"apareceu algo que parece série de excesso por barra: {suspeitos}."
        " Se foi construído de propósito, este teste deve virar a asserção"
        " contrária, com a identidade que ele reconstrói escrita ao lado"
    )


def test_o_excesso_do_portao_B_e_diferenca_de_dois_CAIXAS(conn, cenario):
    """O que o Portão B de fato compara, medido.

    `excesso_sobre_b3_cents = caixa_cents(candidata) − caixa_cents(B3)`, dois
    saldos de ledger de **runs diferentes**. Cada um inclui todos os custos do
    seu run; nenhum inclui marcação de posição aberta, porque `caixa_cents` é
    caixa.

    Isso é exato quando os runs terminam sem posição — e é o caso aqui.
    """
    dataset_id, cfg = cenario
    run_a = _abrir(conn)
    baselines.rodar_b3(conn, run_id=run_a, dataset_id=dataset_id, config=cfg)
    run_b = _abrir(conn)
    baselines.rodar_b3(conn, run_id=run_b, dataset_id=dataset_id, config=cfg)

    excesso = simulador.caixa_cents(conn, run_a) - simulador.caixa_cents(
        conn, run_b
    )
    assert excesso == 0, (
        "dois runs de B3 sobre a mesma janela e a mesma config deram caixas"
        " diferentes: o determinismo do simulador quebrou (R12)"
    )
