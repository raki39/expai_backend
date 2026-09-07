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

from app.calibracao import bootstrap, observacao, piloto

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


# ===========================================================================
# O AJUSTE. Uma garantia do usuário por teste, e os nomes dizem qual.
# ===========================================================================

ABERTURA = 60_000_00000000

# Com `spread_bps=1`, `slippage_bps=2`, `penalty_bps=1`, o simulador soma
# (0,5 + 2 + 1) = 3,5 bps sobre a abertura:
#
#     p_exec_previsto = ceil(60_000 × 1,00035) = 60_021
#
# Então o `ask` escolhido controla `E2 = previsto − ask` diretamente, e é assim
# que estes testes põem `p10` de um lado ou do outro de δ.
ASK_PESSIMISTA = 60_001_00000000    # E2 ≈ 3.333 mili-bps, muito acima de δ
ASK_JUSTO = 60_020_00000000         # E2 ≈ 167 mili-bps, ABAIXO de δ = 500


def semear_par(
    conn: sqlite3.Connection, i: int, *, ask: int, disponivel: bool = True,
) -> "object":
    """Uma barra e a amostra de BBO do mesmo instante."""
    from app.aovivo import bbo as m

    t = T0 + i * GRADE
    conn.execute(
        "INSERT INTO stream_bar (venue, symbol, timeframe, open_time_ms,"
        " open, high, low, close, volume, quote_volume, trades,"
        " interval_ms, price_scale_exp, volume_scale_exp, recebido_em,"
        " origem) VALUES ('binance','BTCUSDT','15m',?,?,?,?,?,1,1,1,"
        "?,8,8,'x','ao_vivo')",
        (t, ABERTURA, ABERTURA, ABERTURA, ABERTURA, GRADE),
    )
    if not disponivel:
        return m.Amostra(t_grid_ms=t, disponivel=False, motivo="desconectado")

    corrigido = t - 500
    # Variação de 1 unidade a cada 3 instantes: série constante tem
    # desvio-padrão ZERO, e o incremento 17 já registrou um teste que passava
    # pelo motivo errado por causa disso. Aqui a variação é mínima e
    # determinística - o bastante para o bootstrap não degenerar.
    return m.Amostra(
        t_grid_ms=t, disponivel=True,
        bid=ask - 200_00000, bid_qty=5_00000000,
        ask=ask + (i % 3) * 1_000_000, ask_qty=99_00000000, u=i + 1,
        received_at_ms=corrigido + 2_450,
        received_at_corrigido_ms=corrigido,
        sampled_at_ms=corrigido, defasagem_ms=500,
        offset_us=-2_450_000, rtt_us=12_000, incerteza_residual_us=6_000,
        relogio_medido_em_ms=corrigido - 30_000,
    )


def montar_piloto(
    conn: sqlite3.Connection, cfg, *, ask: int, aquecimento: int = 0,
    instantes: int = 1_400,
) -> None:
    """Fecha um piloto real, com `aquecimento` barras sem BBO antes dele.

    O aquecimento existe porque classificar regime exige **672 barras
    anteriores** (ADR 0026): sem ele, todo instante do piloto sai `indefinido`
    e o teste não conseguiria alcançar o ramo `calibrado`.
    """
    from app.aovivo import bbo as m
    from app.calibracao import observacao as obs

    serie = m.Serie(venue="binance", symbol="BTCUSDT",
                    price_scale_exp=8, volume_scale_exp=8)
    amostras = [
        semear_par(conn, i, ask=ask, disponivel=False)
        for i in range(aquecimento)
    ] + [
        semear_par(conn, i, ask=ask)
        for i in range(aquecimento, aquecimento + instantes)
    ]
    m.receber(conn, serie, CONTRATO, amostras)
    obs.registrar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id,
        orcamento_cents=1_000, lados=("compra",),
    )
    piloto.fechar(
        conn, m.Serie(venue="binance", symbol="BTCUSDT",
                      price_scale_exp=0, volume_scale_exp=0),
        CONTRATO,
    )


# --------------------------------------------------- garantia 1: piloto fechado


def test_GARANTIA_1_nao_ha_estimativa_com_o_piloto_ABERTO(conn, cfg):
    """Estimar antes de o período declarado terminar deixaria a janela crescer
    até o número ficar agradável."""
    from app.calibracao import ajuste

    with pytest.raises(ajuste.PilotoAberto) as e:
        ajuste.estimar(
            conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
            config=cfg.config, config_version_id=cfg.id,
        )
    assert "nao foi fechada" in str(e.value)


# ------------------------------------------------ garantia 2: só E2, nunca P&L


def test_GARANTIA_2_o_ajuste_le_SO_E2_e_nunca_desempenho(conn, cfg):
    """Calibrar olhando o P&L do B3 seria ajustar o simulador até a estratégia
    parecer boa - a forma mais direta de fabricar um resultado."""
    import re
    from pathlib import Path

    from tests._prosa import sql_sem_prosa

    sql = sql_sem_prosa(Path("app/calibracao/ajuste.py"))
    for tabela in ("execution", "baseline_result", "ledger_entry",
                   "ledger_transaction", "hypothesis", "shadow_ordem", "run"):
        assert not re.search(rf"\b(from|join)\s+{tabela}\b", sql, re.IGNORECASE), (
            f"o ajuste lê {tabela!r}: ele só pode ler E2"
        )
    assert re.search(r"\bfrom\s+calibracao_observacao\b", sql, re.IGNORECASE)
    assert "e2_mili_bps" in sql


# --------------------------------------------------- garantia 3: δ não relaxa


def test_GARANTIA_3_delta_e_FIXO_e_nao_ha_como_relaxar(conn, cfg):
    """O usuário retirou essa saída ao fechar a D45.

    Relaxar "porque não cabe na reserva" é ajustar a régua ao CALENDÁRIO, e a
    resposta honesta é que aquele regime não foi calibrado - não que ele foi
    calibrado com menos rigor.
    """
    import inspect

    from app.calibracao import ajuste

    assert ajuste.DELTA_MILI_BPS == 500

    for fn in (ajuste.estimar, ajuste.derivar, ajuste.aplicar,
               ajuste.estimar_por_regime):
        nomes = set(inspect.signature(fn).parameters)
        assert not (nomes & {"delta", "delta_mili_bps", "precisao", "tolerancia"}), (
            f"{fn.__name__} aceita delta: ele é FIXO"
        )

    # E o banco grava δ em cada versão, com CHECK - nenhuma leitura futura
    # precisa supor qual régua valia.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO calibracao_versao (contrato, venue, symbol,"
            " config_version_origem, piloto_de_ms, piloto_ate_ms_exclusive,"
            " janela_revalidacao_id, delta_mili_bps, n, n_efetivo_x1000,"
            " tau_x1000, sigma_e_x1000, p10_mili_bps, n_necessario, aplicado,"
            " motivo, criado_em)"
            " VALUES ('bbo@1','b','s',1,1,2,1,900,1,1,1,1,1,1,0,'x','x')"
        )


# ------------------------------------------ garantia 4: regime não calibrado


def test_GARANTIA_4_regime_sem_amostra_fica_NAO_CALIBRADO(conn, cfg):
    """Sem herdar parâmetro de outro regime, sem agrupamento, sem interpolação.

    E isso vira condição de validade de todo resultado obtido nele - a mesma
    disciplina que §8.4.1.1 aplica ao nível de fidelidade.
    """
    from app.calibracao import ajuste

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    e = ajuste.estimar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id,
    )
    por_regime = {r.regime: r for r in e.por_regime}
    assert set(por_regime) == set(ajuste.REGIMES)

    # `indefinido` NUNCA é calibrado: ele não é regime da taxonomia, e sim a
    # declaração de que ainda não dá para dizer. Tratá-lo como quarto regime
    # seria calibrar sobre uma classe que não existe.
    assert por_regime["indefinido"].estado == "nao_calibrado"
    assert "672 barras" in por_regime["indefinido"].motivo
    assert por_regime["indefinido"].n > 0, (
        "os primeiros 672 instantes do piloto não têm histórico anterior "
        "suficiente, e caem aqui"
    )

    # Preço constante => volatilidade ZERO => só `vol_baixa` recebe amostra.
    # Os outros dois ficam sem NENHUMA observação, e é exatamente esse o caso
    # que a garantia 4 governa: sem herdar parâmetro, sem agrupamento, sem
    # interpolação.
    for nome in ("vol_media", "vol_alta"):
        assert por_regime[nome].n == 0
        assert por_regime[nome].estado == "nao_calibrado"
        assert por_regime[nome].spread_bps_x1000 is None, (
            "não calibrado não carrega parâmetro - nem herdado, nem próprio"
        )
        assert "herdar" in por_regime[nome].motivo


def test_GARANTIA_4_o_regime_COM_amostra_e_calibrado(conn, cfg):
    """O outro lado da mesma regra.

    Sem ele, "tudo fica não calibrado" passaria como se fosse a garantia
    funcionando - e um caminho que nunca calibra nada não é conservador, é
    quebrado. É a mesma razão pela qual o Portão A precisa de um número que
    prove que ele não passou por ser surdo.
    """
    from app.calibracao import ajuste

    montar_piloto(conn, cfg, ask=ASK_JUSTO, aquecimento=680, instantes=1_400)
    e = ajuste.estimar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id,
    )
    por_regime = {r.regime: r for r in e.por_regime}
    # Preço constante no aquecimento => volatilidade ZERO => `vol_baixa`.
    assert por_regime["vol_baixa"].n > 0, [
        (r.regime, r.n) for r in e.por_regime
    ]
    assert por_regime["vol_baixa"].estado == "calibrado"
    assert por_regime["vol_baixa"].spread_bps_x1000 is not None


# ------------------------------------------- garantia 5: nunca mais otimista


def test_GARANTIA_5_config_ja_pessimista_e_MANTIDA(conn, cfg):
    """Reduzir custo tornaria o simulador mais otimista, e esse é o único erro
    que este projeto não pode cometer."""
    from app.calibracao import ajuste

    montar_piloto(conn, cfg, ask=ASK_PESSIMISTA, aquecimento=680,
                  instantes=1_400)
    e = ajuste.estimar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id,
    )
    a = ajuste.derivar(e, cfg.config)
    assert e.p10_mili_bps > ajuste.DELTA_MILI_BPS, "o cenário é o de folga"
    assert not a.aplicavel
    assert a.spread_bps_x1000_depois == a.spread_bps_x1000_antes
    assert "MANTIDA" in a.motivo


def test_GARANTIA_5_o_ajuste_NUNCA_devolve_valor_menor(conn, cfg):
    """A propriedade, e não um caso: não há ramo que reduza custo."""
    from app.calibracao import ajuste

    for p10 in (-5_000.0, -100.0, 0.0, 499.0, 500.0, 5_000.0):
        pedido = ajuste._spread_necessario_x1000(p10, cfg.config)
        vigente = int(float(cfg.config.spread_bps) * 1000)
        assert pedido >= vigente, f"p10={p10} reduziu o custo"


def test_GARANTIA_5_o_deficit_entra_DOBRADO_no_campo(conn, cfg):
    """`spread_bps` é o spread CHEIO, aplicado pela metade em cada lado.

    Somar o déficit sem dobrar deixaria metade dele fora - e o simulador
    continuaria otimista com cara de calibrado.
    """
    from app.calibracao import ajuste

    vigente = int(float(cfg.config.spread_bps) * 1000)
    # Déficit de 333 mili-bps por lado => +666 no campo.
    assert ajuste._spread_necessario_x1000(167.0, cfg.config) == vigente + 666


# ------------------------------------ garantia 7: revalidação selada e única


def test_GARANTIA_7_ajustar_SEM_revalidacao_selada_e_recusado(conn, cfg):
    """Ajustar sem período reservado deixaria a confirmação para depois - e
    "depois" é quando já se sabe o que se quer confirmar."""
    from app.calibracao import ajuste

    montar_piloto(conn, cfg, ask=ASK_JUSTO, aquecimento=680, instantes=1_400)
    with pytest.raises(ajuste.RevalidacaoNaoSelada) as e:
        ajuste.aplicar(
            conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
            config=cfg.config, config_version_id=cfg.id, autor="teste",
            settings=None,
        )
    assert "ANTES de ajustar" in str(e.value)


def test_GARANTIA_7_a_revalidacao_e_DISJUNTA_do_piloto(conn, cfg):
    from app.calibracao import ajuste

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    j = ajuste.selar_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT"
    )
    janela = piloto.ler(
        conn,
        __import__("app.aovivo.bbo", fromlist=["Serie"]).Serie(
            venue="binance", symbol="BTCUSDT",
            price_scale_exp=0, volume_scale_exp=0),
        CONTRATO,
    )
    assert j["de_ms"] == janela.ate_ms_exclusive
    assert j["ate_ms_exclusive"] > j["de_ms"]


def test_GARANTIA_7_selar_de_novo_devolve_a_MESMA_janela(conn, cfg):
    """Uma segunda janela seria uma segunda chance de escolher a fronteira."""
    from app.calibracao import ajuste

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    a = ajuste.selar_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT")
    b = ajuste.selar_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT")
    assert a["id"] == b["id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM janela_revalidacao"
    ).fetchone()[0] == 1


def test_GARANTIA_7_a_janela_selada_e_IMUTAVEL(conn, cfg):
    from app.calibracao import ajuste

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    ajuste.selar_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT")
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("UPDATE janela_revalidacao SET de_ms = 1")
    assert "periodo que confirma" in str(e.value)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM janela_revalidacao")


@pytest.fixture
def uso_real(conn: sqlite3.Connection, cfg):
    """Uma janela selada e uma calibração DE VERDADE, com um uso gravado.

    As duas primeiras versões destes testes eram VAZIAS e passavam pelo motivo
    errado: `INSERT` com `janela_id = 1` inexistente falhava por CHAVE
    ESTRANGEIRA, e um `UPDATE` numa tabela sem linhas nunca dispara um
    `BEFORE UPDATE`. Uma trava conferida sobre tabela vazia não foi conferida.
    """
    conn.execute(
        "INSERT INTO janela_revalidacao (contrato, venue, symbol, de_ms,"
        " ate_ms_exclusive, piloto_ate_ms_exclusive, selada_em)"
        " VALUES (?, 'binance', 'BTCUSDT', 100, 200, 100, 'x')",
        (CONTRATO,),
    )
    janela_id = conn.execute(
        "SELECT id FROM janela_revalidacao"
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO calibracao_versao (contrato, venue, symbol,"
        " config_version_origem, piloto_de_ms, piloto_ate_ms_exclusive,"
        " janela_revalidacao_id, delta_mili_bps, n, n_efetivo_x1000,"
        " tau_x1000, sigma_e_x1000, p10_mili_bps, n_necessario, aplicado,"
        " motivo, criado_em)"
        " VALUES (?, 'binance', 'BTCUSDT', ?, 1, 100, ?, 500, 10, 10000,"
        " 1000, 100, 600, 5, 0, 'teste', 'x')",
        (CONTRATO, cfg.id, janela_id),
    )
    calibracao_id = conn.execute(
        "SELECT id FROM calibracao_versao"
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO revalidacao_uso (janela_id, calibracao_versao_id,"
        " lb95_mili_bps, p10_mili_bps, n, passou, usada_em)"
        " VALUES (?, ?, 10, 600, 500, 1, 'x')",
        (janela_id, calibracao_id),
    )
    return {"janela_id": janela_id, "calibracao_id": calibracao_id}


def test_GARANTIA_7_a_revalidacao_e_de_USO_UNICO(conn, cfg, uso_real):
    """O `PRIMARY KEY (janela_id)` é quem impõe - mesmo desenho do holdout.

    E a janela e a calibração existem: sem elas, o `INSERT` falharia por chave
    estrangeira e o teste passaria sem exercitar a trava.
    """
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute(
            "INSERT INTO revalidacao_uso (janela_id, calibracao_versao_id,"
            " lb95_mili_bps, p10_mili_bps, n, passou, usada_em)"
            " VALUES (?, ?, 0, 0, 1, 0, 'y')",
            (uso_real["janela_id"], uso_real["calibracao_id"]),
        )
    assert "UNIQUE" in str(e.value) or "PRIMARY" in str(e.value), str(e.value)


def test_GARANTIA_7_o_uso_e_imutavel(conn, cfg, uso_real):
    """`UPDATE` em tabela vazia nunca dispara `BEFORE UPDATE` - a primeira
    versão deste teste passava sem tocar o gatilho."""
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("UPDATE revalidacao_uso SET passou = 0")
    assert "ate passar" in str(e.value)
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("DELETE FROM revalidacao_uso")
    assert "ja consumida" in str(e.value)


def test_GARANTIA_6_a_versao_de_calibracao_e_IMUTAVEL(conn, cfg, uso_real):
    """Recalibrar produz uma versão NOVA, e a antiga continua descrevendo os
    resultados obtidos sob ela."""
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("UPDATE calibracao_versao SET p10_mili_bps = 0")
    assert "versao NOVA" in str(e.value)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM calibracao_versao")


# ------------------------------------------- o critério muda entre as janelas


def test_a_revalidacao_exige_o_LIMITE_INFERIOR_e_nao_o_ponto():
    """`p10 >= 0` diz que o melhor palpite é que o modelo é pessimista.
    `LB95 >= 0` diz que há 95% de confiança de que ele é.

    Confirmação que aceita estimativa pontual não confirma nada.
    """
    from pathlib import Path

    fonte = Path("app/calibracao/ajuste.py").read_text(encoding="utf-8")
    assert "ic.limite_inferior >= 0" in fonte
    assert "LB95(p10(E2)) >= 0" in fonte


# ------------------------------- garantias 6 e 8: versão nova, e o B1 depois


def test_GARANTIA_6_o_ajuste_NAO_altera_run_anterior(conn, cfg):
    """A calibração nasce como `config_version` NOVA.

    Cada run continua apontando para a config sob a qual foi aberto, e nenhum
    resultado já publicado muda de valor - a imutabilidade da `config_version`
    é quem entrega isso, e ela já existia.
    """
    from app.calibracao import ajuste
    from app.config import service as config_service
    from app.settings import get_settings

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    ajuste.selar_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT")

    hash_antes = config_service.versao_por_id(conn, cfg.id).config_hash
    spread_antes = config_service.versao_por_id(conn, cfg.id).config.spread_bps

    r = ajuste.aplicar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, autor="teste",
        settings=get_settings(),
    )
    assert r["aplicado"] is True, r["motivo"]
    assert r["config_version_nova"] != cfg.id

    # A versão de ORIGEM não foi tocada.
    depois = config_service.versao_por_id(conn, cfg.id)
    assert depois.config_hash == hash_antes
    assert depois.config.spread_bps == spread_antes

    # E a BASE também não muda na versão nova (ADR 0033): ela é a hipótese
    # original. Este teste exigia o contrário até o usuário corrigir o
    # desenho - o ajuste subia o campo global, e o valor medido em `vol_baixa`
    # acabava aplicado em `vol_alta`, onde nada foi observado.
    nova = config_service.versao_por_id(conn, r["config_version_nova"])
    assert nova.config.spread_bps == spread_antes, (
        "a base é hipótese, e a calibração não a reescreve"
    )
    assert nova.config_hash == hash_antes, (
        "o payload é idêntico: o que muda é o perfil, e ele vive FORA do hash"
    )

    # O que mudou é o perfil, referenciado por hash na coluna da tabela.
    from app.calibracao import perfil as perfil_mod

    p = perfil_mod.da_config_version(conn, r["config_version_nova"])
    assert p is not None
    assert p.hash == r["calibracao_perfil_hash"]
    assert perfil_mod.da_config_version(conn, cfg.id) is None, (
        "a versão antiga não ganha perfil nenhum"
    )


def test_GARANTIA_8_o_B1_negativo_e_reexecutado_sob_a_versao_CALIBRADA(
    conn, cfg, client
):
    """R67 / A2 do Portão A: operar ao acaso continua perdendo depois do ajuste?

    Se passasse a dar lucro, a calibração estaria errada e nenhum número
    medido nela significaria coisa alguma - então a conferência pertence ao
    mesmo ato que produz o ajuste, e não a uma rota que alguém lembra de
    chamar.
    """
    import sys

    from tests.test_maos_rapidas import precos_passeio
    from tests.test_simulador import criar_dataset

    dataset_id = criar_dataset(conn, precos_passeio(3_000))
    assert dataset_id

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    r = client.post("/api/calibracao/selar-revalidacao", headers=CABECALHO,
                    json={"author": "teste"})
    assert r.status_code == 201, r.text

    r = client.post("/api/calibracao/ajustar", headers=CABECALHO,
                    json={"author": "teste"})
    assert r.status_code == 201, r.text
    corpo = r.json()

    assert corpo["aplicado"] is True, corpo["motivo"]
    b1 = corpo["b1_negativo"]
    assert b1["reexecutado"] is True, b1.get("motivo")
    assert b1["config_version"] == corpo["config_version_nova"]
    assert b1["corridas"], "o B1 tem de ter rodado sob a versão nova"
    assert b1["negativo"] is True, (
        f"operar ao acaso passou a dar LUCRO depois do ajuste: {b1['corridas']}"
    )


def test_sem_ajuste_aplicado_o_B1_NAO_afirma_ter_rodado(conn, cfg, client):
    """Dizer "passou" sem reexecutar seria afirmar uma conferência que não
    aconteceu - `None` com o motivo escrito, como o Portão A já faz."""
    montar_piloto(conn, cfg, ask=ASK_PESSIMISTA, instantes=1_400)
    client.post("/api/calibracao/selar-revalidacao", headers=CABECALHO,
                json={"author": "teste"})
    r = client.post("/api/calibracao/ajustar", headers=CABECALHO,
                    json={"author": "teste"})
    corpo = r.json()
    assert corpo["aplicado"] is False
    assert corpo["b1_negativo"]["reexecutado"] is False
    assert "nenhum ajuste" in corpo["b1_negativo"]["motivo"]


def test_a_revalidacao_VAZIA_nao_consome_a_janela(conn, cfg, client):
    """Consumir a janela sem dado seria gastar a confirmação sem confirmar."""
    from app.calibracao import ajuste
    from app.settings import get_settings

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    ajuste.selar_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT")
    ajuste.aplicar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, autor="t",
        settings=get_settings(),
    )
    r = client.post("/api/calibracao/revalidar", headers=CABECALHO,
                    json={"author": "teste"})
    assert r.status_code == 409
    assert "NAO e consumida" in r.json()["detail"]

    janela = ajuste.ler_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT")
    assert janela["consumida"] is False


# ===========================================================================
# ADR 0033: o override é POR REGIME
#
# "Usar o maior valor observado não significa ser pessimista onde nada foi
#  observado. A etiqueta de 'não calibrado' não corrigiria o preço usado."
# ===========================================================================


def ov(regime: str, spread_x1000: int):
    from app.calibracao.perfil import Override

    return Override(regime=regime, spread_bps_x1000=spread_x1000,
                    n=500, p10_mili_bps=163)


def test_regime_SEM_override_usa_a_BASE_e_nunca_o_de_outro(conn, cfg):
    """O ponto inteiro do ADR 0033.

    O máximo sobre uma amostra de regimes não é limite superior para os
    regimes fora dela - e `vol_alta` é, por construção da taxonomia, o tercil
    de MAIOR volatilidade, onde o custo tende a ser pior.
    """
    from app.calibracao import perfil as perfil_mod

    p = perfil_mod.gravar(
        conn, spread_bps_base_x1000=1_000, overrides=[ov("vol_baixa", 1_674)]
    )

    assert p.spread_bps_x1000("vol_baixa") == (1_674, True)
    assert p.spread_bps_x1000("vol_media") == (1_000, False), (
        "usou o override de vol_baixa num regime onde nada foi observado"
    )
    assert p.spread_bps_x1000("vol_alta") == (1_000, False)
    assert p.spread_bps_x1000(None) == (1_000, False)


def test_INDEFINIDO_nunca_herda_parametro_de_ninguem(conn, cfg):
    """Ele não é um quarto regime: é a declaração de que faltam 672 barras."""
    from app.calibracao import perfil as perfil_mod

    with pytest.raises(perfil_mod.RegimeInvalido) as e:
        perfil_mod.gravar(
            conn, spread_bps_base_x1000=1_000,
            overrides=[ov("indefinido", 1_674)],
        )
    assert "nunca pode herdar" in str(e.value)


def test_o_BANCO_tambem_recusa_override_para_indefinido(conn, cfg):
    from app.calibracao import perfil as perfil_mod

    p = perfil_mod.gravar(conn, spread_bps_base_x1000=1_000, overrides=[])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO calibracao_perfil_regime (perfil_hash, regime,"
            " spread_bps_x1000, n, p10_mili_bps) VALUES (?, 'indefinido', 1, 1, 1)",
            (p.hash,),
        )


def test_a_AUSENCIA_de_linha_e_a_informacao(conn, cfg):
    """Gravar uma linha com valor "herdado" tornaria a herança invisível; a
    ausência a torna impossível."""
    from app.calibracao import perfil as perfil_mod

    p = perfil_mod.gravar(
        conn, spread_bps_base_x1000=1_000, overrides=[ov("vol_baixa", 1_674)]
    )
    linhas = [
        r["regime"] for r in conn.execute(
            "SELECT regime FROM calibracao_perfil_regime WHERE perfil_hash = ?",
            (p.hash,),
        )
    ]
    assert linhas == ["vol_baixa"], "só o regime calibrado tem linha"


def test_o_hash_do_perfil_inclui_a_TAXONOMIA(conn, cfg, monkeypatch):
    """Se os cortes da D40 mudassem, `vol_alta` passaria a nomear outra coisa
    e o override deixaria de descrever o que descrevia.

    Sem a taxonomia no hash, o perfil sobreviveria a uma mudança que o
    invalida - e sobreviveria calado.
    """
    from app.calibracao import perfil as perfil_mod
    from app.regime import deteccao

    antes = perfil_mod.calcular_hash(
        spread_bps_base_x1000=1_000, overrides=[ov("vol_baixa", 1_674)]
    )
    monkeypatch.setattr(deteccao, "CORTE_SUPERIOR_MILI_BPS", 25_400)
    depois = perfil_mod.calcular_hash(
        spread_bps_base_x1000=1_000, overrides=[ov("vol_baixa", 1_674)]
    )
    assert antes != depois


def test_o_hash_nao_depende_da_ORDEM_de_insercao(conn, cfg):
    from app.calibracao import perfil as perfil_mod

    a = perfil_mod.calcular_hash(
        spread_bps_base_x1000=1_000,
        overrides=[ov("vol_alta", 2_000), ov("vol_baixa", 1_674)],
    )
    b = perfil_mod.calcular_hash(
        spread_bps_base_x1000=1_000,
        overrides=[ov("vol_baixa", 1_674), ov("vol_alta", 2_000)],
    )
    assert a == b


def test_gravar_e_IDEMPOTENTE_pelo_hash(conn, cfg):
    from app.calibracao import perfil as perfil_mod

    a = perfil_mod.gravar(
        conn, spread_bps_base_x1000=1_000, overrides=[ov("vol_baixa", 1_674)])
    b = perfil_mod.gravar(
        conn, spread_bps_base_x1000=1_000, overrides=[ov("vol_baixa", 1_674)])
    assert a.hash == b.hash
    assert conn.execute(
        "SELECT COUNT(*) FROM calibracao_perfil").fetchone()[0] == 1


def test_o_perfil_e_IMUTAVEL(conn, cfg):
    from app.calibracao import perfil as perfil_mod

    perfil_mod.gravar(
        conn, spread_bps_base_x1000=1_000, overrides=[ov("vol_baixa", 1_674)])
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("UPDATE calibracao_perfil SET spread_bps_base_x1000 = 2")
    assert "perfil NOVO" in str(e.value)
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("UPDATE calibracao_perfil_regime SET spread_bps_x1000 = 2")
    assert "recalibrar sem dizer" in str(e.value)
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("DELETE FROM calibracao_perfil_regime")
    assert "voltar silenciosamente para a base" in str(e.value)


def test_o_vinculo_e_feito_no_INSERT_e_a_linha_nao_muda(conn, cfg):
    """Trocar o perfil de uma versão mudaria o preço de tudo que rodou sob ela.

    E isso é impossível porque `config_version` é imutável por gatilho - não
    porque alguém confere. Eu havia escrito uma função `vincular` que fazia
    `UPDATE` com uma checagem por cima: ela **nunca poderia funcionar**, e uma
    função sem caminho é a forma do `BLOCOS`.
    """
    from app.calibracao import perfil as perfil_mod

    assert not hasattr(perfil_mod, "vincular"), (
        "função que o banco impede de rodar não deve existir"
    )
    a = perfil_mod.gravar(
        conn, spread_bps_base_x1000=1_000, overrides=[ov("vol_baixa", 1_674)])
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute(
            "UPDATE config_version SET calibracao_perfil_hash = ? WHERE id = ?",
            (a.hash, cfg.id),
        )
    assert "imutavel" in str(e.value)


# --------------------------------------------- o efeito no preço, de verdade


def test_o_override_MUDA_o_preco_e_a_base_NAO(conn, cfg):
    """Sem isto, "por regime" seria só uma etiqueta - e uma etiqueta não
    corrige um preço."""
    from decimal import Decimal

    from app.calibracao import observacao as obs

    base = obs.prever(ABERTURA, "compra", cfg.config)
    com_override = obs.prever(
        ABERTURA, "compra", cfg.config, spread_bps=Decimal("1.674")
    )
    assert com_override > base
    # +0,337 bps = metade de (1,674 − 1,000), que é o déficit por lado.
    assert (com_override - base) / ABERTURA * 10_000 == pytest.approx(
        0.337, abs=0.001
    )


def test_SEM_override_o_preco_e_BYTE_A_BYTE_o_historico(conn, cfg):
    """R12 vale sem asterisco: todo caminho que existia continua passando
    `None` e lendo `config.spread_bps`."""
    from app.simulador.execucao import preco_executado

    for lado in ("compra", "venda"):
        assert preco_executado(ABERTURA, lado, cfg.config) == preco_executado(
            ABERTURA, lado, cfg.config, spread_bps=None
        )
        assert preco_executado(
            ABERTURA, lado, cfg.config
        ) == preco_executado(
            ABERTURA, lado, cfg.config, spread_bps=cfg.config.spread_bps
        )


# ------------------------------------ a fidelidade fica INCONCLUSIVA, não falsa


def test_regime_nao_calibrado_deixa_o_resultado_INCONCLUSIVO(conn, cfg):
    """Não reprovado, não aprovado - a forma que §8.4.1.1 dá ao nível de
    fidelidade."""
    from app.calibracao import perfil as perfil_mod

    p = perfil_mod.gravar(
        conn, spread_bps_base_x1000=1_000, overrides=[ov("vol_baixa", 1_674)])

    f = perfil_mod.fidelidade(p, ["vol_baixa", "vol_baixa"])
    assert f.conclusiva is True

    f = perfil_mod.fidelidade(p, ["vol_baixa", "vol_alta"])
    assert f.conclusiva is False
    assert f.regimes_nao_calibrados == ["vol_alta"]
    assert "INCONCLUSIVO" in f.motivo
    assert "shadow segue rodando" in f.motivo


def test_periodo_so_com_INDEFINIDO_tambem_e_inconclusivo(conn, cfg):
    from app.calibracao import perfil as perfil_mod

    p = perfil_mod.gravar(
        conn, spread_bps_base_x1000=1_000, overrides=[ov("vol_baixa", 1_674)])
    f = perfil_mod.fidelidade(p, [None, None])
    assert f.conclusiva is False
    assert f.regimes_nao_calibrados == ["indefinido"]


def test_SEM_perfil_nenhum_regime_e_calibrado(conn, cfg):
    """As versões antigas não têm perfil, e é assim de propósito."""
    from app.calibracao import perfil as perfil_mod

    f = perfil_mod.fidelidade(None, ["vol_baixa"])
    assert f.conclusiva is False
    assert f.regimes_nao_calibrados == ["vol_baixa"]
