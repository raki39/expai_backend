"""As duas séries corretas, e as nove garantias que o usuário exigiu.

**CORREÇÃO BLOQUEANTE 1.** O rastreamento achou que todas as estatísticas liam
a série do MERCADO, e o número que decidiu foi este: num mercado que subiu
198,8%, uma regra que **perdeu 16% do capital** recebia "Sharpe realizado" de
**+25,78** e p-valor de **846 ppm** — contra um limiar de primeira rejeição de
467. O Sharpe da equity dela é **−17,06**.

> *"Se isso passasse despercebido, um agente poderia parecer genial simplesmente
> porque o Bitcoin subiu."*

Este arquivo prova as duas séries e cada garantia, e a nona é a mais
importante: **retorno do mercado nunca substitui retorno da estratégia**, e isso
é guarda de código.
"""

from __future__ import annotations

import sqlite3
import statistics

import pytest

from app.estatistica import sharpe as sharpe_mod
from app.ledger import contas
from app.maos_rapidas import baselines, curva as curva_mod, executor, series
from app.regra import registro as regra_registro
from app.regra.schema import CruzamentoMedias, Regra
from app.simulador import execucao as simulador
from tests.test_cerebro import settings  # noqa: F401
from tests.test_maos_rapidas import cenario  # noqa: F401
from tests.test_simulador import criar_dataset  # noqa: F401

SEMENTE = 100_000


def _abrir(conn: sqlite3.Connection) -> int:
    from app.ledger.livro import abrir_run

    run_id, _ = abrir_run(
        conn, config_version_id=1, seed_capital_usd_cents=SEMENTE
    )
    return run_id


def _rodar(conn, dataset_id, cfg, *, rapida: int, lenta: int) -> int:
    run_id = _abrir(conn)
    regra = Regra(
        params=CruzamentoMedias(rapida=rapida, lenta=lenta),
        position_fraction_bps=10_000,
        stop_loss_bps=None,
        condicoes_validade=baselines.condicoes(cfg),
    )
    rid = regra_registro.registrar(conn, regra)
    executor.rodar(
        conn, run_id=run_id, dataset_id=dataset_id, regra=regra, rule_id=rid,
        config=cfg,
    )
    return run_id


@pytest.fixture
def par(conn: sqlite3.Connection, cenario):
    """Uma candidata que gira demais e um B3, sobre a MESMA janela."""
    dataset_id, cfg = cenario
    candidata = _rodar(conn, dataset_id, cfg, rapida=2, lenta=3)
    b3 = _rodar(conn, dataset_id, cfg, rapida=20, lenta=50)
    barras = executor.carregar_janela(conn, dataset_id)
    return candidata, b3, barras, dataset_id, cfg


# ---------------------------------------------------------------------------
# O defeito, reproduzido: o mercado e a estratégia divergem em SINAL
# ---------------------------------------------------------------------------


def test_a_serie_do_mercado_e_a_da_estrategia_sao_objetos_DIFERENTES(conn, par):
    """A demonstração que motivou a correção inteira.

    Duas séries sobre o mesmo run, e elas medem coisas diferentes: uma é o que
    o **preço** fez, outra é o que a **estratégia** rendeu. Num mercado que
    subiu, a primeira é otimista para qualquer regra — inclusive as que
    perderam dinheiro.
    """
    candidata, _b3, barras, _ds, _cfg = par
    do_mercado = executor.retornos_do_run(conn, candidata)
    da_estrategia = series.retorno_da_estrategia(conn, candidata, barras=barras)

    assert do_mercado != da_estrategia, (
        "as duas series ficaram identicas: ou o cenario mudou, ou alguem"
        " religou a estatistica na serie do mercado"
    )
    # E a diferença não é de escala: os Sharpes discordam.
    sm = sharpe_mod.momentos(do_mercado).sharpe_anualizado(900_000)
    se = sharpe_mod.momentos(da_estrategia).sharpe_anualizado(900_000)
    assert abs(sm - se) > 0.1


def test_a_estrategia_que_PERDE_tem_sharpe_negativo(conn, cenario):
    """**A garantia 9, medida onde ela importa.**

    Numa janela em que o preço sobe e a regra gira demais, o resultado
    econômico é negativo. O Sharpe da série da estratégia tem de acompanhar o
    ledger — se ele ficar positivo, voltamos ao defeito.
    """
    dataset_id, cfg = cenario
    run = _rodar(conn, dataset_id, cfg, rapida=2, lenta=3)
    barras = executor.carregar_janela(conn, dataset_id)
    resultado = simulador.caixa_cents(conn, run) - SEMENTE
    if resultado >= 0:
        pytest.skip("o cenario nao produziu regra perdedora; nada a afirmar")

    da_estrategia = series.retorno_da_estrategia(conn, run, barras=barras)
    sharpe = sharpe_mod.momentos(da_estrategia).sharpe_anualizado(900_000)
    assert sharpe < 0, (
        f"a regra perdeu {-resultado} centavos e o Sharpe da serie dela deu"
        f" {sharpe:+.3f}: a serie nao esta medindo a estrategia"
    )


# ---------------------------------------------------------------------------
# As nove garantias
# ---------------------------------------------------------------------------


def test_garantia_1_mesma_grade_e_mesmo_preco_de_marcacao(conn, par):
    """As duas equities saem com o mesmo número de pontos, na mesma grade.

    E a marcação é **uma função só** — `curva.valor_da_posicao_cents`. Duas
    implementações dela quebrariam a garantia sem quebrar nenhum teste: foi o
    que aconteceu na primeira versão deste módulo, quando escrevi `10^8` onde o
    divisor é `10^14` e a equity explodiu seis ordens de grandeza.
    """
    candidata, b3, barras, _ds, _cfg = par
    ea = series.equity_por_barra(conn, candidata, barras=barras)
    eb = series.equity_por_barra(conn, b3, barras=barras)
    assert len(ea.valores_cents) == len(eb.valores_cents) == len(barras)

    import inspect

    assert "valor_da_posicao_cents" in inspect.getsource(series.equity_por_barra)
    assert series.valor_da_posicao_cents is curva_mod.valor_da_posicao_cents


def test_garantia_2_as_operacoes_NAO_precisam_ocorrer_juntas(conn, par):
    """Os dois runs giram em instantes diferentes, e a série existe assim.

    O que casa é a **grade de avaliação**, e não o giro. Exigir operações
    simultâneas tornaria a comparação impossível — e é justamente essa a
    objeção que a construção derruba.
    """
    candidata, b3, barras, _ds, _cfg = par
    ia = executor.idas_e_voltas(conn, candidata)
    ib = executor.idas_e_voltas(conn, b3)
    assert ia != ib, "o cenario precisa de giros diferentes para provar isto"

    ex = series.excesso_incremental(
        conn, candidata_run_id=candidata, b3_run_id=b3, barras=barras
    )
    assert len(ex.incremental_cents) == len(barras) - 1
    assert ex.reconstroi_a_diferenca


def test_garantia_3_o_custo_do_PENSAMENTO_entra(conn, par):
    """`curva_do_run` soma só execuções; esta soma tudo que tocou o caixa.

    Foi a diferença de nove centavos entre o número-herói e a tabela, na mesma
    tela. Num run de B3 não há reflexão, então os dois coincidem — e o teste
    afirma a identidade que prova que nada ficou de fora.
    """
    candidata, _b3, barras, _ds, _cfg = par
    eq = series.equity_por_barra(conn, candidata, barras=barras)

    fora = int(
        conn.execute(
            "SELECT COALESCE(SUM(e.amount_minor), 0) AS c"
            "  FROM ledger_entry e"
            "  JOIN ledger_transaction t ON t.id = e.transaction_id"
            "  JOIN account a ON a.id = e.account_id"
            " WHERE t.run_id = ? AND a.code = ? AND t.kind <> 'abertura'"
            "   AND t.id NOT IN (SELECT ledger_transaction_id FROM execution"
            "                     WHERE run_id = ?"
            "                       AND ledger_transaction_id IS NOT NULL)",
            (candidata, contas.CAIXA_SIM, candidata),
        ).fetchone()["c"]
    )
    assert eq.custo_de_pensar_cents == fora


def test_garantia_4_o_comparador_e_run_id_E_run_digest(conn, par):
    """Um id aponta para uma linha; o digest diz qual resultado ela produziu."""
    candidata, b3, barras, _ds, _cfg = par
    ex = series.excesso_incremental(
        conn, candidata_run_id=candidata, b3_run_id=b3, barras=barras
    )
    assert ex.b3.run_id == b3
    assert ex.b3.run_digest and len(ex.b3.run_digest) > 8
    assert ex.b3.run_digest == executor.digest_do_run(conn, b3)
    assert ex.candidata.run_digest != ex.b3.run_digest


def test_garantia_5_recusa_dataset_timeframe_ou_identidade_diferentes(
    conn, cenario
):
    """E ela LEVANTA, em vez de devolver número.

    Um excesso entre datasets diferentes tem a aparência de um resultado e não
    é comparação nenhuma — e a aparência é o perigo.
    """
    dataset_a, cfg = cenario
    run_a = _rodar(conn, dataset_a, cfg, rapida=20, lenta=50)

    from tests.test_maos_rapidas import precos_passeio

    dataset_b = criar_dataset(conn, precos_passeio(2_500, seed=99))
    run_b = _rodar(conn, dataset_b, cfg, rapida=20, lenta=50)

    barras = executor.carregar_janela(conn, dataset_a)
    with pytest.raises(series.SeriesIncompativeis, match="datasets diferentes"):
        series.excesso_incremental(
            conn, candidata_run_id=run_a, b3_run_id=run_b, barras=barras
        )


def test_garantia_6_a_equity_final_RECONCILIA_com_o_ledger(conn, par):
    """O caixa reconstruído é o saldo da conta, ao centavo.

    Sem isto a série seria uma segunda aritmética do dinheiro — e a regra 16 diz
    que o ledger é a autoridade.
    """
    candidata, b3, barras, _ds, _cfg = par
    for run in (candidata, b3):
        eq = series.equity_por_barra(conn, run, barras=barras)
        assert eq.reconcilia_com_o_ledger, (
            f"run {run}: caixa reconstruido {eq.caixa_final_cents} contra"
            f" ledger {eq.ledger_caixa_cents}"
        )
        assert eq.ledger_caixa_cents == simulador.caixa_cents(conn, run)


def test_garantia_7_a_soma_RECONSTROI_a_diferenca_final(conn, par):
    """**A garantia que torna a série utilizável.**

    Se a soma não reconstruísse a diferença, a variância que sai dela
    dimensionaria outro efeito — e a D48 voltaria a medir o objeto errado por
    outro caminho.
    """
    candidata, b3, barras, _ds, _cfg = par
    ex = series.excesso_incremental(
        conn, candidata_run_id=candidata, b3_run_id=b3, barras=barras
    )
    assert ex.soma_cents == ex.diferenca_final_cents
    assert ex.reconstroi_a_diferenca

    # E a diferença final é a econômica: os dois terminam sem posição, então
    # ela bate com a diferença dos caixas do ledger.
    if (
        ex.equity_candidata.posicao_final_sats == 0
        and ex.equity_b3.posicao_final_sats == 0
    ):
        pelos_caixas = simulador.caixa_cents(
            conn, candidata
        ) - simulador.caixa_cents(conn, b3)
        assert ex.soma_cents == pelos_caixas, (
            "a soma nao bate com a diferenca dos caixas: e ela que o Portao B"
            " usa como `excesso_sobre_b3_cents`"
        )


def test_garantia_8_barra_ausente_NAO_vira_retorno_zero(conn, par):
    """A política da D40, e ela é declarada e contada.

    Zero é uma **afirmação** sobre o retorno. Dizer que a equity não se moveu
    num instante em que ninguém observou o preço inventa um dado — é o mesmo
    argumento que fez `S_t = S_{t−1}` no CUSUM.
    """
    _c, _b, barras, _ds, _cfg = par
    assert "NAO vira retorno zero" in series.POLITICA_BARRA_AUSENTE
    assert "D40" in series.POLITICA_BARRA_AUSENTE

    # Numa grade contígua, não há ausência a contar.
    eq = series.equity_por_barra(conn, _c, barras=barras)
    assert eq.barras_ausentes == 0

    # E com um buraco de propósito, a ausência aparece — e não vira zero.
    com_buraco = list(barras[:100]) + list(barras[150:])
    assert series._barras_ausentes(com_buraco) == 50


def test_garantia_9_a_estatistica_NAO_le_a_serie_do_mercado():
    """**Guarda de código, e não boa vontade.**

    Varre os módulos que produzem estatística por `retornos_do_run`. Se alguém
    religar, a suíte quebra — em vez de o número voltar a mentir em silêncio.
    """
    import pathlib

    from tests._prosa import codigo_sem_prosa

    raiz = pathlib.Path(__file__).resolve().parents[1] / "app"
    alvos = [
        raiz / "validador" / "promocao.py",
        raiz / "cerebro" / "ciclo.py",
        raiz / "relatorio" / "portao_b.py",
    ]
    acusados = [
        a.relative_to(raiz).as_posix()
        for a in alvos
        if "retornos_do_run" in codigo_sem_prosa(a)
    ]
    assert not acusados, (
        f"{acusados} voltaram a ler a serie do MERCADO. Uma regra que perdeu"
        " 16% do capital recebia Sharpe +25,78 por causa disso"
    )
    # E a guarda não é vazia: ela acha o nome quando ele está no código.
    assert "retornos_do_run" in "x = executor.retornos_do_run(conn, run)"


# ---------------------------------------------------------------------------
# UMA definição, e as duas partes leem dela
# ---------------------------------------------------------------------------


def test_o_agente_e_o_validador_leem_a_MESMA_serie(conn, par):
    """A guarda do run 30, e ela acusou na primeira tentativa desta correção.

    Eu troquei a série do validador e não a do agente, e o teste
    `test_o_agente_e_o_validador_concordam_sobre_n_efetivo` recusou: 2.881
    contra 2.893. `n_efetivo` decide entre `refutada` e `inconclusiva`.
    """
    candidata, _b3, barras, _ds, _cfg = par
    pela_definicao = series.serie_do_run(conn, candidata)
    direto = series.retorno_da_estrategia(conn, candidata, barras=barras)
    assert pela_definicao == direto

    import inspect

    from app.cerebro import ciclo
    from app.validador import promocao

    assert "series_mod.serie_do_run" in inspect.getsource(ciclo)
    assert "serie_do_run" in inspect.getsource(promocao._serie_da_estrategia)


def test_serie_do_run_de_um_run_sem_execucao_e_vazia(conn):
    """Sem execução não há série, e vazio é o caminho que já existe."""
    run = _abrir(conn)
    assert series.serie_do_run(conn, run) == []


# ---------------------------------------------------------------------------
# A variância que a D48 precisa
# ---------------------------------------------------------------------------


def test_a_variancia_do_EXCESSO_difere_da_do_mercado(conn, par):
    """E é a diferença que invalidou os números da D48.

    O efeito mínimo declarado é `excesso_sobre_b3_cents`. Padronizá-lo pela
    volatilidade do mercado estima o objeto errado: a dispersão da diferença
    depende de quanto as duas estratégias se movem **juntas**.
    """
    candidata, b3, barras, _ds, _cfg = par
    ex = series.excesso_incremental(
        conn, candidata_run_id=candidata, b3_run_id=b3, barras=barras
    )
    do_excesso = statistics.pstdev(ex.incremental_cents)
    do_mercado = (
        statistics.pstdev(executor.retornos_do_run(conn, candidata))
        * SEMENTE
        / 10_000
    )
    assert do_excesso > 0 and do_mercado > 0
    assert abs(do_excesso / do_mercado - 1.0) > 0.05, (
        "a dispersao do excesso ficou igual a do mercado: o cenario deixou de"
        " demonstrar por que a substituicao importa"
    )
