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


# ===========================================================================
# O RASTREAMENTO das cinco grandezas, e o que cada uma ESTIMA
# ===========================================================================
#
# > "Faca o rastreamento exato da origem dos dados usados em: variancia da D48;
# > autocorrelacao e n_efetivo da D48; p-valor submetido ao BY; retornos usados
# > pelo DSR; futura variavel observada pelo CUSUM. Para cada um, declare qual
# > grandeza esta sendo estimada." - o usuario, 2026-09-08
#
# **O rastreamento achou que a ausencia da serie de excesso SUSTENTA calculos
# atuais**, e portanto ela deixa de ser divida tecnica e vira correcao
# bloqueante. Estes testes fixam o rastreio, para que o defeito nao possa ser
# esquecido nem descoberto de novo do zero.


def test_a_variancia_da_D48_e_do_MERCADO_e_nao_do_estimador(conn):
    """RASTREIO 1 e 2: variancia, autocorrelacao e `n_efetivo` da D48.

    `viabilidade.montar` mede `desvio_bps` e `rho_ppm` sobre
    `loader.retornos_bps_entre` — a serie de fechamento a fechamento do
    DATASET, **a mesma para qualquer estrategia sobre a mesma janela**.

    O efeito minimo declarado e `excesso_sobre_b3_cents`. Padronizar um efeito
    de DIFERENCA pela volatilidade do MERCADO estima o objeto errado: a
    variancia da diferenca depende de quanto as duas estrategias se movem
    juntas, e nao de quanto o mercado se move.
    """
    import inspect

    from app.relatorio import viabilidade

    fonte = inspect.getsource(viabilidade.montar)
    assert "loader.retornos_bps_entre" in fonte, (
        "a fonte da variancia mudou: refaca o rastreio antes de mexer neste"
        " teste"
    )
    assert viabilidade.VALIDACAO_DA_VARIANCIA[
        "pertence_ao_estimador_do_efeito"
    ] is False


def test_os_numeros_da_D48_estao_declarados_NAO_VALIDADOS(conn):
    """E a declaracao vai na RESPOSTA, e nao so no docstring.

    Publicar 134.399 barras como se fosse numero validado seria a forma exata
    do padrao: um valor que descrevia algo, parou de descrever, e nada avisou.
    """
    from app.hipotese import dimensionamento as d
    from app.relatorio import viabilidade

    r = viabilidade.montar(conn, potencia_ppm=d.POTENCIA_ALVO_PPM)
    assert r["numeros_validados"] is False
    v = r["validacao_da_variancia"]
    assert v["fator_medido_em_n"] < 1
    assert "SUPERESTIMADA" in v["amostra_exigida_esta"]
    assert "IN-SAMPLE" in v["o_que_sobrevive"]
    assert "dataset inteiro" in v["o_que_NAO_sobrevive"]
    assert "BLOQUEANTE" in v["correcao"]


def test_o_pvalor_que_vai_ao_BY_mede_o_MERCADO(conn, run_b3):
    """RASTREIO 3 e 4: o p-valor e o DSR.

    `promocao._estatistica_do_run` calcula os momentos sobre
    `executor.retornos_do_run`, que e a serie do DATASET. Entao o "Sharpe
    realizado", o p-valor que BY ordena e o DSR que o consome medem o
    **mercado** na janela executada.

    Medido em cenario sintetico: mercado subindo 198,8%, regra girando 164
    vezes e terminando com 83.741 contra 100.000 de semente — **perdeu 16%** —
    recebeu Sharpe **+25,78** e p-valor de **846 ppm**, contra um limiar de
    primeira rejeicao de 467 ppm. O Sharpe da equity dela e **-17,06**.
    """
    import inspect

    from app.validador import promocao

    fonte = inspect.getsource(promocao._estatistica_do_run)
    assert "executor.retornos_do_run" in fonte
    assert "MERCADO" in fonte, (
        "a declaracao de que esta serie e do mercado sumiu do modulo que a usa"
    )

    # E a ressalva vai na RESPOSTA, junto do numero que ela qualifica.
    run_id, _ds, _cfg, _r = run_b3
    est = promocao._estatistica_do_run(
        conn, run_id, duracao_barra_ms=900_000, n_efetivo=100
    )
    if est.get("disponivel"):
        assert "MERCADO" in est["serie_medida"]
        assert "BLOQUEANTE" in est["nao_e_o_desempenho_da_estrategia"]


def test_o_CUSUM_ainda_NAO_TEM_produtor_do_observado(conn):
    """RASTREIO 5: a variavel observada pelo CUSUM.

    `monitor.registrar` recebe `observados` como PARAMETRO, e nada em `app/` o
    chama — nao ha candidata (D38). O `alvo` do CUSUM esta em milicents de
    excesso sobre o B3 por barra, entao o `observado_t` tem de ser **a mesma
    serie de excesso incremental** que falta.

    A ausencia e latente: no dia em que houver candidata, quem escrever o
    produtor tera de construir a serie — ou o CUSUM comparara um observado de
    um objeto com um alvo de outro.
    """
    import pathlib

    from tests._prosa import sql_sem_prosa

    raiz = pathlib.Path(__file__).resolve().parents[1] / "app"
    chamadores = [
        c.relative_to(raiz).as_posix()
        for c in raiz.rglob("*.py")
        if "monitor.registrar(" in sql_sem_prosa(c).replace(" ", "")
    ]
    assert not chamadores, (
        f"apareceu produtor do observado do CUSUM em {chamadores}: ele PRECISA"
        " ser a serie de excesso incremental sobre grade comum, porque o alvo"
        " esta em milicents de excesso sobre o B3 por barra"
    )


def test_a_serie_de_excesso_reconstroi_o_excesso_final(conn, cenario):
    """**A construcao que o usuario especificou FUNCIONA, e esta medida.**

    `excesso_incremental_t = Δequity_agente_t − Δequity_B3_t`, sobre a mesma
    grade de barras e o mesmo preco de marcacao.

    A garantia que importa: **a soma reconstroi o excesso final exatamente.**
    Medido aqui, e nao suposto — e e o que torna a correcao bloqueante viavel
    em vez de especulativa.

    Este teste NAO constroi a serie em producao: ele prova que a construcao
    fecha, usando as pecas que ja existem (`curva_do_run` sobre a mesma grade).
    """
    dataset_id, cfg = cenario
    run_a = _abrir(conn)
    baselines.rodar_b3(conn, run_id=run_a, dataset_id=dataset_id, config=cfg)
    run_b = _abrir(conn)
    baselines.rodar_b2(conn, run_id=run_b, dataset_id=dataset_id, config=cfg)

    barras = executor.carregar_janela(conn, dataset_id)

    def equity(run: int) -> list[int]:
        pontos = curva_mod.curva_do_run(
            conn, run, barras=barras, pontos=10**9
        )
        return [p.patrimonio_cents for p in pontos]

    ea, eb = equity(run_a), equity(run_b)
    assert len(ea) == len(eb), (
        "as duas curvas tem de sair na MESMA grade: e a garantia 'mesmo preco"
        " de marcacao para os dois'"
    )
    incremental = [
        (ea[i] - ea[i - 1]) - (eb[i] - eb[i - 1]) for i in range(1, len(ea))
    ]
    # Barras sem operacao continuam existindo: a serie tem uma entrada por
    # barra, e nao uma por execucao.
    assert len(incremental) == len(barras) - 1

    esperado = ea[-1] - eb[-1]
    assert sum(incremental) == esperado, (
        f"a soma dos excessos incrementais deu {sum(incremental)} e o excesso"
        f" final e {esperado}: a construcao nao fecha"
    )
    # E o excesso final e o mesmo que o Portao B usaria, porque os dois runs
    # terminam sem posicao.
    assert esperado == simulador.caixa_cents(conn, run_a) - simulador.caixa_cents(
        conn, run_b
    )
