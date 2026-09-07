"""Calibração do simulador: previsto contra observado (incremento 18).

ADR 0027 e ADR 0032. Os testes de `bootstrap` verificam **propriedades**, e o
que eu não verifico está dito no próprio módulo: não afirmo reproduzir tabela
numérica de Politis–White, porque não tenho o artigo à mão.
"""

from __future__ import annotations

import math
import random
import sqlite3

import pytest

from app.calibracao import bootstrap, observacao

# ===========================================================================
# Séries sintéticas, com dependência conhecida
# ===========================================================================


def ruido_branco(n: int, *, semente: int = 7) -> list[float]:
    r = random.Random(semente)
    return [r.gauss(0, 1) for _ in range(n)]


def ar1(n: int, rho: float, *, semente: int = 7) -> list[float]:
    """AR(1) com `rho` declarado. Tau teórico = (1+rho)/(1-rho)."""
    r = random.Random(semente)
    x = [r.gauss(0, 1)]
    for _ in range(n - 1):
        x.append(rho * x[-1] + r.gauss(0, math.sqrt(1 - rho ** 2)))
    return x


# ===========================================================================
# tau: o tempo de autocorrelação integrado
# ===========================================================================


def test_ruido_branco_tem_tau_perto_de_1():
    t = bootstrap.tau_hac(ruido_branco(2_000))
    assert 0.8 < t.tau_bruto < 1.3, t.tau_bruto
    assert t.tau >= 1.0, "o usado no dimensionamento nunca desce de 1"


def test_dependencia_positiva_levanta_tau():
    t = bootstrap.tau_hac(ar1(4_000, 0.5))
    # Teórico (1+0,5)/(1-0,5) = 3. A janela de Bartlett trunca e enviesa para
    # baixo, então o que se exige é a DIREÇÃO e a ordem de grandeza.
    assert 1.5 < t.tau < 4.0, t.tau
    assert t.tau > bootstrap.tau_hac(ruido_branco(4_000)).tau


def test_a_hipotese_AR1_e_declarada_E_verificada():
    """O ADR exige as duas coisas, e o usuário apontou a omissão.

    `(1+rho)/(1-rho)` é o caso AR(1) de tau, e usá-lo sem dizer isso foi
    omissão da derivação original. Aqui ele existe nomeado, e vem com o teste
    da própria hipótese: `rho_k` decai geometricamente?
    """
    t = bootstrap.tau_hac(ar1(4_000, 0.5))
    assert t.ar1_plausivel, t.desvio_max_geometrico
    assert 2.5 < t.tau_ar1 < 3.6, t.tau_ar1

    # Numa série sem estrutura AR(1), a hipótese não se sustenta - e o campo
    # diz isso em vez de o número sair como se valesse.
    alternada = [(-1.0) ** i + 0.01 * i for i in range(500)]
    assert not bootstrap.tau_hac(alternada).ar1_plausivel


def test_serie_constante_nao_divide_por_zero():
    """Desvio-padrão ZERO já fez um teste do incremento 17 passar pelo motivo
    errado. Aqui a série constante tem caminho próprio."""
    t = bootstrap.tau_hac([5.0] * 100)
    assert t.tau == 1.0


# ===========================================================================
# Comprimento de bloco: Politis–White
# ===========================================================================


def test_ruido_branco_pede_bloco_CURTO():
    b = bootstrap.comprimento_de_bloco(ruido_branco(1_000))
    assert 1 <= b <= 4, b


def test_dependencia_forte_pede_bloco_LONGO():
    """A propriedade que decide se a regra presta: mais persistência, mais bloco."""
    curto = bootstrap.comprimento_de_bloco(ruido_branco(2_000))
    longo = bootstrap.comprimento_de_bloco(ar1(2_000, 0.9))
    assert longo > curto, (longo, curto)
    assert longo >= 5, longo


def test_o_bloco_cresce_com_a_persistencia():
    blocos = [
        bootstrap.comprimento_de_bloco(ar1(2_000, rho))
        for rho in (0.0, 0.5, 0.9)
    ]
    assert blocos == sorted(blocos), blocos
    assert blocos[-1] > blocos[0]


def test_o_bloco_tem_teto_e_piso():
    assert bootstrap.comprimento_de_bloco([1.0, 2.0]) == 1
    b = bootstrap.comprimento_de_bloco(ar1(300, 0.99))
    assert b <= max(1, int(min(3 * math.sqrt(300), 300 / 3)))


# ===========================================================================
# O quantil e o limite inferior
# ===========================================================================


def test_quantil_tem_UMA_convencao():
    """Trocar de convenção no meio é o modo de falha silencioso do
    procedimento - a mesma lição da curtose bruta contra a excedente."""
    x = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert bootstrap.quantil(x, 0.0) == 0.0
    assert bootstrap.quantil(x, 1.0) == 4.0
    assert bootstrap.quantil(x, 0.5) == 2.0
    assert bootstrap.quantil(x, 0.10) == pytest.approx(0.4)


def test_o_limite_inferior_fica_ABAIXO_do_ponto():
    ic = bootstrap.limite_inferior_do_quantil(ruido_branco(500), 0.10)
    assert ic.limite_inferior < ic.ponto


def test_o_limite_e_REPRODUZIVEL():
    """Um IC que muda a cada chamada não é um IC - é um sorteio (regra 13)."""
    x = ar1(500, 0.6)
    a = bootstrap.limite_inferior_do_quantil(x, 0.10)
    b = bootstrap.limite_inferior_do_quantil(x, 0.10)
    assert a.limite_inferior == b.limite_inferior


def test_IGNORAR_A_DEPENDENCIA_APERTA_O_INTERVALO_NA_DIRECAO_DE_APROVAR():
    """O teste que justifica o block bootstrap existir.

    Reamostrar ponto a ponto (bloco = 1) destrói a dependência da série, e o
    intervalo sai **estreito demais** - com o limite inferior alto demais.
    Numa revalidação que exige `LB >= 0`, isso aprova simulador que não deveria
    passar.

    É o único lugar deste módulo onde uma escolha erra na direção de aprovar, e
    é por isso que o comprimento de bloco não é um parâmetro que alguém possa
    mexer depois de ver o número.
    """
    x = ar1(1_000, 0.9)
    com_dependencia = bootstrap.limite_inferior_do_quantil(x, 0.10)
    sem_dependencia = bootstrap.limite_inferior_do_quantil(x, 0.10, bloco=1)

    assert com_dependencia.bloco > 1
    assert sem_dependencia.limite_inferior > com_dependencia.limite_inferior, (
        "ignorar a dependência deveria produzir um piso ALTO demais"
    )


def test_serie_curta_demais_e_recusada_em_vez_de_estimada():
    with pytest.raises(bootstrap.SerieCurtaDemais):
        bootstrap.limite_inferior_do_quantil([1.0] * 9, 0.10)


def test_o_n_efetivo_desconta_a_dependencia():
    ic = bootstrap.limite_inferior_do_quantil(ar1(1_000, 0.8), 0.10)
    assert ic.n == 1_000
    assert ic.n_efetivo < 1_000, "dependência positiva reduz a amostra efetiva"


# ===========================================================================
# Dimensionamento: K = 2,92, e não 1,71
# ===========================================================================


def test_K_e_a_razao_de_VARIANCIAS_e_nao_a_de_erros_padrao():
    """Eu escrevi 1,71, que é a raiz de 2,92.

    A razão de erros-padrão aplicada onde vai a de variâncias subestimava a
    amostra em 71%. Fica como teste porque o erro estava numa CONTA, e conta
    errada não é pega relendo a frase.
    """
    assert bootstrap.K_QUANTIL_P10 == 2.92
    assert math.isclose(math.sqrt(2.92), 1.7088, abs_tol=1e-3)

    # `sigma_e` grande de propósito: `n` é arredondado para CIMA, e com `n`
    # pequeno o arredondamento domina a razão. Medir a propriedade onde o
    # arredondamento a esconde seria medir o `ceil`, não a fórmula.
    certo = bootstrap.tamanho_necessario(sigma_e=100.0, delta=0.5, tau=1.0)
    errado = bootstrap.tamanho_necessario(
        sigma_e=100.0, delta=0.5, tau=1.0, k=math.sqrt(2.92)
    )
    assert certo.n_efetivo / errado.n_efetivo == pytest.approx(1.709, abs=0.01)
    assert certo.n_efetivo == 448_699


def test_o_dimensionamento_multiplica_por_tau():
    a = bootstrap.tamanho_necessario(sigma_e=1.0, delta=0.5, tau=1.0)
    b = bootstrap.tamanho_necessario(sigma_e=1.0, delta=0.5, tau=3.0)
    assert b.n_bruto == 3 * a.n_bruto
    assert b.n_efetivo == a.n_efetivo, "tau move o bruto, não o efetivo"


def test_delta_menor_exige_amostra_MAIOR_ao_quadrado():
    a = bootstrap.tamanho_necessario(sigma_e=100.0, delta=1.0, tau=1.0)
    b = bootstrap.tamanho_necessario(sigma_e=100.0, delta=0.5, tau=1.0)
    assert b.n_efetivo == pytest.approx(4 * a.n_efetivo, rel=0.001)


def test_a_funcao_nao_conhece_calendario_nenhum():
    """E é assim que ela fica incapaz de negociar consigo mesma.

    O usuário retirou a possibilidade de relaxar `delta` "para caber na
    reserva". Não há parâmetro de horizonte aqui: quem não alcança o `n`
    permanece NÃO CALIBRADO, e isso vira condição de validade.
    """
    import inspect

    nomes = set(inspect.signature(bootstrap.tamanho_necessario).parameters)
    assert nomes == {"sigma_e", "delta", "tau", "z", "k"}
    assert not (nomes & {"dias", "horizonte", "reserva", "prazo"})


# ===========================================================================
# A observação: previsto contra observado
# ===========================================================================

GRADE = 900_000
T0 = 1_756_000_000_000 // GRADE * GRADE
CONTRATO = "bbo@1"
ORCAMENTO = 71_400          # US$ 714,00 em centavos — o giro da hipótese 41


@pytest.fixture
def cfg(conn: sqlite3.Connection):
    from app.config import service as config_service

    v = config_service.versao_atual(conn)
    assert v is not None
    return v


def semear(
    conn: sqlite3.Connection, n: int, *, abertura: int = 60_000_00000000,
    ask: int = 60_001_00000000, bid: int = 59_999_00000000,
    ask_qty: int = 7_00000000,
) -> None:
    """Barra fechada e amostra de BBO no MESMO instante, para `n` instantes."""
    from app.aovivo import bbo as m

    serie = m.Serie(venue="binance", symbol="BTCUSDT",
                    price_scale_exp=8, volume_scale_exp=8)
    amostras = []
    for i in range(n):
        t = T0 + i * GRADE
        conn.execute(
            "INSERT INTO stream_bar (venue, symbol, timeframe, open_time_ms,"
            " open, high, low, close, volume, quote_volume, trades,"
            " interval_ms, price_scale_exp, volume_scale_exp, recebido_em,"
            " origem) VALUES ('binance','BTCUSDT','15m',?,?,?,?,?,1,1,1,"
            "?,8,8,'x','ao_vivo')",
            (t, abertura, abertura + 1000, abertura - 1000, abertura, GRADE),
        )
        corrigido = t - 500
        amostras.append(m.Amostra(
            t_grid_ms=t, disponivel=True,
            bid=bid, bid_qty=5_00000000, ask=ask, ask_qty=ask_qty, u=i + 1,
            received_at_ms=corrigido + 2_450,
            received_at_corrigido_ms=corrigido,
            sampled_at_ms=corrigido, defasagem_ms=500,
            offset_us=-2_450_000, rtt_us=12_000, incerteza_residual_us=6_000,
            relogio_medido_em_ms=corrigido - 30_000,
        ))
    m.receber(conn, serie, CONTRATO, amostras)


def test_E1_e_E2_medem_coisas_DIFERENTES(conn, cfg):
    """E1 já contém meio spread; E2 é o alvo.

    Calibrar `spread_bps` a partir de E1 e continuar somando `spread_bps` no
    simulador contaria o spread DUAS VEZES, e nada acusaria. Foi o usuário
    quem separou os dois ao fechar a D45.
    """
    semear(conn, 3)
    observacao.registrar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, orcamento_cents=ORCAMENTO,
        lados=("compra",),
    )
    linha = conn.execute(
        "SELECT abertura, bbo_ask, p_exec_previsto, e1_mili_bps, e2_mili_bps"
        "  FROM calibracao_observacao WHERE lado = 'compra' LIMIT 1"
    ).fetchone()

    esperado_e1 = observacao._mili_bps(
        int(linha["bbo_ask"]) - int(linha["abertura"]), int(linha["abertura"])
    )
    esperado_e2 = observacao._mili_bps(
        int(linha["p_exec_previsto"]) - int(linha["bbo_ask"]),
        int(linha["abertura"]),
    )
    assert int(linha["e1_mili_bps"]) == esperado_e1
    assert int(linha["e2_mili_bps"]) == esperado_e2
    assert esperado_e1 != esperado_e2, "se fossem iguais, o teste não separaria nada"


def test_a_previsao_usa_o_NUCLEO_COMPARTILHADO_do_simulador(conn, cfg):
    """Reimplementar a fórmula faria a calibração medir a distância entre duas
    implementações nossas, em vez do erro do simulador."""
    from app.dataset.loader import BarraCarregada
    from app.simulador.execucao import preco_executado, preco_referencia

    abertura = 60_000_00000000
    barra = BarraCarregada(0, 0, abertura, abertura, abertura, abertura, 0, 0, 0)
    esperado = preco_executado(
        preco_referencia(barra, "compra", cfg.config), "compra", cfg.config
    )
    assert observacao.prever(abertura, "compra", cfg.config) == esperado


def test_a_direcao_do_pessimismo_e_p10_MAIOR_QUE_ZERO(conn, cfg):
    """Na compra pagamos o ask. Prever menos que o ask é otimismo, e um
    simulador otimista transforma estratégia ruim em estratégia aprovada."""
    semear(conn, 3, ask=60_006_00000000)
    observacao.registrar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, orcamento_cents=ORCAMENTO,
        lados=("compra",),
    )
    e2 = observacao.serie_e2(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        lado="compra", config_version_id=cfg.id,
    )
    assert e2 and all(v > 0 for v in e2), f"simulador otimista: {e2}"


def test_a_VENDA_e_simetrica(conn, cfg):
    """`E2_venda = bid - previsto`: pessimismo é prever receber no máximo o bid."""
    semear(conn, 3)
    observacao.registrar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, orcamento_cents=ORCAMENTO,
    )
    linha = conn.execute(
        "SELECT abertura, bbo_bid, p_exec_previsto, e2_mili_bps"
        "  FROM calibracao_observacao WHERE lado = 'venda' LIMIT 1"
    ).fetchone()
    assert int(linha["p_exec_previsto"]) < int(linha["bbo_bid"]), (
        "na venda o previsto tem de ficar ABAIXO do bid"
    )
    assert int(linha["e2_mili_bps"]) == observacao._mili_bps(
        int(linha["bbo_bid"]) - int(linha["p_exec_previsto"]),
        int(linha["abertura"]),
    )


def test_so_ha_observacao_quando_HA_BARRA_E_COTACAO_do_mesmo_instante(conn, cfg):
    """O JOIN é o que torna a comparação honesta."""
    from app.aovivo import bbo as m

    semear(conn, 3)
    serie = m.Serie(venue="binance", symbol="BTCUSDT",
                    price_scale_exp=8, volume_scale_exp=8)
    t = T0 + 9 * GRADE
    corrigido = t - 500
    m.receber(conn, serie, CONTRATO, [m.Amostra(
        t_grid_ms=t, disponivel=True, bid=1, bid_qty=1, ask=2, ask_qty=1, u=99,
        received_at_ms=corrigido + 2_450, received_at_corrigido_ms=corrigido,
        sampled_at_ms=corrigido, defasagem_ms=500, offset_us=-2_450_000,
        rtt_us=1, incerteza_residual_us=1, relogio_medido_em_ms=corrigido - 1,
    )])

    r = observacao.registrar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, orcamento_cents=ORCAMENTO,
        lados=("compra",),
    )
    assert r["instantes_pareados"] == 3, "a amostra sem barra não pode parear"


def test_a_referencia_e_MERCADO_e_nunca_resultado_simulado(conn, cfg):
    """R63. Comparar simulador com simulador daria erro zero por construção, e
    o número pareceria excelente."""
    from pathlib import Path

    from tests._prosa import sql_sem_prosa

    import re

    sql = sql_sem_prosa(Path("app/calibracao/observacao.py"))

    # A guarda mira `FROM tabela` / `JOIN tabela`, e não a palavra solta: a
    # primeira versão procurava a substring `execution` e acusava
    # `execution_reference`, que é CAMPO DE CONFIG. Guarda larga demais mente
    # na direção de dar trabalho - e este projeto já registrou as duas
    # direções, a estreita no regex do export e agora a larga aqui.
    for tabela in ("execution", "baseline_result", "hypothesis", "agent_event"):
        padrao = re.compile(rf"\b(from|join)\s+{tabela}\b", re.IGNORECASE)
        assert not padrao.search(sql), (
            f"a calibração lê a tabela {tabela!r}: a referência tem de ser "
            f"mercado observado, e nunca outro resultado simulado"
        )

    # E não-vacuidade: a guarda tem de casar a forma que procura.
    assert re.search(r"\b(from|join)\s+execution\b",
                     "SELECT x FROM execution WHERE 1", re.IGNORECASE)

    assert re.search(r"\bfrom\s+bbo_amostra\b", sql, re.IGNORECASE)
    assert re.search(r"\bjoin\s+stream_bar\b", sql, re.IGNORECASE)


def test_registrar_e_IDEMPOTENTE(conn, cfg):
    semear(conn, 3)
    kw = dict(contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
              config=cfg.config, config_version_id=cfg.id,
              orcamento_cents=ORCAMENTO, lados=("compra",))
    a = observacao.registrar(conn, **kw)
    b = observacao.registrar(conn, **kw)
    assert (a["gravadas"], a["repetidas"]) == (3, 0)
    assert (b["gravadas"], b["repetidas"]) == (0, 3)


def test_a_observacao_e_apenas_por_ACRESCIMO(conn, cfg):
    """Reescrever uma previsão depois de ver o mercado é a definição de
    ajustar a régua olhando o resultado."""
    semear(conn, 2)
    observacao.registrar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id,
        orcamento_cents=ORCAMENTO, lados=("compra",),
    )
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("UPDATE calibracao_observacao SET e2_mili_bps = 0")
    assert "olhando o resultado" in str(e.value)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM calibracao_observacao")


def test_nocional_alem_do_TOPO_sai_da_estatistica_sem_sumir_do_registro(conn, cfg):
    """ADR 0027: o preço do BBO só vale para nocional <= o tamanho cotado.

    Conferido observação a observação, nunca assumido.
    """
    semear(conn, 3, ask_qty=1)
    observacao.registrar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id,
        orcamento_cents=ORCAMENTO, lados=("compra",),
    )
    total = conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao"
    ).fetchone()[0]
    assert total == 3, "a observação continua GRAVADA"

    dentro = conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao WHERE dentro_do_l1 = 1"
    ).fetchone()[0]
    assert dentro == 0

    serie = observacao.serie_e2(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        lado="compra", config_version_id=cfg.id,
    )
    assert serie == [], "e sai da estatística"


def test_config_incompativel_com_o_contrato_NAO_MEDE_NADA(conn, cfg):
    """Requisito 5 do ADR 0032, aplicado no ponto que importa."""
    semear(conn, 3)
    outra = cfg.config.model_copy(update={"execution_reference": "limite_adverso"})
    with pytest.raises(observacao.ContratoIncompativel) as e:
        observacao.registrar(
            conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
            config=outra, config_version_id=cfg.id, orcamento_cents=ORCAMENTO,
        )
    assert "execution_reference" in str(e.value)
    assert conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao"
    ).fetchone()[0] == 0


# ===========================================================================
# O shadow do B3: a recusa é ESTRUTURAL
# ===========================================================================


def semear_barras(conn: sqlite3.Connection, precos: list[int]) -> None:
    """Só barras, sem BBO — o shadow lê `stream_bar`."""
    for i, p in enumerate(precos):
        t = T0 + i * GRADE
        conn.execute(
            "INSERT INTO stream_bar (venue, symbol, timeframe, open_time_ms,"
            " open, high, low, close, volume, quote_volume, trades,"
            " interval_ms, price_scale_exp, volume_scale_exp, recebido_em,"
            " origem) VALUES ('binance','BTCUSDT','15m',?,?,?,?,?,1,1,1,"
            "?,8,8,'x','ao_vivo')",
            (t, p, p + 1000, p - 1000, p, GRADE),
        )


def rampa(n: int, base: int = 60_000_00000000) -> list[int]:
    """Sobe e depois desce, para o cruzamento 20/50 gerar os dois sinais."""
    meio = n // 2
    subida = [base + i * 10_00000000 for i in range(meio)]
    descida = [subida[-1] - i * 10_00000000 for i in range(n - meio)]
    return subida + descida


def test_o_shadow_NAO_RECEBE_REGRA(conn, cfg):
    """A recusa de §11.2.1 é por CONSTRUÇÃO, e não por checagem.

    Uma checagem protegeria enquanto ninguém a removesse, e §8.5.1 já disse
    que garantia que depende de boa vontade já foi violada. Aqui não existe
    argumento que faça outra coisa entrar - é o mesmo desenho do
    `acesso = 'agente'` literal no SQL do incremento 9.
    """
    import inspect

    from app.calibracao import shadow

    for fn in (shadow.rodar, shadow.ordens_do_b3):
        nomes = set(inspect.signature(fn).parameters)
        assert not (nomes & {"regra", "rule", "rule_id", "regras", "hipotese"}), (
            f"{fn.__name__} aceita regra: a 0C roda APENAS o B3"
        )


def test_a_rota_tambem_nao_tem_campo_de_regra(conn):
    """Um campo `regra` no corpo, ainda que validado, tornaria "roda apenas o
    B3" uma checagem em vez de uma propriedade."""
    from app.api.rotas.calibracao import PedidoDeCalibracao

    campos = set(PedidoDeCalibracao.model_fields)
    assert not (campos & {"regra", "rule", "rule_id", "hipotese", "params"})
    assert campos == {"venue", "symbol", "contrato", "de_ms", "ate_ms_exclusive"}


def test_o_shadow_deriva_o_B3_da_config_e_CONGELA(conn, cfg):
    """Tunar o B3 depois de ver o resultado destrói o grupo de controle - e a
    partir do congelamento isso é impossível, não desaconselhado."""
    from app.calibracao import shadow
    from app.regra import registro

    semear_barras(conn, rampa(120))
    r = shadow.rodar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id,
    )
    assert registro.esta_congelada(conn, r["rule_id"])
    assert f"{cfg.config.b3_fast}/{cfg.config.b3_slow}" in r["regra"]


def test_a_ordem_entra_na_barra_SEGUINTE_ao_sinal(conn, cfg):
    """`latency_bars = 1`: o sinal fecha na barra `i` e a ordem entra na
    abertura da barra `i+1`. É o que o contrato `bbo@1` amarra."""
    from app.calibracao import shadow

    semear_barras(conn, rampa(120))
    shadow.rodar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id,
    )
    linhas = conn.execute(
        "SELECT t_grid_ms, sinal_em_ms FROM shadow_ordem ORDER BY t_grid_ms"
    ).fetchall()
    assert linhas, "a rampa deveria cruzar as médias"
    for l in linhas:
        assert int(l["t_grid_ms"]) - int(l["sinal_em_ms"]) == (
            cfg.config.latency_bars * GRADE
        )


def test_o_preco_previsto_da_ordem_e_o_do_NUCLEO_do_simulador(conn, cfg):
    from app.calibracao import observacao as obs
    from app.calibracao import shadow

    semear_barras(conn, rampa(120))
    shadow.rodar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id,
    )
    l = conn.execute(
        "SELECT lado, abertura, p_exec_previsto FROM shadow_ordem LIMIT 1"
    ).fetchone()
    assert int(l["p_exec_previsto"]) == obs.prever(
        int(l["abertura"]), str(l["lado"]), cfg.config
    )


def test_perguntar_o_que_o_B3_FARIA_nao_grava_nada(conn, cfg):
    """Efeito colateral numa pergunta é como a fronteira do período de
    calibração se move sem ninguém decidir."""
    from app.calibracao import shadow

    semear_barras(conn, rampa(120))
    barras = shadow.barras_do_fluxo(
        conn, venue="binance", symbol="BTCUSDT", timeframe="15m"
    )
    ordens = shadow.ordens_do_b3(barras, cfg.config)
    assert ordens
    assert conn.execute("SELECT COUNT(*) FROM shadow_ordem").fetchone()[0] == 0


def test_o_shadow_e_IDEMPOTENTE(conn, cfg):
    from app.calibracao import shadow

    semear_barras(conn, rampa(120))
    kw = dict(contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
              config=cfg.config, config_version_id=cfg.id)
    a = shadow.rodar(conn, **kw)
    b = shadow.rodar(conn, **kw)
    assert a["gravadas"] > 0 and b["gravadas"] == 0
    assert b["repetidas"] == a["gravadas"]


def test_a_ordem_hipotetica_e_apenas_por_ACRESCIMO(conn, cfg):
    from app.calibracao import shadow

    semear_barras(conn, rampa(120))
    shadow.rodar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id,
    )
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("UPDATE shadow_ordem SET p_exec_previsto = 1")
    assert "sabendo o resultado" in str(e.value)
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("DELETE FROM shadow_ordem")
    assert "giro medido" in str(e.value)


def test_barras_de_menos_e_RECUSA_e_nao_lista_vazia(conn, cfg):
    """Lista vazia seria lida como "o B3 não operou", que é outra coisa."""
    from app.calibracao import shadow

    # Lista literal: `rampa(1)` estourava no próprio helper, e um helper que
    # quebra antes do código sob teste mede o helper.
    semear_barras(conn, [60_000_00000000])
    with pytest.raises(shadow.SemBarras):
        shadow.rodar(
            conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
            config=cfg.config, config_version_id=cfg.id,
        )


def test_o_resumo_separa_ordem_de_ordem_COMPARAVEL(conn, cfg):
    """O JOIN é quem responde. Uma coluna duplicaria estado que já existe, e
    duas fontes sobre o mesmo fato divergem (regra 16)."""
    from app.calibracao import shadow

    semear_barras(conn, rampa(120))
    shadow.rodar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id,
    )
    r = shadow.resumo(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config_version_id=cfg.id,
    )
    assert r["ordens"] > 0
    assert r["comparaveis"] == 0, "não há BBO: nada é comparável ainda"
    assert r["sem_mercado_observado"] == r["ordens"]


def test_o_shadow_le_MERCADO_e_nunca_resultado_simulado(conn, cfg):
    """R63, do lado do shadow."""
    import re
    from pathlib import Path

    from tests._prosa import sql_sem_prosa

    sql = sql_sem_prosa(Path("app/calibracao/shadow.py"))
    for tabela in ("execution", "baseline_result", "hypothesis"):
        assert not re.search(rf"\b(from|join)\s+{tabela}\b", sql, re.IGNORECASE)
    assert re.search(r"\bfrom\s+stream_bar\b", sql, re.IGNORECASE)


def test_config_incompativel_nao_roda_shadow(conn, cfg):
    from app.calibracao import observacao as obs
    from app.calibracao import shadow

    semear_barras(conn, rampa(120))
    outra = cfg.config.model_copy(update={"latency_bars": 3})
    with pytest.raises(obs.ContratoIncompativel):
        shadow.rodar(
            conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
            config=outra, config_version_id=cfg.id,
        )
    assert conn.execute("SELECT COUNT(*) FROM shadow_ordem").fetchone()[0] == 0


# ===========================================================================
# A rota
# ===========================================================================

CABECALHO = {"Authorization": "Bearer token-de-teste-0a"}


def test_a_rota_declara_o_LIMITE_no_proprio_resultado(client):
    """R65. O limite vai no resultado, e não numa nota de rodapé do relatório."""
    r = client.get("/api/calibracao", headers=CABECALHO)
    assert r.status_code == 200
    limite = r.json()["limite"]
    assert "TAKER" in limite and "maker" in limite
    assert "Fase 3" in limite


def test_a_rota_NAO_publica_p10_com_o_piloto_ABERTO(client):
    """Publicar o quantil de um piloto em andamento é olhar o resultado antes
    de o período declarado terminar - a quinta pergunta do teste de escopo."""
    r = client.get("/api/calibracao", headers=CABECALHO)
    corpo = r.json()
    assert corpo["piloto"]["estado"] == "acumulando"
    assert corpo["erro_de_execucao"]["estado"] == "piloto_em_andamento"
    assert "p10_mili_bps" not in corpo["erro_de_execucao"]


def test_a_rota_recusa_corpo_com_campo_de_regra(client):
    """`extra: forbid` transforma a tentativa em 422, e não em campo ignorado."""
    r = client.post(
        "/api/calibracao/rodar",
        headers=CABECALHO,
        json={"venue": "binance", "symbol": "BTCUSDT",
              "regra": {"familia": "cruzamento_medias"}},
    )
    assert r.status_code == 422
