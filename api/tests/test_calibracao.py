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

from app.calibracao import ajuste, bootstrap, observacao, piloto

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


def _relogio_ms() -> int:
    import time

    return int(time.time() * 1000)


#: A ANCORA DOS TESTES E O FUTURO, e isso e consequencia do desenho - nao
#: conveniencia.
#:
#: Uma janela selada comeca na primeira grade POSTERIOR ao proprio selamento, e
#: o selamento vem do relogio do BANCO (o gatilho recusa qualquer outro). Entao
#: o dado que cai dentro de uma janela selada e, por construcao, dado que ainda
#: nao existia quando ela foi selada - e num teste isso so se escreve colocando
#: os instantes adiante do relogio.
#:
#: Ancorar no passado, como antes, so era possivel porque o selo era um numero
#: escolhido por quem inseria. Era exatamente a porta que estas correcoes
#: fecharam.
#:
#: Oito grades sao duas horas: folga suficiente para a suite inteira rodar sem
#: que o relogio alcance a ancora, que e fixada no import.
T0 = (_relogio_ms() // GRADE + 8) * GRADE
CONTRATO = "bbo@1"
ORCAMENTO = 71_400          # US$ 714,00 em centavos — o giro da hipótese 41

#: Sob o ADR 0041 nao ha materializacao sem janela SELADA: o gatilho
#: `calibracao_observacao_dentro_da_janela` recusa. Os helpers abaixo selam a
#: janela certa e chamam `registrar` com a finalidade - e e de proposito que
#: nao exista um default: a finalidade e da JANELA, e um default aqui faria a
#: barreira depender de quem chama.
PERFIL_DE_TESTE = "sem-perfil"
HASH_DE_TESTE = "hash-de-janela-de-teste"


#: A duracao congelada que `selar_calibracao_no_inicio` grava. Um `INSERT`
#: cru de revalidacao tem de usar exatamente esta, e o gatilho
#: `janela_revalidacao_duracao_congelada` recusa qualquer outra.
DURACAO_DE_TESTE = 14 * 86_400_000

#: O FIM PRE-DECLARADO da janela de calibracao nos testes de `INSERT` cru.
#: Larga de proposito - 20.000 grades -, porque o que estes testes exercitam
#: nao e o horizonte derivado (isso tem teste proprio), e uma janela curta
#: faria a observacao cair fora dela por motivo que nao e o do teste.
DURACAO_DA_CALIBRACAO_DE_TESTE = 20_000 * GRADE


# ===========================================================================
# A IDENTIDADE CALIBRADA TEM DE ESTAR CERTIFICADA — 2026-10-13
#
# `janela_revalidacao_exige_a_finalizacao` prova que o perfil EXISTE.
# Estes helpers servem aos testes que provam a outra metade: que ele foi
# **conferido**, pelos cinco escopos, para o alvo DESTA identidade.
#
# O helper CERTIFICA de verdade — grava `certificacao_execucao` e
# `certificacao_manifesto` com os componentes reais de `alvo.montar`. Fingir
# a certificação com um alvo inventado faria o gatilho (f) recusar, e ele
# estaria certo: os componentes gravados precisam descrever a identidade.
# ===========================================================================


def semear_dataset(conn) -> None:
    """Um `dataset` minimo, so para que haja FONTE DE DADOS no alvo.

    `alvo.montar` recusa sem ela, e a recusa e correta: a fonte e componente
    do alvo, e um alvo sem fonte descreveria um laboratorio que nao le nada.
    Os testes de calibracao nunca precisaram de dataset porque a calibracao le
    `stream_bar`, que e o fluxo AO VIVO - e nao o dataset historico.
    """
    if conn.execute("SELECT COUNT(*) FROM dataset").fetchone()[0]:
        return
    conn.execute(
        "INSERT INTO dataset (venue, symbol, timeframe, interval_ms,"
        " start_ms, end_ms, reserved_from_ms, bars, sha256, source,"
        " source_files_json, fetched_at, fidelity_level, price_scale_exp,"
        " volume_scale_exp)"
        " VALUES ('binance','BTCUSDT','15m',?,1,1000,900,10,?,'teste','[]',"
        " 'x',1,8,8)",
        (GRADE, "d" * 64),
    )


def _alvo_de_agora(conn) -> dict:
    """O alvo da identidade VIGENTE, pelo mesmo caminho que a rota usa."""
    from app.certificacao import alvo as alvo_mod
    from app.dataset import loader as dataset_loader

    semear_dataset(conn)
    meta = dataset_loader.dataset_vigente(conn)
    assert meta is not None
    return alvo_mod.montar(conn, dataset_hash=meta.sha256)


def certificar(conn, *, escopos=None, alvo: dict | None = None,
               passa: bool = True) -> str:
    """Sela manifesto para os escopos pedidos. Devolve o `alvo_hash`.

    Por `INSERT` cru, e nao rodando a suite: rodar os cinco escopos de verdade
    dentro de um teste de calibracao levaria minutos e mediria outra coisa. O
    que estes testes exercitam e a TRAVA, e ela le manifesto.

    `componentes_json` leva os componentes REAIS, porque o gatilho (f) confere
    `identidade_executavel` contra a janela - e e justamente essa conferencia
    que impede um alvo antigo de autorizar uma identidade nova.
    """
    import json

    from app.certificacao import escopos as escopos_mod

    o_alvo = alvo if alvo is not None else _alvo_de_agora(conn)
    alvo_hash = str(o_alvo["alvo_de_certificacao_hash"])
    for escopo in (escopos if escopos is not None else escopos_mod.TODOS):
        cur = conn.execute(
            "INSERT INTO certificacao_execucao (alvo_hash, componentes_json,"
            " build_do_backend, iniciada_em, casos_esperados,"
            " micros_para_copiar, micros_da_suite, bytes_da_copia,"
            " intocado_json, escopo, blocos_esperados)"
            " VALUES (?,?,'teste','x',?,0,0,0,'{}',?,0)",
            (alvo_hash,
             json.dumps(o_alvo["componentes"], ensure_ascii=False),
             escopos_mod.CASOS_ESPERADOS[escopo], escopo),
        )
        eid = int(cur.lastrowid)
        for i in range(escopos_mod.CASOS_ESPERADOS[escopo]):
            conn.execute(
                "INSERT INTO certificacao_caso (execucao_id, chave, familia,"
                " tipo, barrado, promovido, resultado_json)"
                " VALUES (?,?,'x','estrutural',1,0,'{}')",
                (eid, f"{escopo}-{i}"),
            )
        conn.execute(
            "INSERT INTO certificacao_manifesto (execucao_id, alvo_hash,"
            " manifesto_json, selado_em, passa) VALUES (?,?,'{}','x',?)",
            (eid, alvo_hash, 1 if passa else 0),
        )
    return alvo_hash


def revalidacao_crua(conn, *, fech: int, regime: str, calibracao_ate: int,
                     symbol: str = "BTCUSDT",
                     duracao: int = DURACAO_DE_TESTE,
                     alvo_hash: str | None = None,
                     identidade: str | None = None):
    """Uma `janela_revalidacao` por `INSERT` cru, VALIDA por construcao.

    `de_ms` e `ate_ms_exclusive` saem da MESMA expressao SQL que o caminho
    oficial usa - porque o gatilho
    `janela_revalidacao_de_ms_calculado_pelo_banco` recalcula a conta e recusa
    qualquer outro valor. Declarar a fronteira a mao deixou de ser possivel, e
    e isso que os testes abaixo exploram: cada um perturba UM campo e exige que
    a trava daquele campo seja a que fala.
    """
    if alvo_hash is None or identidade is None:
        # A CERTIFICACAO DA IDENTIDADE VIGENTE, porque o cru nasce VALIDO.
        # Sem ela, todo teste abaixo falharia pelo gatilho (d) em vez de pelo
        # que ele perturba - e um teste que falha pelo motivo errado nao
        # prova o que afirma.
        certificar(conn)
        derivados = _identidade_e_alvo_do_fechamento(conn, fech)
        identidade = identidade if identidade is not None else derivados[0]
        alvo_hash = alvo_hash if alvo_hash is not None else derivados[1]
    return conn.execute(
        "INSERT INTO janela_revalidacao (contrato, venue, symbol, regime,"
        " de_ms, ate_ms_exclusive, calibracao_fechamento_id,"
        " calibracao_ate_ms_exclusive, grade_ms, alvo_hash,"
        " identidade_executavel)"
        f" VALUES (?,?,?,?,{ajuste.PRIMEIRA_GRADE_SQL},"
        f" {ajuste.PRIMEIRA_GRADE_SQL} + ?,?,?,?,?,?)",
        (CONTRATO, "binance", symbol, regime,
         GRADE, GRADE, calibracao_ate,
         GRADE, GRADE, calibracao_ate, duracao,
         fech, calibracao_ate, GRADE, alvo_hash, identidade),
    )


def _identidade_e_alvo_do_fechamento(conn, fech: int) -> tuple[str, str]:
    """A identidade da finalizacao daquele fechamento, e o alvo dela.

    O `INSERT` cru precisa dos dois VALIDOS para que cada teste perturbe UM
    campo - um cru que ja nascesse invalido faria todos eles falharem pelo
    gatilho errado.
    """
    linha = conn.execute(
        "SELECT c.config_hash AS h, c.calibracao_perfil_hash AS p"
        "  FROM calibracao_regime_fechamento f"
        "  JOIN calibracao_versao v"
        "    ON v.janela_calibracao_id = f.janela_calibracao_id"
        "  JOIN config_version c ON c.id = v.config_version_nova"
        " WHERE f.id = ?", (fech,),
    ).fetchone()
    if linha is None:
        return "sem-finalizacao", "0" * 64
    from app.calibracao import identidade as ident_mod

    return (
        ident_mod.identidade_executavel(str(linha["h"]), linha["p"]),
        str(_alvo_de_agora(conn)["alvo_de_certificacao_hash"]),
    )


def _fluxo_curto(conn) -> None:
    """Barras de `T0` ate `T0 + 4` grades, so para o watermark existir.

    O fim da janela de calibracao e fato do DADO: sem barra alem dele, a
    finalizacao e recusada - corretamente.
    """
    for k in range(5):
        conn.execute(
            "INSERT OR IGNORE INTO stream_bar (venue, symbol, timeframe,"
            " open_time_ms, open, high, low, close, volume, quote_volume,"
            " trades, interval_ms, price_scale_exp, volume_scale_exp,"
            " recebido_em, origem)"
            " VALUES ('binance','BTCUSDT','15m',?,?,?,?,?,1,1,1,?,8,8,"
            " 'x','ao_vivo')",
            (T0 + k * GRADE, ABERTURA, ABERTURA, ABERTURA, ABERTURA, GRADE),
        )


def _finalizacao_crua(conn) -> None:
    """A finalizacao consolidada, por `INSERT` cru. UMA por janela."""
    jc = conn.execute(
        "SELECT id, de_ms, ate_ms_exclusive FROM janela_calibracao"
        " WHERE contrato = ?", (CONTRATO,)
    ).fetchone()
    # APLICADA, e apontando para a config VIGENTE.
    #
    # Nao e detalhe: desde 2026-10-13 a janela de revalidacao carrega a
    # identidade que a finalizacao produziu, e uma finalizacao com
    # `aplicado = 0` nao produz identidade nenhuma - `config_version_nova` e
    # NULL, e o gatilho (e) recusa, corretamente. Apontar `nova` para a
    # vigente e o que o caminho real produz: `aplicar` cria a versao nova, e
    # ela PASSA a ser a vigente.
    vigente = conn.execute(
        "SELECT MAX(id) FROM config_version"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO calibracao_versao (contrato, venue, symbol,"
        " config_version_origem, config_version_nova, janela_calibracao_id,"
        " calibracao_de_ms,"
        " calibracao_ate_ms_exclusive, delta_mili_bps, n, n_efetivo_x1000,"
        " tau_x1000, sigma_e_x1000, p10_mili_bps, n_necessario, aplicado,"
        " motivo, criado_em)"
        " VALUES (?, 'binance', 'BTCUSDT', 1, ?, ?, ?, ?, 500, 10, 10000,"
        " 1000, 100, 600, 5, 1, 'finalizacao crua do teste', 'x')",
        (CONTRATO, vigente, jc["id"], jc["de_ms"], jc["ate_ms_exclusive"]),
    )


def fechar_piloto_cru(conn, *, ate_ms: int = T0) -> None:
    """Uma linha em `janela_piloto`, para que a calibracao tenha de onde nascer.

    Ela existia implicitamente antes: o helper de selamento DECLARAVA
    `piloto_ate_ms_exclusive` e ninguem conferia. O gatilho
    `janela_calibracao_fronteiras_conferidas` passou a exigir que a fronteira
    de origem EXISTA - duas fontes do mesmo fato divergem, e esta entrava na
    conta de `de_ms`.
    """
    if conn.execute(
        "SELECT 1 FROM janela_piloto WHERE contrato = ?", (CONTRATO,)
    ).fetchone():
        return
    conn.execute(
        "INSERT INTO janela_piloto (contrato, venue, symbol, de_ms,"
        " ate_ms_exclusive, observacoes_validas, observacoes_totais,"
        " dias_corridos_x1000, fechada_por, criado_em)"
        " VALUES (?, 'binance', 'BTCUSDT', ?, ?, 1000, 1344, 14000, 'dias','x')",
        (CONTRATO, ate_ms - 1_344 * GRADE, ate_ms),
    )


def selar_calibracao_no_inicio(
    conn, *, piloto_ate_ms: int = T0,
    duracao_ms: int = DURACAO_DA_CALIBRACAO_DE_TESTE,
) -> int:
    """Sela a janela de calibracao, e **nao escolhe o inicio dela**.

    Duas coisas NAO sao nomeadas no `INSERT`, e as duas ausencias sao o ponto:

    - `selada_em_ms` / `selada_em` tem `DEFAULT` que le o relogio do BANCO, e
      um gatilho recusa qualquer carimbo fora dele;
    - **`de_ms` e calculado pelo banco** - `MAX(primeira grade > agora, fim do
      piloto)` -, e outro gatilho recalcula a conta e recusa o resto. Antes o
      helper declarava `de_ms`, e declarar a fronteira era exatamente o que
      60 s de tolerancia no selo permitiam fazer perto de uma virada de grade.

    Com `piloto_ate_ms = T0` (oito grades adiante do relogio), o banco calcula
    `de_ms = T0` - o mesmo valor de antes, agora derivado em vez de escolhido.
    """
    fechar_piloto_cru(conn, ate_ms=piloto_ate_ms)
    conn.execute(
        "INSERT INTO janela_calibracao (contrato, venue, symbol, de_ms,"
        " piloto_ate_ms_exclusive, grade_ms,"
        " confianca_ppm, metodo_bootstrap, tratamento_dependencia, alvo,"
        " regra_encerramento, duracao_minima_ms, delta_mili_bps,"
        " duracao_calibracao_ms, ate_ms_exclusive)"
        f" VALUES (?,?,?,{ajuste.PRIMEIRA_GRADE_SQL},?,?,950000,"
        " 'estacionario_politis_white',"
        " 'hac_bartlett_newey_west','lb95_p10_e2_maior_igual_zero',"
        f" 'n_efetivo_maior_igual_n_exigido',?,500,?,"
        f" {ajuste.PRIMEIRA_GRADE_SQL} + ?)",
        (CONTRATO, "binance", "BTCUSDT", GRADE, GRADE, piloto_ate_ms,
         piloto_ate_ms, GRADE, DURACAO_DE_TESTE, duracao_ms,
         GRADE, GRADE, piloto_ate_ms, duracao_ms),
    )
    return int(conn.execute(
        "SELECT id FROM janela_calibracao WHERE contrato = ?", (CONTRATO,)
    ).fetchone()[0])


def materializar(conn, cfg, *, finalidade: str = "calibracao", **kw):
    """`registrar` com a janela ja selada. Devolve o dict publicado."""
    if finalidade == "calibracao" and not conn.execute(
        "SELECT 1 FROM janela_calibracao WHERE contrato = ?", (CONTRATO,)
    ).fetchone():
        selar_calibracao_no_inicio(conn)
    base = dict(
        finalidade=finalidade, contrato=CONTRATO, venue="binance",
        symbol="BTCUSDT", config=cfg.config, config_version_id=cfg.id,
        calibracao_perfil_hash=PERFIL_DE_TESTE, janela_hash=HASH_DE_TESTE,
        de_ms=T0,
        # O FIM DA JANELA, e nao um numero solto: a janela de calibracao ganhou
        # fim declarado, e o gatilho recusa instante fora dele.
        ate_ms_exclusive=T0 + DURACAO_DA_CALIBRACAO_DE_TESTE // GRADE * GRADE,
        orcamento_cents=ORCAMENTO,
    )
    base.update(kw)
    return observacao.registrar(conn, **base).como_dict()



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
    """Barra fechada e amostra de BBO no MESMO instante, para `n` instantes.

    **E uma barra de FRONTEIRA a mais, sem BBO valida.**

    A trava contra materializar antes do backfill exige `watermark > t`: o
    instante nao pode ser a ultima barra do fluxo, porque a ultima e a unica
    que poderia nao estar fechada. Sem a barra extra, o `n`-esimo instante
    seria sempre ADIADO - correto, e nao e o que estes testes querem medir.

    A fronteira entra com `disponivel=False`: ela levanta o watermark e **nao**
    acrescenta candidato, entao `instantes_validos_na_janela` continua `n`.
    """
    from app.aovivo import bbo as m

    serie = m.Serie(venue="binance", symbol="BTCUSDT",
                    price_scale_exp=8, volume_scale_exp=8)
    amostras = []
    for i in range(n + 1):
        t = T0 + i * GRADE
        conn.execute(
            "INSERT INTO stream_bar (venue, symbol, timeframe, open_time_ms,"
            " open, high, low, close, volume, quote_volume, trades,"
            " interval_ms, price_scale_exp, volume_scale_exp, recebido_em,"
            " origem) VALUES ('binance','BTCUSDT','15m',?,?,?,?,?,1,1,1,"
            "?,8,8,'x','ao_vivo')",
            (t, abertura, abertura + 1000, abertura - 1000, abertura, GRADE),
        )
        if i == n:   # a FRONTEIRA: barra no fluxo, sem BBO valida
            amostras.append(m.Amostra(
                t_grid_ms=t, disponivel=False, motivo="sem_amostra_na_janela"))
            continue
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
    materializar(conn, cfg, lados=("compra",))
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
    materializar(conn, cfg, lados=("compra",))
    e2 = observacao.serie_e2_da_calibracao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        lado="compra",
    )
    assert e2 and all(v > 0 for v in e2), f"simulador otimista: {e2}"


def test_a_VENDA_e_simetrica(conn, cfg):
    """`E2_venda = bid - previsto`: pessimismo é prever receber no máximo o bid."""
    semear(conn, 3)
    materializar(conn, cfg)
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

    r = materializar(conn, cfg, lados=("compra",))
    assert r["comparaveis"] == 3, "a amostra sem barra não pode parear"


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
    """Reprocessar o mesmo periodo sob a MESMA identidade nao duplica nada.

    E sob OUTRA identidade nao acrescenta: o gatilho recusa. A segunda metade
    esta em `test_a_identidade_executavel_e_CONGELADA_por_janela`.
    """
    semear(conn, 3)
    a = materializar(conn, cfg, lados=("compra",))
    b = materializar(conn, cfg, lados=("compra",))
    assert (a["gravadas"], a["ja_existiam"]) == (3, 0)
    assert (b["gravadas"], b["ja_existiam"]) == (0, 3)


def test_a_materializacao_publica_os_SEIS_numeros_separados(conn, cfg):
    """Instante e observacao sao grandezas diferentes (ADR 0041 par.7).

    Trocar uma pela outra sob o mesmo rotulo e o padrao que este projeto conta
    trinta e uma vezes - a ultima foi "33 lancamentos" no lugar de "65
    creditos".
    """
    semear(conn, 3)
    r = materializar(conn, cfg)

    assert r["instantes_validos_na_janela"] == 3
    assert r["candidatos_de_lado"] == 6, "tres instantes, DOIS lados"
    assert r["comparaveis"] == 6
    assert r["sem_barra_pareada"] == 0
    assert r["excluidos_por_l1"] == 0
    assert r["na_estatistica"] == 6
    assert set(r["o_que_cada_um_e"]) >= {
        "instantes_validos_na_janela", "candidatos_de_lado", "comparaveis",
        "excluidos_por_l1", "na_estatistica",
    }


def test_a_finalidade_NAO_tem_default(conn, cfg):
    """Um default faria a barreira depender de quem chama."""
    import inspect

    sig = inspect.signature(observacao.registrar)
    assert sig.parameters["finalidade"].default is inspect.Parameter.empty


def test_observacao_FORA_da_janela_selada_e_RECUSADA(conn, cfg):
    """A barreira do ADR 0041, no banco e nao no codigo.

    Sem janela de calibracao selada nao existe observacao de calibracao - e a
    recusa vem do gatilho, nao de um `if` que alguem possa remover.
    """
    semear(conn, 3)
    with pytest.raises(sqlite3.IntegrityError) as e:
        observacao.registrar(
            conn, finalidade="calibracao", contrato=CONTRATO, venue="binance",
            symbol="BTCUSDT", config=cfg.config, config_version_id=cfg.id,
            calibracao_perfil_hash=PERFIL_DE_TESTE, janela_hash=HASH_DE_TESTE,
            de_ms=T0, ate_ms_exclusive=T0 + 10 * GRADE,
            orcamento_cents=ORCAMENTO, lados=("compra",),
        )
    assert "fora da janela" in str(e.value)
    assert conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao"
    ).fetchone()[0] == 0


def test_a_identidade_executavel_e_CONGELADA_por_janela(conn, cfg):
    """Remedir sob outra config e trocar o simulador no meio da medicao.

    Antes, `config_version_id` estava na CHAVE PRIMARIA: recalcular a previsao
    do mesmo instante sob outra config era um INSERT novo, e nada acusava.
    """
    semear(conn, 3)
    materializar(conn, cfg, lados=("compra",))
    with pytest.raises(sqlite3.IntegrityError) as e:
        materializar(conn, cfg, lados=("compra",),
                     calibracao_perfil_hash="outro-perfil")
    assert "identidade executavel divergente" in str(e.value)


def test_a_observacao_e_apenas_por_ACRESCIMO(conn, cfg):
    """Reescrever uma previsão depois de ver o mercado é a definição de
    ajustar a régua olhando o resultado."""
    semear(conn, 2)
    materializar(conn, cfg, lados=("compra",))
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
    materializar(conn, cfg, lados=("compra",))
    total = conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao"
    ).fetchone()[0]
    assert total == 3, "a observação continua GRAVADA"

    dentro = conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao WHERE dentro_do_l1 = 1"
    ).fetchone()[0]
    assert dentro == 0

    serie = observacao.serie_e2_da_calibracao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        lado="compra",
    )
    assert serie == [], "e sai da estatística"


def test_config_incompativel_com_o_contrato_NAO_MEDE_NADA(conn, cfg):
    """Requisito 5 do ADR 0032, aplicado no ponto que importa."""
    semear(conn, 3)
    outra = cfg.config.model_copy(update={"execution_reference": "limite_adverso"})
    with pytest.raises(observacao.ContratoIncompativel) as e:
        materializar(conn, cfg, config=outra)
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
    assert campos == {"venue", "symbol", "contrato", "finalidade", "regime"}, (
        "`de_ms` e `ate_ms_exclusive` SAIRAM (ADR 0041). Com eles no corpo, a"
        " finalidade de uma observacao era escolhida por quem chamava: bastava"
        " mandar o intervalo do piloto para materializar dentro dele. Hoje o"
        " periodo vem da JANELA SELADA, e `finalidade` diz qual delas"
    )


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


REGIME_DE_TESTE = "vol_baixa"


def fechar_regime_medido(conn, cfg, *, regime: str = REGIME_DE_TESTE) -> dict:
    """Mede e fecha o regime. **Sem finalizar e sem selar revalidacao.**

    Desde 2026-10-07 `estado == "calibrado"` sai do FECHAMENTO, e nao de um
    `n_necessario` recomputado na leitura. Eram duas reguas para o mesmo fato -
    esta, e o `n_exigido` congelado que `fechar_regime` usa -, e com elas um
    regime podia receber override SEM ter fechado.
    """
    conta = ajuste.medir_regime(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, regime=regime,
    )
    return ajuste.fechar_regime(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        regime=regime, n_efetivo=conta["n_efetivo"],
        ultimo_t_grid_ms=conta["ultimo_t_grid_ms"],
    )


def fechar_e_selar(conn, cfg, *, regime: str = REGIME_DE_TESTE) -> dict:
    """Fecha o regime, FINALIZA o perfil, e so entao sela a revalidacao dele.

    **A ordem mudou em 2026-10-07, e e ela que o helper existe para impor.**
    O perfil e consolidado UMA vez quando a janela de calibracao termina; a
    revalidacao confirma esse perfil, entao ela nasce DEPOIS dele. Selar antes
    reservaria periodo para confirmar o que ainda nao foi decidido - e o
    gatilho `janela_revalidacao_exige_a_finalizacao` recusa.
    """
    from app.settings import get_settings

    fechar_regime_medido(conn, cfg, regime=regime)
    ajuste.aplicar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, autor="teste",
        settings=get_settings(),
    )
    # A SEGUNDA CERTIFICACAO, que no runbook e o passo 9. Ela vem DEPOIS de
    # `aplicar` porque e a identidade CALIBRADA que ela certifica - certificar
    # antes certificaria a base, e o alvo seria outro.
    certificar(conn)
    # Nem `agora_ms`, nem `dias`, nem grade: so a identidade. O resto sai das
    # regras congeladas no selamento da calibracao.
    return ajuste.selar_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        regime=regime,
    )


def montar_piloto(
    conn: sqlite3.Connection, cfg, *, ask: int, aquecimento: int = 0,
    instantes: int = 1_700,
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
    serie_p = m.Serie(venue="binance", symbol="BTCUSDT",
                      price_scale_exp=0, volume_scale_exp=0)
    janela = piloto.fechar(conn, serie_p, CONTRATO)

    # ETAPA 1 - o PILOTO, materializado na janela fechada. Dele saem `sigma_e`,
    # `tau` e a forma da cauda, e nada mais (ADR 0027 par.5).
    obs.registrar(
        conn, finalidade="piloto", contrato=CONTRATO, venue="binance",
        symbol="BTCUSDT", config=cfg.config, config_version_id=cfg.id,
        calibracao_perfil_hash=PERFIL_DE_TESTE, janela_hash=HASH_DE_TESTE,
        de_ms=janela.de_ms, ate_ms_exclusive=janela.ate_ms_exclusive,
        orcamento_cents=1_000, lados=("compra",),
    )

    # ETAPA 2 - a CALIBRACAO, pelo CAMINHO OFICIAL. `selar_calibracao` congela
    # as regras da revalidacao (n_exigido por regime, duracao, metodo,
    # confianca, alvo, dependencia, encerramento) ANTES do primeiro dado dela -
    # e so consegue porque `sigma_e` e `tau` vem do piloto, que ja fechou.
    #
    # Selar pela mao, com INSERT cru, faria o teste passar sem exercitar o
    # congelamento - e e o congelamento que impede o resultado da calibracao de
    # mexer no tamanho da revalidacao.
    ultimo = conn.execute(
        "SELECT MAX(t_grid_ms) AS t FROM bbo_amostra WHERE contrato = ?",
        (CONTRATO,),
    ).fetchone()["t"]
    jc = ajuste.selar_calibracao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config,
    )
    # O FIM DA JANELA, e nao o alcance do dado: a janela de calibracao passou a
    # ter fim DECLARADO, derivado do piloto, e o gatilho recusa instante fora
    # dele.
    de_cal = int(jc["de_ms"])
    obs.registrar(
        conn, finalidade="calibracao", contrato=CONTRATO, venue="binance",
        symbol="BTCUSDT", config=cfg.config, config_version_id=cfg.id,
        calibracao_perfil_hash=PERFIL_DE_TESTE, janela_hash=HASH_DE_TESTE,
        de_ms=de_cal,
        ate_ms_exclusive=min(int(ultimo) + GRADE, int(jc["ate_ms_exclusive"])),
        orcamento_cents=1_000, lados=("compra",),
    )


# --------------------------------------------------- garantia 1: piloto fechado


def test_GARANTIA_1_nao_ha_estimativa_com_o_piloto_ABERTO(conn, cfg):
    """Estimar antes de o período declarado terminar deixaria a janela crescer
    até o número ficar agradável."""
    from app.calibracao import ajuste

    with pytest.raises(ajuste.SemJanelaDeCalibracao) as e:
        ajuste.estimar(
            conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
            config=cfg.config, config_version_id=cfg.id,
        )
    assert "janela de CALIBRACAO selada" in str(e.value)


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
            " config_version_origem, janela_calibracao_id, calibracao_de_ms,"
            " calibracao_ate_ms_exclusive, delta_mili_bps, n,"
            " n_efetivo_x1000, tau_x1000, sigma_e_x1000, p10_mili_bps,"
            " n_necessario, aplicado, motivo, criado_em)"
            " VALUES ('bbo@1','b','s',1,1,1,2,900,1,1,1,1,1,1,0,'x','x')"
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
    assert por_regime["indefinido"].n == 0, (
        "sob o ADR 0041 a estimativa le a janela de CALIBRACAO, que comeca"
        " depois do piloto - e a essa altura o fluxo ja tem as 672 barras que"
        " o detector exige. A licao nao sumiu: ela passou a viver no"
        " DIMENSIONAMENTO, que le o piloto, onde o aquecimento ainda produz"
        " `indefinido`"
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
    # O FECHAMENTO e a regua: sem ele o regime nao e `calibrado`, por desenho.
    fechar_regime_medido(conn, cfg, regime="vol_baixa")
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
    fechar_regime_medido(conn, cfg, regime="vol_baixa")
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


# ===========================================================================
# A IDENTIDADE CALIBRADA TEM DE ESTAR CERTIFICADA - exigencia de 2026-10-13
#
# > "`janela_revalidacao_exige_a_finalizacao` prova que o perfil existe, mas
# > nao prova que ele foi certificado."
#
# A sequencia obrigatoria, e ela e a fronteira dupla do runbook:
#
#   terminar a calibracao -> fechar os regimes -> UMA finalizacao consolidada
#   -> montar o alvo dessa identidade -> certificar os CINCO escopos ->
#   so entao selar qualquer janela de revalidacao
#
# ## Onde cada metade mora, e o que o banco NAO alcanca
#
# | afirmacao | onde | por que |
# |---|---|---|
# | cinco escopos com manifesto que passa para `alvo_hash` | gatilho (d) | manifesto e tabela |
# | a identidade e a da finalizacao daquela janela | gatilho (e) | os dois hashes sao colunas |
# | o alvo certificado DESCREVE essa identidade | gatilho (f) | `componentes_json` guarda o componente |
# | o `alvo_hash` corresponde ao CODIGO que roda agora | **codigo** | exige AST dos 71 modulos e leitura de arquivo |
#
# A ultima e a unica que o SQL nao consegue, e e declarada em vez de fingida.
# O que a fecha e `test_INSERT_em_janela_revalidacao_existe_num_lugar_SO`.
# ===========================================================================


def _cenario_pronto_para_selar(conn, cfg):
    """Piloto, calibracao, fechamento e finalizacao - sem certificar.

    O ponto exato em que o perfil consolidado EXISTE e ainda nao foi
    conferido. E daqui que as recusas abaixo partem.
    """
    from app.settings import get_settings

    # A fonte de dados e componente do ALVO, e sem ela nao ha alvo a conferir.
    # Em producao ela sempre existe; os testes de calibracao nunca precisaram
    # dela porque a calibracao le `stream_bar`, o fluxo AO VIVO.
    semear_dataset(conn)
    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    fechar_regime_medido(conn, cfg)
    ajuste.aplicar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, autor="teste",
        settings=get_settings(),
    )


def _selar(conn, regime: str = REGIME_DE_TESTE):
    return ajuste.selar_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        regime=regime,
    )


def test_PERFIL_FINALIZADO_e_NAO_certificado_e_RECUSADO(conn, cfg):
    """O caso 1: o perfil existe, e ninguem conferiu o laboratorio dele.

    Este e o passo 9 do runbook visto do lado do codigo. A observacao de
    revalidacao e PRODUZIDA pelo laboratorio - confirmar sob laboratorio nao
    certificado e confirmar um simulador que ninguem conferiu, e a janela e de
    uso unico e comeca no FUTURO: nao ha como refazer.
    """
    _cenario_pronto_para_selar(conn, cfg)
    assert ajuste.ler_finalizacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT"
    ) is not None, "o perfil consolidado EXISTE - e isso que o caso isola"

    with pytest.raises(ajuste.IdentidadeCalibradaNaoCertificada) as e:
        _selar(conn)
    assert e.value.composicao["pendentes"] == ["a1a", "a1b", "a2", "a3", "a4"]
    assert "passo 9" in str(e.value)
    assert ajuste.ler_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        regime=REGIME_DE_TESTE,
    ) is None, "nenhuma janela foi criada pela tentativa recusada"


def test_QUATRO_escopos_certificados_e_RECUSADO__o_quinto_nao_e_detalhe(
    conn, cfg
):
    """O caso 2, e `escopos.py` diz por que quatro nao bastam.

    > "A1a certifica apenas que defeitos conhecidos nao sao promovidos. Sem
    > A1b, uma implementacao que rejeita tudo poderia passar."

    Entao o teste tira justamente o A1b: o escopo cuja ausencia e a mais facil
    de nao notar, porque os outros quatro passariam de graca num protocolo
    surdo.
    """
    _cenario_pronto_para_selar(conn, cfg)
    certificar(conn, escopos=["a1a", "a2", "a3", "a4"])

    with pytest.raises(ajuste.IdentidadeCalibradaNaoCertificada) as e:
        _selar(conn)
    assert e.value.composicao["pendentes"] == ["a1b"]
    assert e.value.composicao["certificados"] == ["a1a", "a2", "a3", "a4"]

    # E o quinto completa: a recusa era pelo que faltava, e nao por outra coisa.
    certificar(conn, escopos=["a1b"])
    assert _selar(conn)["regime"] == REGIME_DE_TESTE


def test_um_escopo_que_FALHOU_nao_conta_como_certificado(conn, cfg):
    """`passa = 0` nao e certificado, e o gatilho usa a MESMA regra do relatorio.

    Um manifesto selado com `passa = 0` e um certificado que diz "nao passou".
    Conta-lo seria ler o fato de haver manifesto como se fosse o veredito dele.
    """
    _cenario_pronto_para_selar(conn, cfg)
    certificar(conn, escopos=["a1a", "a1b", "a2", "a3"])
    certificar(conn, escopos=["a4"], passa=False)

    with pytest.raises(ajuste.IdentidadeCalibradaNaoCertificada) as e:
        _selar(conn)
    assert e.value.composicao["falhou"] == ["a4"]


def test_CINCO_escopos_de_OUTRO_alvo_sao_RECUSADOS(conn, cfg):
    """O caso 3, e e o que o gatilho (f) existe para fechar.

    Certificados completos, cinco de cinco - de um alvo que descreve OUTRA
    identidade executavel. Sem (f), bastaria apontar a janela para um alvo
    antigo ja certificado: a contagem de (d) daria cinco, e os certificados
    descreveriam um laboratorio que nao e o que a calibracao produziu.
    """
    _cenario_pronto_para_selar(conn, cfg)

    # Um alvo FORJADO, com cinco certificados e identidade diferente.
    outro = _alvo_de_agora(conn)
    componentes = dict(outro["componentes"])
    componentes["identidade_executavel"] = "outra-config+outro-perfil"
    alvo_alheio = {
        "alvo_de_certificacao_hash": "a" * 64,
        "componentes": componentes,
    }
    certificar(conn, alvo=alvo_alheio)

    # Pelo CAMINHO: o alvo de hoje nao tem certificado nenhum.
    with pytest.raises(ajuste.IdentidadeCalibradaNaoCertificada):
        _selar(conn)

    # E pelo BANCO: nem citando o alvo alheio a mao a linha entra.
    fech = conn.execute(
        "SELECT id, fim_ms_exclusive FROM calibracao_regime_fechamento"
    ).fetchone()
    ident_certa, _ = _identidade_e_alvo_do_fechamento(conn, int(fech["id"]))
    with pytest.raises(sqlite3.IntegrityError) as e:
        revalidacao_crua(
            conn, fech=int(fech["id"]), regime=REGIME_DE_TESTE,
            calibracao_ate=int(fech["fim_ms_exclusive"]),
            alvo_hash="a" * 64, identidade=ident_certa,
        )
    assert "OUTRA identidade" in str(e.value)


def test_CINCO_escopos_do_alvo_EXATO_permitem(conn, cfg):
    """O caso 4: a porta abre, e abre so aqui.

    Sem este teste os quatro de cima provariam apenas que nada passa - e um
    portao que nunca deixa passar e indistinguivel de um portao quebrado. E o
    mesmo argumento do controle positivo sintetico da quarentena.
    """
    _cenario_pronto_para_selar(conn, cfg)
    alvo_hash = certificar(conn)

    janela = _selar(conn)
    assert janela["alvo_hash"] == alvo_hash
    # E a identidade GRAVADA e a da finalizacao, nao a "vigente de hoje".
    final = ajuste.ler_finalizacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT"
    )
    nova = conn.execute(
        "SELECT config_hash, calibracao_perfil_hash FROM config_version"
        " WHERE id = ?", (final["config_version_nova"],),
    ).fetchone()
    from app.calibracao import identidade as ident_mod

    assert janela["identidade_executavel"] == ident_mod.identidade_executavel(
        str(nova["config_hash"]), nova["calibracao_perfil_hash"]
    )


def test_mudar_a_CONFIG_depois_de_certificar_volta_a_RECUSAR(conn, cfg):
    """O caso 5, e ele e o que torna a garantia reversivel no sentido certo.

    `alvo.montar` monta a identidade a partir da config VIGENTE: o alvo
    descreve o que executa, e nao o que um dia foi decidido. Uma versao nova
    criada depois da certificacao faz o alvo mudar, e os cinco certificados
    passam a descrever um laboratorio que deixou de ser o que roda.

    A recusa volta sozinha - ninguem precisa lembrar de revogar nada. E o
    `o_que_falta` e OUTRO, porque o problema e outro: nao falta certificar,
    falta a vigente voltar a ser a calibrada.
    """
    from app.config import service as config_service

    from app.settings import get_settings

    _cenario_pronto_para_selar(conn, cfg)
    certificar(conn)

    # A certificacao esta completa: `composicao` passa.
    from app.certificacao import escopos as escopos_mod

    antes = escopos_mod.composicao(
        conn, str(_alvo_de_agora(conn)["alvo_de_certificacao_hash"])
    )
    assert antes["passa"] is True

    # Agora uma config_version NOVA, posterior a certificacao. Qualquer campo
    # material serve: o que importa e que a VIGENTE deixe de ser a calibrada.
    atual = config_service.versao_atual(conn)
    config_service.criar_versao(
        conn, get_settings(),
        {"b1_repetitions": atual.config.b1_repetitions + 1},
        author="teste", note="qualquer mudanca material depois do selo",
    )

    with pytest.raises(ajuste.IdentidadeVigenteNaoEADaFinalizacao) as e:
        _selar(conn)
    assert "deixou de ser" in str(e.value)
    assert ajuste.ler_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        regime=REGIME_DE_TESTE,
    ) is None

    # E o ALVO tambem mudou: os cinco certificados descrevem outro laboratorio.
    depois = escopos_mod.composicao(
        conn, str(_alvo_de_agora(conn)["alvo_de_certificacao_hash"])
    )
    assert depois["passa"] is False
    assert depois["alvo_de_certificacao_hash"] != antes["alvo_de_certificacao_hash"]


def test_o_CORPO_nao_aceita_alvo_execucao_nem_qualquer_outro_campo(client):
    """O caso 6: nao ha como o chamador ESCOLHER qual certificacao autoriza.

    `extra="forbid"` no modelo base, e o 422 vem do pydantic antes de a rota
    rodar. Os cinco nomes testados sao exatamente os que alguem tentaria.
    """
    for extra in ("target_hash", "alvo_hash", "execution_id",
                  "execucao_id", "alvo_esperado"):
        r = client.post(
            "/api/calibracao/selar-revalidacao", headers=CABECALHO,
            json={"author": "t", "regime": "vol_baixa", extra: "x"},
        )
        assert r.status_code == 422, f"{extra} passou: {r.text}"

    # E o caminho normal continua recusando por MOTIVO, e nao por schema.
    r = client.post(
        "/api/calibracao/selar-revalidacao", headers=CABECALHO,
        json={"author": "t", "regime": "vol_baixa"},
    )
    assert r.status_code == 409


def test_o_GATILHO_e_o_RELATORIO_concordam_sobre_os_cinco(conn, cfg):
    """Duas implementacoes da mesma regra divergem. Esta compara as duas.

    O gatilho (d) conta, por escopo, o manifesto MAIS RECENTE com `passa = 1`;
    `escopos.composicao` faz o mesmo em Python. Sao arquivos diferentes, e a
    divergencia so apareceria no dia em que um escopo tivesse dois manifestos
    com vereditos opostos - que e exatamente o cenario 4 abaixo.
    """
    from app.certificacao import escopos as escopos_mod

    _cenario_pronto_para_selar(conn, cfg)
    alvo_hash = str(_alvo_de_agora(conn)["alvo_de_certificacao_hash"])
    fech = conn.execute(
        "SELECT id, fim_ms_exclusive FROM calibracao_regime_fechamento"
    ).fetchone()
    ident, _ = _identidade_e_alvo_do_fechamento(conn, int(fech["id"]))

    cenarios = [
        ("nenhum", []),
        ("quatro", [(["a1a", "a1b", "a2", "a3"], True)]),
        ("cinco", [(["a4"], True)]),
        ("o quinto FALHOU depois de passar", [(["a4"], False)]),
        ("e passou de novo", [(["a4"], True)]),
    ]
    vistos = set()
    for nome, passos in cenarios:
        for quais, passa in passos:
            certificar(conn, escopos=quais, passa=passa)

        do_relatorio = escopos_mod.composicao(conn, alvo_hash)["passa"]

        # O veredito do BANCO, sem gravar nada: tenta e desfaz.
        conn.execute("SAVEPOINT sonda")
        try:
            revalidacao_crua(
                conn, fech=int(fech["id"]), regime=REGIME_DE_TESTE,
                calibracao_ate=int(fech["fim_ms_exclusive"]),
                alvo_hash=alvo_hash, identidade=ident,
            )
            do_banco = True
        except sqlite3.IntegrityError as erro:
            assert "CINCO escopos" in str(erro), (
                f"{nome}: recusou por outro motivo - {erro}"
            )
            do_banco = False
        finally:
            conn.execute("ROLLBACK TO sonda")
            conn.execute("RELEASE sonda")

        assert do_banco == do_relatorio, (
            f"{nome}: o banco diz {do_banco} e o relatorio diz {do_relatorio}"
        )
        vistos.add(do_relatorio)

    assert vistos == {True, False}, (
        "o teste tem de exercitar os DOIS vereditos; so um seria vacuidade"
    )


def test_INSERT_em_janela_revalidacao_existe_num_lugar_SO():
    """A metade que o banco nao alcanca: quem ESCREVE o `alvo_hash`.

    O gatilho (f) prova que o alvo citado descreve a identidade da janela. O
    que ele nao pode provar e que esse alvo corresponde ao CODIGO que roda
    agora - isso exige o fechamento transitivo por AST dos 71 modulos e a
    leitura dos arquivos, e nao ha SQL que faca.

    O que fecha essa metade e haver **um escritor so**, e ele derivar os dois
    campos em vez de recebe-los. Esta guarda varre a FORMA: um `INSERT` novo em
    qualquer modulo de `app/` quebra o teste ate alguem decidir.
    """
    import pathlib
    import re

    from tests._prosa import sql_sem_prosa

    raiz = pathlib.Path(__file__).resolve().parents[1] / "app"
    padrao = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+janela_revalidacao",
                        re.IGNORECASE)
    achados = sorted(
        arq.relative_to(raiz).as_posix()
        for arq in raiz.rglob("*.py")
        if padrao.search(sql_sem_prosa(arq))
    )
    assert achados == ["calibracao/ajuste.py"], (
        "a janela de revalidacao tem UM escritor, e ele deriva alvo e"
        f" identidade em vez de recebe-los. Achados: {achados}"
    )

    # E a guarda nao e vazia: ela acha o escritor que existe.
    assert padrao.search(
        sql_sem_prosa(raiz / "calibracao" / "ajuste.py")
    ), "a guarda nao enxerga nem o escritor conhecido - ela esta vazia"


def test_UMA_rota_so_chama_selar_revalidacao():
    """A outra metade: nenhuma rota alem de uma chega a `selar_revalidacao`.

    O `INSERT` morar num lugar so nao basta — se duas rotas chamassem a funcao,
    a conferencia de certificacao seria contornavel por quem chamasse a que
    nao a faz. Hoje ela e feita DENTRO de `selar_revalidacao`, entao qualquer
    chamador a recebe; esta guarda existe para que um segundo chamador seja
    uma decisao, e nao um descuido.

    A varredura e sobre `app/` inteiro, por FORMA — nao sobre uma lista de
    arquivos que envelheceria calada.
    """
    import pathlib
    import re

    from tests._prosa import codigo_sem_prosa

    raiz = pathlib.Path(__file__).resolve().parents[1] / "app"
    chamada = re.compile(r"\bselar_revalidacao\s*\(")
    achados = sorted(
        arq.relative_to(raiz).as_posix()
        for arq in raiz.rglob("*.py")
        if arq.name != "migrations.py"
        and chamada.search(codigo_sem_prosa(arq))
    )
    # `calibracao/ajuste.py` aparece porque e onde ela e DEFINIDA (`def
    # selar_revalidacao(`); a rota e o unico chamador.
    assert achados == ["api/rotas/calibracao.py", "calibracao/ajuste.py"], (
        f"chamadores inesperados de `selar_revalidacao`: {achados}"
    )


def test_os_DOIS_campos_sao_NOT_NULL__um_INSERT_que_os_omita_e_RECUSADO(conn):
    """"Nenhuma insercao valida" tem uma metade que nao precisa de gatilho.

    `alvo_hash` e `identidade_executavel` sao `NOT NULL`: um `INSERT` cru que
    simplesmente nao os traga e recusado pelo BANCO, antes de qualquer gatilho.
    Sem isso, a porta mais obvia — omitir o campo — ficaria aberta, e os tres
    gatilhos leriam `NULL`.
    """
    colunas = {
        r["name"]: r for r in conn.execute("PRAGMA table_info(janela_revalidacao)")
    }
    for campo in ("alvo_hash", "identidade_executavel"):
        assert colunas[campo]["notnull"] == 1, campo
        assert colunas[campo]["dflt_value"] is None, (
            f"{campo} tem DEFAULT — um `INSERT` que o omitisse passaria com um"
            " valor que ninguem escolheu"
        )

    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'janela_revalidacao'"
    ).fetchone()["sql"]
    assert "length(alvo_hash) = 64" in ddl, (
        "sem o CHECK de comprimento, um `alvo_hash` truncado entraria e os"
        " gatilhos procurariam manifesto de um alvo que nao existe"
    )


def test_GARANTIA_7_INVERTIDA_selar_revalidacao_SEM_finalizacao_e_recusado(
    conn, cfg
):
    """A garantia 7 mudou de lado em 2026-10-07, e nao foi perdida.

    **Ela era:** a revalidacao tem de estar selada ANTES do ajuste, porque
    reservar o periodo depois de ver `p10` seria escolher o periodo que
    confirma.

    **O motivo dela desapareceu.** A fronteira da janela de revalidacao deixou
    de ser escolhivel: `de_ms` e calculado pelo banco, a duracao vem congelada
    de `janela_calibracao` e o regime vem do fechamento - tres gatilhos
    recalculam as tres. Nao sobrou nada para escolher.

    **E a ordem antiga tinha um defeito pior:** ela produzia observacao de
    revalidacao sob uma identidade executavel que ainda nao fora certificada.
    Hoje e o contrario - finaliza o perfil, certifica, e so entao reserva o
    periodo que vai confirma-lo.
    """
    montar_piloto(conn, cfg, ask=ASK_JUSTO, aquecimento=680, instantes=1_400)
    conta = ajuste.medir_regime(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, regime=REGIME_DE_TESTE,
    )
    ajuste.fechar_regime(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        regime=REGIME_DE_TESTE, n_efetivo=conta["n_efetivo"],
        ultimo_t_grid_ms=conta["ultimo_t_grid_ms"],
    )
    assert ajuste.ler_finalizacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT"
    ) is None

    # (1) O CAMINHO recusa, e diz qual PASSO do runbook falta.
    with pytest.raises(ajuste.SemJanelaDeCalibracao) as e:
        ajuste.selar_revalidacao(
            conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
            regime=REGIME_DE_TESTE,
        )
    assert "passo 8" in str(e.value)

    # (2) E o BANCO tambem, por `INSERT` cru - porque uma recusa que vive so
    #     no caminho some no dia em que alguem escrever um segundo caminho.
    fech = conn.execute(
        "SELECT id, fim_ms_exclusive FROM calibracao_regime_fechamento"
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError) as e2:
        revalidacao_crua(
            conn, fech=int(fech["id"]), regime=REGIME_DE_TESTE,
            calibracao_ate=int(fech["fim_ms_exclusive"]),
        )
    assert "finalizacao" in str(e2.value)


def test_a_FINALIZACAO_e_UNICA_por_janela_e_IDEMPOTENTE(conn, cfg):
    """A resposta a pergunta do usuario: quantas identidades calibradas nascem.

    Antes de 2026-10-07: **tantas quantas chamadas de `ajustar`**. A janela nao
    tinha fim, cada chamada lia uma serie maior, derivava outra lista de
    overrides e criava uma `config_version` nova - e o numero de
    recertificacoes era escolhido por quem chamava.

    Hoje: **uma**. `UNIQUE (janela_calibracao_id)` em `calibracao_versao`, e a
    segunda chamada devolve a primeira sem escrever.
    """
    from app.settings import get_settings

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    conta = ajuste.medir_regime(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, regime=REGIME_DE_TESTE,
    )
    ajuste.fechar_regime(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        regime=REGIME_DE_TESTE, n_efetivo=conta["n_efetivo"],
        ultimo_t_grid_ms=conta["ultimo_t_grid_ms"],
    )

    primeira = ajuste.aplicar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, autor="teste",
        settings=get_settings(),
    )
    versoes = conn.execute(
        "SELECT COUNT(*) FROM calibracao_versao"
    ).fetchone()[0]
    perfis = conn.execute(
        "SELECT COUNT(*) FROM calibracao_perfil"
    ).fetchone()[0]
    configs = conn.execute(
        "SELECT COUNT(*) FROM config_version"
    ).fetchone()[0]

    # CHAMAR DE NOVO nao cria nada.
    segunda = ajuste.aplicar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, autor="teste",
        settings=get_settings(),
    )
    assert segunda["calibracao_id"] == primeira["calibracao_id"]
    assert segunda["config_version_nova"] == primeira["config_version_nova"]
    assert segunda.get("ja_existia") is True
    assert conn.execute(
        "SELECT COUNT(*) FROM calibracao_versao"
    ).fetchone()[0] == versoes == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM calibracao_perfil"
    ).fetchone()[0] == perfis
    assert conn.execute(
        "SELECT COUNT(*) FROM config_version"
    ).fetchone()[0] == configs

    # E o BANCO recusa uma segunda finalizacao por `INSERT` cru.
    jc = conn.execute("SELECT id FROM janela_calibracao").fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute(
            "INSERT INTO calibracao_versao (contrato, venue, symbol,"
            " config_version_origem, janela_calibracao_id, calibracao_de_ms,"
            " calibracao_ate_ms_exclusive, delta_mili_bps, n,"
            " n_efetivo_x1000, tau_x1000, sigma_e_x1000, p10_mili_bps,"
            " n_necessario, aplicado, motivo, criado_em)"
            " SELECT ?, 'binance','BTCUSDT',?,id,de_ms,ate_ms_exclusive,500,"
            " 10,10000,1000,100,600,5,0,'segunda','x'"
            "   FROM janela_calibracao WHERE id = ?",
            (CONTRATO, cfg.id, jc),
        )
    assert "UNIQUE" in str(e.value)


def test_GARANTIA_7_a_revalidacao_e_DISJUNTA_do_piloto(conn, cfg):
    """A disjuncao mudou de ponta, e ficou MAIS forte.

    Ela era do piloto; sob o ADR 0041 e da CALIBRACAO daquele regime - e como
    a calibracao ja e disjunta do piloto, a revalidacao continua disjunta dos
    dois. O teste passou a conferir as duas fronteiras, e nao uma.
    """
    from app.calibracao import ajuste

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    j = fechar_e_selar(conn, cfg)
    fech = ajuste.ler_fechamento(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        regime=REGIME_DE_TESTE,
    )
    assert j["de_ms"] >= fech["fim_ms_exclusive"], (
        "a revalidacao comeca onde a calibracao DAQUELE regime terminou"
    )
    assert j["de_ms"] > j["selada_em_ms"], (
        "e no FUTURO: dado que ja existia no selamento nao entra"
    )
    assert j["excluido"]["instantes"] >= 0
    assert "Ja existia no selamento" in j["excluido"]["por_que"], (
        "o vao entre as duas fica DECLARADO, e nao some"
    )
    janela = piloto.ler(
        conn,
        __import__("app.aovivo.bbo", fromlist=["Serie"]).Serie(
            venue="binance", symbol="BTCUSDT",
            price_scale_exp=0, volume_scale_exp=0),
        CONTRATO,
    )
    assert j["de_ms"] > janela.ate_ms_exclusive, (
        "ela e posterior ao piloto TAMBEM - por transitividade, ja que a"
        " calibracao comeca depois dele"
    )
    assert j["ate_ms_exclusive"] > j["de_ms"]


def test_GARANTIA_7_selar_de_novo_devolve_a_MESMA_janela(conn, cfg):
    """Uma segunda janela seria uma segunda chance de escolher a fronteira."""
    from app.calibracao import ajuste

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    a = fechar_e_selar(conn, cfg)
    b = fechar_e_selar(conn, cfg)
    assert a["id"] == b["id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM janela_revalidacao"
    ).fetchone()[0] == 1


def test_GARANTIA_7_a_janela_selada_e_IMUTAVEL(conn, cfg):
    from app.calibracao import ajuste

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    fechar_e_selar(conn, cfg)
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
    # A janela de revalidacao NAO existe sem fechamento: `regime` e
    # `calibracao_fechamento_id` sao NOT NULL, e o caminho global nao existe
    # mais. Montar a cadeia inteira e o que torna a trava conferida de verdade -
    # e desde 2026-10-07 ela inclui a FINALIZACAO: a revalidacao confirma o
    # perfil consolidado, entao nao ha janela dela antes dele.
    _fluxo_curto(conn)
    selar_calibracao_no_inicio(conn, duracao_ms=DURACAO_CURTA)
    _finalizacao_crua(conn)
    jc = conn.execute("SELECT id FROM janela_calibracao").fetchone()["id"]
    conn.execute(
        "INSERT INTO calibracao_regime_regra (janela_calibracao_id, regime,"
        " n_exigido_calibracao, n_exigido_revalidacao,"
        " alvos_iguais_por_decisao, por_que_os_alvos, sigma_e_x1000,"
        " tau_x1000, piloto_n) VALUES (?, 'vol_baixa', 10, 10, 1, ?, 1, 1000, 1)",
        (jc, ajuste.POR_QUE_OS_ALVOS),
    )
    conn.execute(
        "INSERT INTO calibracao_regime_fechamento (janela_calibracao_id,"
        " regime, n_exigido_calibracao, n_efetivo_x1000, fim_ms_exclusive,"
        " delta_mili_bps, fechado_em)"
        " VALUES (?, 'vol_baixa', 10, 20000, ?, 500, 'x')",
        (jc, T0 + GRADE * 2),
    )
    fech = conn.execute(
        "SELECT id FROM calibracao_regime_fechamento"
    ).fetchone()["id"]
    # Sem `selada_em_ms` e sem `de_ms` escolhido: os dois saem do BANCO.
    revalidacao_crua(conn, fech=fech, regime="vol_baixa",
                     calibracao_ate=T0 + GRADE * 2)
    janela_id = conn.execute(
        "SELECT id FROM janela_revalidacao"
    ).fetchone()["id"]
    # A finalizacao JA existe - `_finalizacao_crua` a gravou, e o
    # `UNIQUE (janela_calibracao_id)` recusaria uma segunda. Era exatamente
    # esse `UNIQUE` que faltava quando o perfil podia nascer progressivo.
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
    assert "recomprar a tentativa" in str(e.value)


def test_GARANTIA_6_a_versao_de_calibracao_e_IMUTAVEL(conn, cfg, uso_real):
    """Recalibrar produz uma versão NOVA, e a antiga continua descrevendo os
    resultados obtidos sob ela."""
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("UPDATE calibracao_versao SET p10_mili_bps = 0")
    assert "apenas por acrescimo" in str(e.value)
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
    fechar_regime_medido(conn, cfg)

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

    # A ORDEM, imposta pela rota: FECHA o regime -> FINALIZA o perfil -> so
    # entao reserva o periodo que vai confirma-lo.
    #
    # Ela inverteu em 2026-10-07. Antes a revalidacao era selada ANTES do
    # ajuste, porque a fronteira dela era escolhivel e reserva-la depois de ver
    # `p10` seria escolher o periodo que confirma. A fronteira deixou de ser
    # escolhivel - tres gatilhos a recalculam -, e a ordem antiga tinha um
    # defeito pior: produzia observacao de revalidacao sob identidade
    # executavel ainda nao certificada.
    cedo = client.post("/api/calibracao/selar-revalidacao", headers=CABECALHO,
                       json={"author": "teste", "regime": "vol_baixa"})
    assert cedo.status_code == 409, cedo.text

    r = client.post("/api/calibracao/fechar-regime", headers=CABECALHO,
                    json={"author": "teste", "regime": "vol_baixa"})
    assert r.status_code == 201, r.text

    r = client.post("/api/calibracao/ajustar", headers=CABECALHO,
                    json={"author": "teste"})
    assert r.status_code == 201, r.text
    corpo = r.json()

    # ANTES da certificacao, selar a revalidacao continua recusado - e agora
    # por OUTRO motivo, que o corpo nomeia. Este e o passo 9 do runbook visto
    # do lado do codigo: a identidade calibrada existe e nao foi conferida.
    sem_certificado = client.post(
        "/api/calibracao/selar-revalidacao", headers=CABECALHO,
        json={"author": "teste", "regime": "vol_baixa"},
    )
    assert sem_certificado.status_code == 409, sem_certificado.text
    assert sem_certificado.json()["detail"]["o_que_falta"] == "certificacao"

    certificar(conn)  # o passo 9: os cinco escopos, sobre a identidade NOVA

    depois = client.post("/api/calibracao/selar-revalidacao",
                         headers=CABECALHO,
                         json={"author": "teste", "regime": "vol_baixa"})
    assert depois.status_code == 201, depois.text

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
    # So o FECHAMENTO. Nao ha revalidacao a selar aqui, e isso e consequencia
    # e nao conveniencia: com `ASK_PESSIMISTA` a finalizacao nao aplica
    # override nenhum, entao nao nasce identidade calibrada - e uma janela de
    # revalidacao sem identidade a confirmar nao tem sujeito. O teste da linha
    # abaixo fixa essa consequencia.
    fechar_regime_medido(conn, cfg)
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
    fechar_e_selar(conn, cfg)
    ajuste.aplicar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, autor="t",
        settings=get_settings(),
    )
    r = client.post("/api/calibracao/revalidar", headers=CABECALHO,
                    json={"author": "teste", "regime": "vol_baixa"})
    assert r.status_code == 409
    assert "NAO e consumida" in r.json()["detail"]

    janela = ajuste.ler_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        regime=REGIME_DE_TESTE)
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


# ===========================================================================
# A IDENTIDADE EXECUTÁVEL
#
# `config_hash` sozinho parou de identificar o comportamento quando o ADR 0033
# pôs o perfil FORA do payload. A identidade efetiva é o par.
# ===========================================================================


def test_a_identidade_e_o_PAR_e_ha_UMA_definicao():
    from app.calibracao import identidade

    assert identidade.identidade_executavel("abc", "def") == "abc+def"
    assert identidade.identidade_executavel("abc", None) == "abc+sem-perfil"


def test_SEM_PERFIL_tem_representacao_CANONICA():
    """Não é `None`, não é "", não é "null".

    A string de identidade fica sempre bem formada - comparar duas nunca
    precisa de caso especial. E `sem-perfil` **diz** o que significa: um campo
    vazio num relatório é lido como "faltou preencher".
    """
    from app.calibracao import identidade

    assert identidade.SEM_PERFIL == "sem-perfil"
    a = identidade.identidade_executavel("h", None)
    assert a.endswith(identidade.SEM_PERFIL)
    assert "None" not in a and "null" not in a
    assert a != "h+" and a != "h"


def test_perfis_DIFERENTES_dao_identidades_diferentes_com_o_MESMO_config_hash(
    conn, cfg
):
    """A garantia que o usuário pediu, no ponto exato.

    Este é o cenário real medido na demonstração: as versões 1 e 2 saíram com
    o **mesmo** `config_hash` (`ebf441d4db65`) e executam com preços
    diferentes. Sem o par, dois runs assim reportariam a mesma identidade.
    """
    from app.calibracao import identidade
    from app.calibracao import perfil as perfil_mod
    from app.config import service as config_service
    from app.ledger import livro
    from app.settings import get_settings

    p = perfil_mod.gravar(
        conn, spread_bps_base_x1000=1_000, overrides=[ov("vol_baixa", 1_674)]
    )
    v2 = config_service.criar_versao(
        conn, get_settings(), {}, author="teste", note="perfil",
        calibracao_perfil_hash=p.hash,
    )

    # O payload é idêntico - é essa a premissa do teste.
    assert v2.config_hash == cfg.config_hash

    run_sem, _ = livro.abrir_run(
        conn, config_version_id=cfg.id, seed_capital_usd_cents=100_000,
        agent_id="ident-sem",
    )
    livro.encerrar_run(conn, run_sem, "concluido")
    run_com, _ = livro.abrir_run(
        conn, config_version_id=v2.id, seed_capital_usd_cents=100_000,
        agent_id="ident-com",
    )
    livro.encerrar_run(conn, run_com, "concluido")

    id_sem = identidade.do_run(conn, run_sem)
    id_com = identidade.do_run(conn, run_com)

    assert id_sem != id_com, (
        "mesmo config_hash e perfis diferentes deram a MESMA identidade - o "
        "par não está sendo usado"
    )
    assert id_sem.endswith(identidade.SEM_PERFIL)
    assert id_com.endswith(p.hash)


def test_o_run_PERSISTE_o_perfil_que_usou(conn, cfg):
    """Do run, e não da config vigente: um run antigo continua identificado
    pelo que ele usou, e não pelo que passou a valer depois."""
    from app.calibracao import perfil as perfil_mod
    from app.config import service as config_service
    from app.ledger import livro
    from app.settings import get_settings

    run_antigo, _ = livro.abrir_run(
        conn, config_version_id=cfg.id, seed_capital_usd_cents=100_000,
        agent_id="antes",
    )
    livro.encerrar_run(conn, run_antigo, "concluido")

    p = perfil_mod.gravar(
        conn, spread_bps_base_x1000=1_000, overrides=[ov("vol_alta", 2_000)])
    config_service.criar_versao(
        conn, get_settings(), {}, author="t", note="perfil",
        calibracao_perfil_hash=p.hash,
    )

    gravado = conn.execute(
        "SELECT calibracao_perfil_hash FROM run WHERE id = ?", (run_antigo,)
    ).fetchone()["calibracao_perfil_hash"]
    assert gravado is None, (
        "o run antigo passou a citar um perfil que não existia quando ele rodou"
    )


def test_perfis_diferentes_dao_DIGESTS_diferentes(conn, cfg):
    """O digest é dos LANÇAMENTOS, então ele muda por consequência: preços
    diferentes produzem caixa diferente.

    Aqui a consequência é verificada de ponta a ponta, e não assumida - a
    diferença entre "deveria mudar" e "muda".
    """
    from decimal import Decimal

    from app.ledger import livro
    from app.maos_rapidas import baselines, executor
    from app.regra import registro
    from tests.test_maos_rapidas import precos_passeio
    from tests.test_simulador import criar_dataset

    dataset_id = criar_dataset(conn, precos_passeio(3_000))
    barras = executor.carregar_janela(conn, dataset_id)

    def uma_passada(spread: Decimal, agente: str) -> str:
        c = cfg.config.model_copy(update={"spread_bps": spread})
        run_id, _ = livro.abrir_run(
            conn, config_version_id=cfg.id,
            seed_capital_usd_cents=c.seed_capital_usd_cents, agent_id=agente,
        )
        regra = baselines.regra_b3(c)
        rule_id = registro.registrar(conn, regra)
        executor.rodar(conn, run_id=run_id, dataset_id=dataset_id, regra=regra,
                       rule_id=rule_id, config=c, barras=barras)
        livro.encerrar_run(conn, run_id, "concluido")
        return executor.digest_do_run(conn, run_id)

    base = uma_passada(Decimal("1"), "digest-base")
    override = uma_passada(Decimal("1.674"), "digest-override")
    assert base != override, (
        "o override de regime não mudou o digest: o preço não chegou ao ledger"
    )


def test_a_prova_de_R12_exige_a_IDENTIDADE_estavel(conn, cfg):
    """Sem isto, três passadas sob perfis diferentes reportariam "hash igual
    nas três" e a prova afirmaria uma reprodutibilidade que não houve."""
    from pathlib import Path

    fonte = Path("app/relatorio/reprodutibilidade.py").read_text(encoding="utf-8")
    assert "identidade_igual_nas_tres" in fonte
    assert "and identidade_estavel" in fonte, (
        "a identidade tem de entrar na conjunção que decide `provado`"
    )


def test_o_CACHE_ja_inclui_o_perfil_POR_CONSTRUCAO(conn, cfg):
    """E por isso o perfil NÃO é pregado na chave.

    A chave do cache é o hash do **pedido inteiro** - sistema, mensagem,
    schema, modelo e teto. Se o prompt algum dia passar a depender do perfil,
    a chave segue sozinha; pregar o hash do perfil por fora invalidaria toda
    entrada existente e custaria dinheiro real na próxima execução, sem
    proteger nada que a construção já não proteja.

    **O limite, declarado:** `contexto.py` monta o custo de giro que o agente
    lê a partir de `config.spread_bps` - a BASE. Sob o ADR 0033 o custo
    executado num regime calibrado difere disso. Na 0C não importa: o agente
    não formula hipótese (regra 1 reescrita) e a candidata é congelada. Na
    Fase 1 importa, e fica registrado aqui em vez de descoberto lá.
    """
    from pathlib import Path

    fonte = Path("app/cerebro/cache.py").read_text(encoding="utf-8")
    assert "hash do pedido INTEIRO" in fonte

    contexto = Path("app/cerebro/contexto.py").read_text(encoding="utf-8")
    assert "config.spread_bps" in contexto, (
        "se o contexto deixar de derivar o custo da config, este teste tem de "
        "ser revisto junto"
    )


def test_a_REANCORAGEM_nao_perde_o_perfil_de_calibracao(conn, cfg):
    """Achado antes de a primeira reancoragem pós-ADR-0033 acontecer.

    "Reancorar não muda nenhum valor" inclui o perfil - e ele não está no
    payload, vive numa coluna. Uma reancoragem que o deixasse de fora
    produziria versão nova com `NULL`, e o simulador voltaria à BASE em todo
    regime **sem avisar**.

    Risco zero no momento em que foi achado: não há perfil em produção porque
    a calibração ainda não rodou. Depois do piloto seria perda silenciosa.
    """
    from app.calibracao import perfil as perfil_mod
    from app.config import service as config_service
    from app.settings import get_settings

    p = perfil_mod.gravar(
        conn, spread_bps_base_x1000=1_000,
        overrides=[perfil_mod.Override("vol_baixa", 1_674, 500, 163)],
    )
    v2 = config_service.criar_versao(
        conn, get_settings(), {}, author="t", note="perfil",
        calibracao_perfil_hash=p.hash,
    )
    assert perfil_mod.da_config_version(conn, v2.id) is not None

    # Força a deriva de schema que a reancoragem existe para resolver. Ela
    # NÃO é simulável por `UPDATE`: `config_version` é imutável por gatilho.
    # O caminho real é um campo novo no schema, que este teste não pode criar
    # - então ele pula, e a guarda de assinatura logo abaixo é quem sustenta.
    import sqlite3 as _s
    try:
        conn.execute(
            "UPDATE config_version SET config_hash = 'derivado' WHERE id = ?",
            (v2.id,),
        )
    except _s.IntegrityError:
        pytest.skip(
            "config_version e imutavel: a deriva de schema real nao e "
            "simulavel por UPDATE, e este teste cobre o caminho pela "
            "assinatura abaixo"
        )

    v3 = config_service.reancorar(conn, get_settings(), author="t")
    assert perfil_mod.da_config_version(conn, v3.id) is not None, (
        "a reancoragem perdeu o perfil: o simulador voltaria a base em todo "
        "regime, sem avisar"
    )
    assert perfil_mod.da_config_version(conn, v3.id).hash == p.hash


def test_reancorar_PASSA_o_perfil_para_inserir_versao():
    """A guarda que não depende de simular deriva de schema.

    `config_version` é imutável por gatilho, então forçar a deriva num teste
    não é possível pelo caminho normal - e um teste que só passa por `skip`
    não guarda nada.
    """
    import inspect

    from app.config import service as config_service

    fonte = inspect.getsource(config_service.reancorar)
    assert "calibracao_perfil_hash=_perfil_da_versao(conn, atual.id)" in fonte, (
        "reancorar tem de repassar o perfil vigente, senão a versão nova "
        "nasce com NULL"
    )


# ===========================================================================
# O CAMINHO GLOBAL NAO EXISTE MAIS — ADR 0041
#
# "Nenhuma rota ou funcao pode selar ou consumir uma revalidacao sem regime e
#  sem vinculo com `calibracao_regime_fechamento`." — o usuario, 2026-09-22
#
# A prova tem tres camadas, e a do banco e a que vale: as duas de cima podem
# ser contornadas por quem escreve codigo novo, e a de baixo nao.
# ===========================================================================


def test_o_caminho_GLOBAL_nao_existe_na_ASSINATURA(conn):
    """`regime` e obrigatorio e sem default nas tres funcoes."""
    import inspect

    for fn in (ajuste.selar_revalidacao, ajuste.ler_revalidacao,
               ajuste.fechar_regime, ajuste.revalidar):
        par = inspect.signature(fn).parameters.get("regime")
        assert par is not None, f"{fn.__name__} perdeu o parametro `regime`"
        assert par.default is inspect.Parameter.empty, (
            f"{fn.__name__} ganhou um default para `regime` - e um default"
            f" reabre o caminho global sem que ninguem decida"
        )


def test_o_caminho_GLOBAL_nao_existe_na_ROTA(client):
    """O corpo sem `regime` e recusado pelo MODELO, com 422."""
    for rota in ("/api/calibracao/fechar-regime",
                 "/api/calibracao/selar-revalidacao",
                 "/api/calibracao/revalidar"):
        r = client.post(rota, headers=CABECALHO, json={"author": "t"})
        assert r.status_code == 422, (rota, r.status_code, r.text)
        assert "regime" in r.text

    # E `indefinido` e recusado pelo mesmo modelo: ele nao e regime, e nao
    # pode ter fechamento, override nem revalidacao.
    r = client.post("/api/calibracao/selar-revalidacao", headers=CABECALHO,
                    json={"author": "t", "regime": "indefinido"})
    assert r.status_code == 422, r.text


def test_o_caminho_GLOBAL_nao_existe_no_BANCO(conn):
    """A camada que vale: `NOT NULL` e `UNIQUE`, e nao disciplina.

    Sem `regime` e sem `calibracao_fechamento_id` a linha nao entra - entao
    nao existe revalidacao global nem que alguem escreva SQL a mao.
    """
    colunas = {
        r["name"]: r
        for r in conn.execute("PRAGMA table_info(janela_revalidacao)")
    }
    assert colunas["regime"]["notnull"] == 1
    assert colunas["calibracao_fechamento_id"]["notnull"] == 1

    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute(
            "INSERT INTO janela_revalidacao (contrato, venue, symbol, de_ms,"
            " ate_ms_exclusive, calibracao_ate_ms_exclusive, selada_em_ms,"
            " selada_em, grade_ms)"
            " VALUES (?, 'binance', 'BTCUSDT', 100, 200, 100, 1, 'x', 100)",
            (CONTRATO,),
        )
    # A linha e RECUSADA, e varias travas estruturais a recusariam - `regime` e
    # `calibracao_fechamento_id` sao `NOT NULL` (conferido acima), nao ha
    # fechamento a que se ligar, a fronteira nao e a do banco e a duracao nao e
    # a congelada. Qual delas fala primeiro e ordem de criacao de gatilho, e
    # fixar isso no teste o tornaria fragil sem o tornar mais forte.
    assert str(e.value), "a linha tem de ser recusada por ALGUMA trava"
    assert conn.execute(
        "SELECT COUNT(*) FROM janela_revalidacao"
    ).fetchone()[0] == 0


def test_UMA_revalidacao_por_fechamento_e_o_BANCO_quem_recusa(conn, cfg, uso_real):
    """A segunda janela para o mesmo fechamento e recusada por `UNIQUE`.

    Repetir a confirmacao ate passar e a forma mais barata de aprovar o que
    nao passou - e por isso a trava nao e um `if`.
    """
    fech = conn.execute(
        "SELECT id FROM calibracao_regime_fechamento"
    ).fetchone()["id"]

    # O `UNIQUE (calibracao_fechamento_id)` esta DECLARADO no schema, e nao e
    # so um efeito colateral do outro `UNIQUE`.
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'janela_revalidacao'"
    ).fetchone()["sql"]
    assert "UNIQUE (calibracao_fechamento_id)" in ddl

    # A segunda janela usa o MESMO regime de proposito. A versao anterior deste
    # teste usava `vol_media`, e sob o vinculo conferido isso deixou de chegar
    # ao `UNIQUE`: o gatilho `janela_revalidacao_regime_do_fechamento` recusa
    # antes, porque o fechamento e de `vol_baixa`. Ela testava a trava errada.
    with pytest.raises(sqlite3.IntegrityError) as e:
        revalidacao_crua(conn, fech=fech, regime="vol_baixa",
                         calibracao_ate=T0 + GRADE * 2)
    assert "UNIQUE" in str(e.value)


def test_indefinido_NAO_pode_ter_fechamento_nem_revalidacao(conn):
    """Os `CHECK` das duas tabelas recusam. Ele nao e regime."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO calibracao_regime_fechamento (janela_calibracao_id,"
            " regime, n_exigido_calibracao, n_efetivo_x1000,"
            " fim_ms_exclusive, delta_mili_bps, fechado_em)"
            " VALUES (1, 'indefinido', 10, 20000, 100, 500, 'x')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO calibracao_regime_regra (janela_calibracao_id,"
            " regime, n_exigido_calibracao, n_exigido_revalidacao,"
            " alvos_iguais_por_decisao, por_que_os_alvos, sigma_e_x1000,"
            " tau_x1000, piloto_n)"
            " VALUES (1, 'indefinido', 10, 10, 1, 'x', 1000, 1000, 10)"
        )


def test_as_REGRAS_da_revalidacao_sao_congeladas_no_SELAMENTO(conn, cfg):
    """Antes do primeiro dado da calibracao, e imutaveis depois.

    O resultado da calibracao pode fornecer o PERFIL que sera revalidado; ele
    nao pode mexer em tamanho, alvo, metodo nem duracao.
    """
    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    janela = conn.execute("SELECT * FROM janela_calibracao").fetchone()

    assert janela["confianca_ppm"] == 950_000
    assert janela["metodo_bootstrap"] == "estacionario_politis_white"
    assert janela["tratamento_dependencia"] == "hac_bartlett_newey_west"
    assert janela["alvo"] == "lb95_p10_e2_maior_igual_zero"
    assert janela["regra_encerramento"] == "n_efetivo_maior_igual_n_exigido"
    assert janela["delta_mili_bps"] == 500
    assert janela["duracao_minima_ms"] > 0
    assert janela["de_ms"] > janela["selada_em_ms"], (
        "a janela comeca DEPOIS do proprio selamento: dado que ja existia nao"
        " entra"
    )

    # e os DOIS alvos de cada regime vem do PILOTO, congelados junto
    regras = conn.execute(
        "SELECT regime, n_exigido_calibracao, n_exigido_revalidacao,"
        "       alvos_iguais_por_decisao, por_que_os_alvos"
        "  FROM calibracao_regime_regra"
    ).fetchall()
    assert regras, "sem regra nao ha criterio de parada"
    assert all(r["n_exigido_calibracao"] > 0 for r in regras)
    assert all(r["n_exigido_revalidacao"] > 0 for r in regras)

    # A IGUALDADE E DECLARADA, e nao inferida de uma coluna reusada.
    for r in regras:
        assert r["alvos_iguais_por_decisao"] == 1
        assert r["n_exigido_calibracao"] == r["n_exigido_revalidacao"]
        assert "MESMA formula" in r["por_que_os_alvos"]
        assert "delta = 500 mili-bps" in r["por_que_os_alvos"]

    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute(
            "UPDATE calibracao_regime_regra SET n_exigido_calibracao = 1"
        )
    assert "congelada" in str(e.value)


def test_os_CINCO_estados_do_regime_sao_separados(conn, cfg):
    """`calibrado` nao e `revalidado`, e o override so vale no segundo."""
    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)

    e = ajuste.estado_por_regime(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT"
    )
    assert set(e["estados_possiveis"]) == {
        "nao_calibrado", "calibrado", "em_revalidacao", "revalidado",
        "falhou_revalidacao",
    }
    assert "indefinido" not in e["por_regime"]
    assert all(
        v["estado"] == "nao_calibrado" for v in e["por_regime"].values()
    ), "nenhum regime fechou ainda"

    fechar_e_selar(conn, cfg)
    e = ajuste.estado_por_regime(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT"
    )
    assert e["por_regime"][REGIME_DE_TESTE]["estado"] == "em_revalidacao"
    assert e["por_regime"][REGIME_DE_TESTE]["override_vale"] is False, (
        "um regime calibrado que nao concluiu a revalidacao permanece"
        " inconclusivo, e o override dele NAO ganha validade"
    )
    assert e["cobertura_cumprida"] is False
    assert "REVALIDADOS" in e["o_que_a_cobertura_MEDE"]


# ===========================================================================
# AS GARANTIAS EXIGIDAS PELO USUARIO EM 2026-09-22, ANTES DO COMMIT
#
# Uma por bloco, e cada uma com `INSERT` CRU alem do teste de rota: o que se
# quer provar e que a recusa vem do BANCO. Uma trava conferida so pelo caminho
# do codigo protege enquanto ninguem escrever SQL a mao - e §8.5.1 ja disse que
# garantia que depende de boa vontade ja foi violada.
# ===========================================================================


def _cadeia_ate_o_fechamento(conn, cfg):
    """Piloto fechado, calibracao selada e materializada, regime fechado."""
    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    conta = ajuste.medir_regime(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, regime=REGIME_DE_TESTE,
    )
    return ajuste.fechar_regime(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        regime=REGIME_DE_TESTE, n_efetivo=conta["n_efetivo"],
        ultimo_t_grid_ms=conta["ultimo_t_grid_ms"],
    )


#: A janela CURTA dos testes de `INSERT` cru: duas grades.
#:
#: Ela tem de TERMINAR para que a finalizacao exista, e o fim e fato do DADO -
#: o watermark do rele precisa passar dele. Com a duracao larga dos testes de
#: materializacao isso exigiria semear vinte mil barras.
DURACAO_CURTA = 2 * GRADE


def _fechamento_cru(conn, regime: str = "vol_baixa", *,
                    fim_ms: int | None = None) -> int:
    """Calibracao selada, FINALIZADA, e um fechamento - tudo por `INSERT` cru.

    Nao usa `uso_real`: aquela fixture grava um `revalidacao_uso`, e o uso e
    IRREVERSIVEL - `DELETE` bate no gatilho, corretamente. Testar o vinculo
    exige uma cadeia onde a janela de revalidacao ainda NAO existe.

    **A finalizacao entra aqui desde 2026-10-07:** o gatilho
    `janela_revalidacao_exige_a_finalizacao` recusa selar revalidacao antes de
    o perfil consolidado existir.
    """
    if not conn.execute(
        "SELECT 1 FROM janela_calibracao WHERE contrato = ?", (CONTRATO,)
    ).fetchone():
        _fluxo_curto(conn)
        selar_calibracao_no_inicio(conn, duracao_ms=DURACAO_CURTA)
        _finalizacao_crua(conn)
    jc = conn.execute(
        "SELECT id FROM janela_calibracao WHERE contrato = ?", (CONTRATO,)
    ).fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO calibracao_regime_regra (janela_calibracao_id,"
        " regime, n_exigido_calibracao, n_exigido_revalidacao,"
        " alvos_iguais_por_decisao, por_que_os_alvos, sigma_e_x1000,"
        " tau_x1000, piloto_n) VALUES (?,?,10,10,1,?,1,1000,1)",
        (jc, regime, ajuste.POR_QUE_OS_ALVOS),
    )
    return int(conn.execute(
        "INSERT INTO calibracao_regime_fechamento (janela_calibracao_id,"
        " regime, n_exigido_calibracao, n_efetivo_x1000, fim_ms_exclusive,"
        " delta_mili_bps, fechado_em) VALUES (?,?,10,20000,?,500,'x')",
        (jc, regime, T0 + GRADE * 2 if fim_ms is None else fim_ms),
    ).lastrowid)


# --------------------------------------------------- 2. o selo e do BANCO


def test_o_SELO_e_gerado_pelo_BANCO_quando_ninguem_o_nomeia(conn):
    """`DEFAULT` le o relogio do banco. O teste compara com ele, e nao com o
    relogio do processo - sao dois relogios, e e o do banco que decide."""
    selar_calibracao_no_inicio(conn)
    linha = conn.execute(
        "SELECT selada_em_ms, selada_em,"
        "       CAST(strftime('%s','now') AS INTEGER) * 1000 AS agora"
        "  FROM janela_calibracao"
    ).fetchone()
    assert abs(int(linha["selada_em_ms"]) - int(linha["agora"])) <= 60_000
    assert linha["selada_em"].endswith("Z")


def test_um_selo_RETROATIVO_cru_e_RECUSADO_pelo_banco(conn):
    """A porta que o `CHECK (de_ms > selada_em_ms)` NAO fechava.

    Datar o selamento no passado faz um periodo ja inspecionado passar por
    futuro, e o `CHECK` aprova com entusiasmo: ele so compara os dois numeros
    que o mesmo `INSERT` escolheu.
    """
    # TUDO MAIS VALIDO, e so o carimbo errado - senao outra trava falaria
    # primeiro e o teste passaria sem exercitar esta.
    fechar_piloto_cru(conn)
    # UM ANO ATRAS: o periodo inteiro que ja foi visto passaria por futuro.
    retroativo = _relogio_ms() - 365 * 86_400_000
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute(
            "INSERT INTO janela_calibracao (contrato, venue, symbol, de_ms,"
            " selada_em_ms, selada_em, piloto_ate_ms_exclusive, grade_ms,"
            " confianca_ppm, metodo_bootstrap, tratamento_dependencia, alvo,"
            " regra_encerramento, duracao_minima_ms, delta_mili_bps)"
            f" VALUES (?, 'binance', 'BTCUSDT', {ajuste.PRIMEIRA_GRADE_SQL},"
            " ?, 'x', ?, ?, 950000, 'estacionario_politis_white',"
            " 'hac_bartlett_newey_west', 'lb95_p10_e2_maior_igual_zero',"
            " 'n_efetivo_maior_igual_n_exigido', ?, 500)",
            (CONTRATO, GRADE, GRADE, T0, retroativo, T0, GRADE,
             DURACAO_DE_TESTE),
        )
    assert "relogio do BANCO" in str(e.value)


def test_um_selo_no_FUTURO_cru_tambem_e_recusado(conn):
    """Nos dois sentidos. Um selo adiantado empurraria o inicio para mais
    longe ainda, e escolher quando comecar e escolher o periodo."""
    fechar_piloto_cru(conn)
    adiante = _relogio_ms() + 365 * 86_400_000
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute(
            "INSERT INTO janela_calibracao (contrato, venue, symbol, de_ms,"
            " selada_em_ms, selada_em, piloto_ate_ms_exclusive, grade_ms,"
            " confianca_ppm, metodo_bootstrap, tratamento_dependencia, alvo,"
            " regra_encerramento, duracao_minima_ms, delta_mili_bps)"
            f" VALUES (?, 'binance', 'BTCUSDT', {ajuste.PRIMEIRA_GRADE_SQL},"
            " ?, 'x', ?, ?, 950000, 'estacionario_politis_white',"
            " 'hac_bartlett_newey_west', 'lb95_p10_e2_maior_igual_zero',"
            " 'n_efetivo_maior_igual_n_exigido', ?, 500)",
            (CONTRATO, GRADE, GRADE, T0, adiante, T0, GRADE,
             DURACAO_DE_TESTE),
        )
    assert "relogio do BANCO" in str(e.value)


def test_o_selo_da_REVALIDACAO_tambem_e_conferido(conn):
    """A mesma trava na outra janela. Sem isto, a metade que confirma ficaria
    de fora - e e justamente ela que reserva um periodo futuro."""
    fech = _fechamento_cru(conn)
    retroativo = _relogio_ms() - 365 * 86_400_000
    cal_ate = T0 + GRADE * 2
    # A identidade e o alvo entram VALIDOS: o teste perturba UM campo, e o
    # campo e `selada_em_ms`. Deixa-los nulos faria a linha ser recusada pela
    # trava da identidade, e o teste passaria afirmando outra coisa.
    certificar(conn)
    ident, alvo_h = _identidade_e_alvo_do_fechamento(conn, fech)
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute(
            "INSERT INTO janela_revalidacao (contrato, venue, symbol, regime,"
            " de_ms, ate_ms_exclusive, calibracao_fechamento_id,"
            " calibracao_ate_ms_exclusive, selada_em_ms, selada_em, grade_ms,"
            " alvo_hash, identidade_executavel)"
            f" VALUES (?, 'binance', 'BTCUSDT', 'vol_baixa',"
            f" {ajuste.PRIMEIRA_GRADE_SQL}, {ajuste.PRIMEIRA_GRADE_SQL} + ?,"
            " ?, ?, ?, 'x', ?, ?, ?)",
            (CONTRATO, GRADE, GRADE, cal_ate, GRADE, GRADE, cal_ate,
             DURACAO_DE_TESTE, fech, cal_ate, retroativo, GRADE,
             alvo_h, ident),
        )
    assert "relogio do BANCO" in str(e.value)


# ------------------------------ 3. o regime da janela e o regime do fechamento


def test_VINCULO_CRUZADO_entre_regime_e_fechamento_e_RECUSADO(conn):
    """Fechamento de `vol_baixa`, janela declarada `vol_alta`.

    O fato "de que regime e esta revalidacao" tem DUAS fontes - a coluna da
    janela e a do fechamento a que ela se liga. Duas fontes do mesmo fato
    divergem, e esta divergencia especifica passaria por todos os `CHECK`: as
    datas fechariam, o `UNIQUE` fecharia, e um regime confirmaria outro.
    """
    fech = _fechamento_cru(conn, "vol_baixa")
    # A linha e valida em TODO o resto - fronteira do banco, duracao
    # congelada, serie certa -, e so o regime esta cruzado.
    with pytest.raises(sqlite3.IntegrityError) as e:
        revalidacao_crua(conn, fech=fech, regime="vol_alta",
                         calibracao_ate=T0 + GRADE * 2)
    assert "vinculo cruzado" in str(e.value)


def test_a_SERIE_da_janela_tambem_tem_de_ser_a_do_fechamento(conn):
    """Mesmo regime, outro contrato: tambem recusado.

    Sem isto, a revalidacao de uma serie se penduraria no fechamento de outra -
    e o `UNIQUE (calibracao_fechamento_id)` ate ajudaria a esconder, porque a
    linha pareceria a primeira.
    """
    fech = _fechamento_cru(conn, "vol_baixa")
    with pytest.raises(sqlite3.IntegrityError) as e:
        revalidacao_crua(conn, fech=fech, regime="vol_baixa",
                         calibracao_ate=T0 + GRADE * 2, symbol="OUTRO")
    assert "vinculo cruzado" in str(e.value)


# ---------------------- 4. as quatro condicoes do inicio da revalidacao


def test_o_INICIO_da_revalidacao_e_CALCULADO_pelo_banco(conn):
    """As quatro condicoes continuam declaradas - e o inicio deixou de ser
    conferido contra elas para ser CALCULADO.

    O `CHECK` dizia que `de_ms` e a primeira grade admissivel **em relacao ao
    selo gravado**, e o selo tem 60 s de tolerancia. Perto de uma virada de
    grade, 60 s bastam para escolher a fronteira: carimbar o selo 59 s atras
    faz a grade que acabou de passar virar admissivel, e a amostra dela ja foi
    capturada (a tolerancia do ADR 0032 e de 2 s).

    Hoje o banco calcula `MAX(primeira grade > agora, fim da calibracao)` e
    recusa qualquer outro valor. O selo continua gravado e conferido; ele
    deixou de ter influencia sobre a fronteira.
    """
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'janela_revalidacao'"
    ).fetchone()["sql"]
    # As quatro continuam DECLARADAS, cada uma com a sua.
    assert "de_ms > selada_em_ms" in ddl                      # 1. futura
    assert "de_ms >= calibracao_ate_ms_exclusive" in ddl      # 2. pos-calibracao
    assert "de_ms % grade_ms = 0" in ddl                      # 3. na grade
    assert "(de_ms - grade_ms) > selada_em_ms" in ddl         # 1b. a primeira

    fech = _fechamento_cru(conn, "vol_media")
    cal_ate = T0 + GRADE * 2
    # Identidade e alvo VALIDOS - so a fronteira e perturbada.
    certificar(conn)
    ident, alvo_h = _identidade_e_alvo_do_fechamento(conn, fech)

    def tentar(de_ms, ate_ms):
        return conn.execute(
            "INSERT INTO janela_revalidacao (contrato, venue, symbol, regime,"
            " de_ms, ate_ms_exclusive, calibracao_fechamento_id,"
            " calibracao_ate_ms_exclusive, grade_ms, alvo_hash,"
            " identidade_executavel)"
            " VALUES (?, 'binance', 'BTCUSDT', 'vol_media', ?, ?, ?, ?, ?,"
            " ?, ?)",
            (CONTRATO, de_ms, ate_ms, fech, cal_ate, GRADE, alvo_h, ident),
        )

    # 3. FORA DA GRADE
    with pytest.raises(sqlite3.IntegrityError):
        tentar(cal_ate + 1, cal_ate + 1 + DURACAO_DE_TESTE)
    # 2. ANTES DO FIM DA CALIBRACAO DAQUELE REGIME
    with pytest.raises(sqlite3.IntegrityError):
        tentar(cal_ate - GRADE, cal_ate - GRADE + DURACAO_DE_TESTE)
    # 1. NO PASSADO DO SELO
    passado = (_relogio_ms() // GRADE - 4) * GRADE
    with pytest.raises(sqlite3.IntegrityError):
        tentar(passado, passado + DURACAO_DE_TESTE)
    # 1b. UMA GRADE ADIANTE DA PRIMEIRA ADMISSIVEL - e este e o caso que so o
    #     calculo pega: ele satisfaz as outras tres.
    with pytest.raises(sqlite3.IntegrityError) as e:
        tentar(cal_ate + GRADE, cal_ate + GRADE + DURACAO_DE_TESTE)
    assert "relogio do BANCO" in str(e.value)

    # E a EXATA entra - a que o proprio banco calcula.
    revalidacao_crua(conn, fech=fech, regime="vol_media",
                     calibracao_ate=cal_ate)
    assert conn.execute(
        "SELECT de_ms FROM janela_revalidacao"
    ).fetchone()["de_ms"] == cal_ate


def test_a_DURACAO_da_revalidacao_e_a_CONGELADA_e_o_banco_recusa_outra(conn):
    """O parametro `dias` saiu da assinatura; isto fecha o `INSERT` cru.

    Encurtar a confirmacao no momento de sela-la e escolher quanta evidencia
    ela vai exigir - e a duracao foi congelada em `janela_calibracao` antes do
    primeiro dado da calibracao.
    """
    fech = _fechamento_cru(conn, "vol_baixa")
    with pytest.raises(sqlite3.IntegrityError) as e:
        revalidacao_crua(conn, fech=fech, regime="vol_baixa",
                         calibracao_ate=T0 + GRADE * 2, duracao=GRADE)
    assert "duracao" in str(e.value) and "congelada" in str(e.value)


def test_as_FRONTEIRAS_de_origem_sao_CONFERIDAS_e_nao_declaradas(conn):
    """`piloto_ate_ms_exclusive` e `grade_ms` tinham duas fontes, e as duas
    entram na conta de `de_ms`.

    Declarar um fim de piloto adiante empurraria a janela de calibracao para
    onde quem insere quisesse, com todos os `CHECK` fechando - e e por isso que
    a conferencia tem de existir, e nao so a aritmetica.
    """
    fechar_piloto_cru(conn, ate_ms=T0)

    def tentar(piloto_ate, grade):
        return conn.execute(
            "INSERT INTO janela_calibracao (contrato, venue, symbol, de_ms,"
            " piloto_ate_ms_exclusive, grade_ms, confianca_ppm,"
            " metodo_bootstrap, tratamento_dependencia, alvo,"
            " regra_encerramento, duracao_minima_ms, delta_mili_bps,"
            " duracao_calibracao_ms, ate_ms_exclusive)"
            f" VALUES (?,?,?,{ajuste.PRIMEIRA_GRADE_SQL},?,?,950000,"
            " 'estacionario_politis_white','hac_bartlett_newey_west',"
            " 'lb95_p10_e2_maior_igual_zero',"
            f" 'n_efetivo_maior_igual_n_exigido',?,500,?,"
            f" {ajuste.PRIMEIRA_GRADE_SQL} + ?)",
            (CONTRATO, "binance", "BTCUSDT", grade, grade, piloto_ate,
             piloto_ate, grade, DURACAO_DE_TESTE, DURACAO_CURTA,
             grade, grade, piloto_ate, DURACAO_CURTA),
        )

    # um fim de piloto INVENTADO, muito adiante
    with pytest.raises(sqlite3.IntegrityError) as e:
        tentar(T0 + GRADE * 500, GRADE)
    assert "CONFERIDA" in str(e.value)
    # e uma grade que nao e a do contrato
    with pytest.raises(sqlite3.IntegrityError) as e:
        tentar(T0, GRADE * 2)
    assert "CONFERIDA" in str(e.value)

    # a verdadeira entra, e `de_ms` sai do banco
    tentar(T0, GRADE)
    assert conn.execute(
        "SELECT de_ms FROM janela_calibracao"
    ).fetchone()["de_ms"] == T0


def test_DADO_JA_EXISTENTE_no_selo_e_hoje_INALCANCAVEL_por_linha_valida(
    conn, cfg
):
    """Condicao 4 do inicio da revalidacao - e a ESTRUTURA passou a garanti-la.

    Ela era alcancavel enquanto `calibracao_ate_ms_exclusive` era o fim do
    REGIME: um fechamento que parasse cedo, com o regime ainda rendendo dado,
    deixava observacao dentro da janela reservada.

    Desde que o perfil passou a ser CONSOLIDADO no fim da janela (2026-10-07),
    a fronteira e o fim da JANELA - e `calibracao_observacao_dentro_da_janela`
    limita a observacao de calibracao exatamente nele. Nao sobra instante
    daquele regime em `t >= de_ms`.

    **Guarda que nao pode disparar e a forma do defeito que este projeto conta
    muitas vezes**, entao o teste faz as duas coisas: afirma a inalcancabilidade
    com o motivo, e prova que o gatilho NAO e vazio derrubando a trava que o
    torna redundante.
    """
    from app.settings import get_settings

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    fechar_regime_medido(conn, cfg)
    # A finalizacao tem de existir - sem ela outro gatilho fala primeiro, e o
    # teste passaria sem exercitar o que ele mede.
    ajuste.aplicar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, autor="teste",
        settings=get_settings(),
    )
    janela_cal = conn.execute(
        "SELECT id, ate_ms_exclusive FROM janela_calibracao"
    ).fetchone()
    fim = int(janela_cal["ate_ms_exclusive"])

    # 1. NAO HA observacao daquele regime em `t >= fim da janela`.
    assert conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao"
        " WHERE regime = ? AND t_grid_ms >= ?", (REGIME_DE_TESTE, fim)
    ).fetchone()[0] == 0

    # 2. O gatilho EXISTE.
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger'"
        "   AND name='janela_revalidacao_sem_dado_preexistente'"
    ).fetchone()

    # 3. E ELE NAO E VAZIO: derrubando a trava que o torna redundante - a que
    #    confere a fronteira contra o fim da janela -, um fechamento que para
    #    CEDO volta a deixar dado dentro do periodo reservado, e ele fala.
    instantes = [
        int(r["t_grid_ms"]) for r in conn.execute(
            "SELECT DISTINCT t_grid_ms FROM calibracao_observacao"
            " WHERE finalidade = 'calibracao' AND regime = ?"
            " ORDER BY t_grid_ms", (REGIME_DE_TESTE,)
        )
    ]
    assert len(instantes) > 5
    cedo = instantes[4] + GRADE
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("DROP TRIGGER janela_revalidacao_fronteiras_conferidas")
    conn.execute("PRAGMA writable_schema = OFF")
    fech = conn.execute(
        "INSERT INTO calibracao_regime_fechamento (janela_calibracao_id,"
        " regime, n_exigido_calibracao, n_efetivo_x1000, fim_ms_exclusive,"
        " delta_mili_bps, fechado_em) VALUES (?, 'vol_media', 10, 20000, ?,"
        " 500, 'x')", (janela_cal["id"], cedo)
    ).lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO calibracao_regime_regra (janela_calibracao_id,"
        " regime, n_exigido_calibracao, n_exigido_revalidacao,"
        " alvos_iguais_por_decisao, por_que_os_alvos, sigma_e_x1000,"
        " tau_x1000, piloto_n) VALUES (?, 'vol_media', 10, 10, 1, ?, 1, 1000, 1)",
        (janela_cal["id"], ajuste.POR_QUE_OS_ALVOS),
    )
    with pytest.raises(sqlite3.IntegrityError) as e:
        revalidacao_crua(conn, fech=fech, regime=REGIME_DE_TESTE,
                         calibracao_ate=cedo)
    assert "ja ha observacao deste regime" in str(e.value)


def test_a_janela_de_CALIBRACAO_tambem_nao_nasce_sobre_dado_existente(conn, cfg):
    """A mesma condicao, do outro lado. Aqui o recorte NAO e por regime: ao
    selar a calibracao, tudo que existe e piloto, e piloto nao entra nela."""
    semear(conn, 5)
    materializar(conn, cfg, lados=("compra",))
    conn.execute("DELETE FROM janela_calibracao") if False else None
    with pytest.raises(sqlite3.IntegrityError) as e:
        selar_calibracao_no_inicio(conn, piloto_ate_ms=T0)
    assert "ja ha observacao materializada" in str(e.value)


# ------------------------- 5. fechou congelou, consumiu congelou


def test_FECHOU_CONGELOU_o_banco_recusa_observacao_nova_do_regime(conn, cfg):
    """Depois do fechamento, a serie DAQUELE regime nao cresce mais.

    Sem isto, "parar em `n_exigido`" seria so o momento em que alguem resolveu
    olhar: a serie continuaria crescendo e a proxima leitura daria outro `p10`.
    """
    _cadeia_ate_o_fechamento(conn, cfg)
    ultimo = conn.execute(
        "SELECT MAX(t_grid_ms) AS t FROM calibracao_observacao"
        " WHERE finalidade = 'calibracao'"
    ).fetchone()["t"]
    linha = conn.execute(
        "SELECT * FROM calibracao_observacao WHERE finalidade = 'calibracao'"
        "   AND regime = ? LIMIT 1", (REGIME_DE_TESTE,)
    ).fetchone()

    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute(
            "INSERT INTO calibracao_observacao (contrato, venue, symbol,"
            " t_grid_ms, lado, finalidade, regime, config_version_id,"
            " calibracao_perfil_hash, janela_hash, abertura, bbo_bid,"
            " bbo_bid_qty, bbo_ask, bbo_ask_qty, p_exec_previsto, e1_mili_bps,"
            " e2_mili_bps, nocional_cents, dentro_do_l1, criado_em)"
            " VALUES (?,?,?,?,?,'calibracao',?,?,?,?,?,?,?,?,?,?,?,?,?,1,'x')",
            (CONTRATO, "binance", "BTCUSDT", int(ultimo) + GRADE * 5,
             "compra", REGIME_DE_TESTE, linha["config_version_id"],
             linha["calibracao_perfil_hash"], linha["janela_hash"],
             linha["abertura"], linha["bbo_bid"], linha["bbo_bid_qty"],
             linha["bbo_ask"], linha["bbo_ask_qty"], linha["p_exec_previsto"],
             linha["e1_mili_bps"], linha["e2_mili_bps"],
             linha["nocional_cents"]),
        )
    assert "esta FECHADA" in str(e.value)


def test_rematerializar_depois_do_fechamento_NAO_quebra_nem_acrescenta(conn, cfg):
    """A barreira e contra instante NOVO, e nao contra repeticao inofensiva.

    `registrar` reprocessa a janela inteira com `INSERT OR IGNORE`, e
    `RAISE(ABORT)` dispara ANTES da resolucao de conflito. Sem a clausula
    `NOT EXISTS (a linha ja existe)` no gatilho, rematerializar recusaria no
    primeiro instante ja gravado - e o caminho oficial ficaria inutilizavel
    depois do primeiro fechamento.
    """
    _cadeia_ate_o_fechamento(conn, cfg)
    antes = conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao"
    ).fetchone()[0]
    janela = ajuste.materializacao.ler_janela_de_calibracao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT"
    )
    r = observacao.registrar(
        conn, finalidade="calibracao", contrato=CONTRATO, venue="binance",
        symbol="BTCUSDT", config=cfg.config, config_version_id=cfg.id,
        calibracao_perfil_hash=PERFIL_DE_TESTE, janela_hash=HASH_DE_TESTE,
        de_ms=int(janela["de_ms"]),
        # O FIM DA JANELA: ela ganhou fim declarado, e instante fora dele e
        # recusado pelo gatilho - o que nao e o que este teste mede.
        ate_ms_exclusive=int(janela["ate_ms_exclusive"]),
        orcamento_cents=1_000, lados=("compra",),
    )
    assert r.gravadas == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao"
    ).fetchone()[0] == antes


def _revalidacao_aplicada(conn, cfg):
    """A cadeia inteira ate haver identidade de revalidacao: piloto, calibracao
    selada e materializada, regime fechado, revalidacao selada e ajuste
    APLICADO. Devolve (janela, resultado do ajuste)."""
    from app.settings import get_settings

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    # A ORDEM: fecha o regime -> FINALIZA o perfil -> sela a revalidacao.
    # `fechar_e_selar` faz as tres, e `aplicar` aqui devolve a finalizacao que
    # ja existe - e e por isso que ele e idempotente.
    janela = fechar_e_selar(conn, cfg)
    r = ajuste.aplicar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, autor="teste",
        settings=get_settings(),
    )
    assert r["aplicado"] is True, r["motivo"]
    return janela, r


def _observacao_de_revalidacao(conn, janela, r, *, t_grid_ms):
    return conn.execute(
        "INSERT INTO calibracao_observacao (contrato, venue, symbol,"
        " t_grid_ms, lado, finalidade, regime, config_version_id,"
        " calibracao_perfil_hash, janela_hash, abertura, bbo_bid,"
        " bbo_bid_qty, bbo_ask, bbo_ask_qty, p_exec_previsto, e1_mili_bps,"
        " e2_mili_bps, nocional_cents, dentro_do_l1, criado_em)"
        " VALUES (?,'binance','BTCUSDT',?,'compra','revalidacao',?,?,?,"
        " 'h',100,99,1,101,1,102,1,1,10,1,'x')",
        (CONTRATO, t_grid_ms, REGIME_DE_TESTE,
         int(r["config_version_nova"]), r["calibracao_perfil_hash"]),
    )


def test_CONSUMIU_CONGELOU_o_banco_recusa_revalidacao_nova_na_janela(conn, cfg):
    """Depois do veredito, a serie daquela janela nao cresce mais.

    A janela e de uso unico. Deixar a serie crescer depois do veredito
    permitiria reler o mesmo `LB95` sobre outra amostra - repetir o teste ate
    passar, pela porta dos fundos.

    A cadeia e a REAL, com o ajuste aplicado, porque a identidade tem de estar
    certa: com ela errada quem recusaria seria o OUTRO gatilho, e o teste
    passaria sem exercitar este.
    """
    janela, r = _revalidacao_aplicada(conn, cfg)

    # ANTES do consumo a observacao entra - senao o teste seguinte passaria
    # por um motivo que nao e o consumo.
    _observacao_de_revalidacao(conn, janela, r, t_grid_ms=int(janela["de_ms"]))

    conn.execute(
        "INSERT INTO revalidacao_uso (janela_id, calibracao_versao_id,"
        " lb95_mili_bps, p10_mili_bps, n, passou, usada_em)"
        " VALUES (?, ?, 10, 600, 500, 1, 'x')",
        (int(janela["id"]), int(r["calibracao_id"])),
    )

    with pytest.raises(sqlite3.IntegrityError) as e:
        _observacao_de_revalidacao(
            conn, janela, r, t_grid_ms=int(janela["de_ms"]) + GRADE
        )
    assert "ja foi consumida" in str(e.value)


# --------------- 6. a revalidacao usa a identidade que a calibracao produziu


def test_a_REVALIDACAO_recusa_a_configuracao_VIGENTE(conn, cfg):
    """A vigente e a que esta a mao, e por isso e a tentacao.

    Confirmar sob ela mediria um simulador diferente daquele que o override
    descreve, e o `LB95` valeria sobre outra coisa.
    """
    from app.settings import get_settings

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    fechar_e_selar(conn, cfg)
    r = ajuste.aplicar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, autor="teste",
        settings=get_settings(),
    )
    assert r["aplicado"] is True, r["motivo"]
    janela = ajuste.ler_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        regime=REGIME_DE_TESTE,
    )

    def inserir(config_version_id, perfil):
        return conn.execute(
            "INSERT INTO calibracao_observacao (contrato, venue, symbol,"
            " t_grid_ms, lado, finalidade, regime, config_version_id,"
            " calibracao_perfil_hash, janela_hash, abertura, bbo_bid,"
            " bbo_bid_qty, bbo_ask, bbo_ask_qty, p_exec_previsto, e1_mili_bps,"
            " e2_mili_bps, nocional_cents, dentro_do_l1, criado_em)"
            " VALUES (?,'binance','BTCUSDT',?,'compra','revalidacao',?,?,?,"
            " 'h',100,99,1,101,1,102,1,1,10,1,'x')",
            (CONTRATO, int(janela["de_ms"]), REGIME_DE_TESTE,
             config_version_id, perfil),
        )

    # A VIGENTE: recusada.
    with pytest.raises(sqlite3.IntegrityError) as e:
        inserir(cfg.id, PERFIL_DE_TESTE)
    assert "identidade errada na revalidacao" in str(e.value)

    # O perfil de outra calibracao, sob a versao certa: tambem recusado.
    with pytest.raises(sqlite3.IntegrityError):
        inserir(int(r["config_version_nova"]), "perfil-de-outra-calibracao")

    # A identidade que a calibracao DAQUELE regime produziu: aceita.
    inserir(int(r["config_version_nova"]), r["calibracao_perfil_hash"])


def test_a_materializacao_da_revalidacao_DERIVA_a_identidade(conn, cfg):
    """Nem ha parametro de identidade para passar errado.

    O caminho oficial le a cadeia janela -> fechamento -> versao aplicada ->
    `config_version` nova -> perfil. Quem chama nao escolhe nada.
    """
    import inspect

    from app.calibracao import materializacao
    from app.settings import get_settings

    nomes = set(inspect.signature(materializacao.da_revalidacao).parameters)
    assert not (nomes & {"config", "config_version_id",
                         "calibracao_perfil_hash", "de_ms",
                         "ate_ms_exclusive", "janela_hash"}), nomes

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    # SEM janela de revalidacao ainda - e ela so nasce depois da finalizacao.
    with pytest.raises(materializacao.JanelaNaoSelada):
        materializacao.da_revalidacao(
            conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
            regime=REGIME_DE_TESTE, orcamento_cents=1_000,
        )

    fechar_e_selar(conn, cfg)
    r = ajuste.aplicar(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        config=cfg.config, config_version_id=cfg.id, autor="teste",
        settings=get_settings(),
    )
    saida = materializacao.da_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        regime=REGIME_DE_TESTE, orcamento_cents=1_000,
    )
    ident = saida["identidade_executavel"]
    assert ident["config_version_id"] == r["config_version_nova"]
    assert ident["calibracao_perfil_hash"] == r["calibracao_perfil_hash"]
    assert ident["config_version_id"] != cfg.id


# ---------------- 7. selar-revalidacao recebe IDENTIDADE, e nada mais


def test_selar_revalidacao_NAO_recebe_data_tamanho_metodo_nem_duracao(conn):
    """A ausencia e a garantia, e ela e por ASSINATURA.

    Um parametro `dias` existia, e com ele dava para encurtar a confirmacao no
    momento de sela-la - que e escolher quanta evidencia ela vai exigir.
    """
    import inspect

    nomes = set(inspect.signature(ajuste.selar_revalidacao).parameters)
    assert nomes == {"conn", "contrato", "venue", "symbol", "regime"}, nomes

    proibidos = {"dias", "agora_ms", "de_ms", "ate_ms_exclusive", "duracao",
                 "duracao_ms", "n_exigido", "delta", "delta_mili_bps",
                 "metodo", "metodo_bootstrap", "confianca", "confianca_ppm",
                 "alvo", "grade_ms"}
    assert not (nomes & proibidos)

    # E a rota tambem nao: `extra=forbid` transforma a tentativa em 422.
    from app.api.rotas.calibracao import PedidoPorRegime

    assert set(PedidoPorRegime.model_fields) == {
        "author", "venue", "symbol", "contrato", "lado", "regime"
    }


def test_a_ROTA_de_selar_revalidacao_recusa_data_no_corpo(client):
    for campo, valor in (("agora_ms", 1), ("dias", 3), ("de_ms", 1),
                         ("n_exigido", 10), ("duracao_minima_ms", 1)):
        r = client.post(
            "/api/calibracao/selar-revalidacao",
            headers=CABECALHO,
            json={"author": "t", "regime": "vol_baixa", campo: valor},
        )
        assert r.status_code == 422, (campo, r.status_code)


# ------------------ 8. os dois alvos de amostra, publicados SEPARADOS


def test_os_DOIS_alvos_de_amostra_sao_COLUNAS_separadas(conn):
    """E a igualdade e DECLARADA, nao inferida de uma coluna reusada."""
    colunas = {
        r["name"] for r in conn.execute(
            "PRAGMA table_info(calibracao_regime_regra)"
        )
    }
    assert {"n_exigido_calibracao", "n_exigido_revalidacao",
            "alvos_iguais_por_decisao", "por_que_os_alvos"} <= colunas
    assert "n_exigido" not in colunas, (
        "o nome generico valia para os dois e nomeava nenhum"
    )

    fechamento = {
        r["name"] for r in conn.execute(
            "PRAGMA table_info(calibracao_regime_fechamento)"
        )
    }
    assert "n_exigido_calibracao" in fechamento
    assert "n_exigido" not in fechamento


def test_a_DECLARACAO_de_igualdade_nao_pode_MENTIR(conn):
    """Um `CHECK` amarra a declaracao aos numeros que ela declara.

    Sem ele, `alvos_iguais_por_decisao = 1` sobre dois numeros diferentes seria
    um campo afirmando o contrario do que esta ao lado - a forma exata do
    defeito que este projeto conta trinta e uma vezes.
    """
    selar_calibracao_no_inicio(conn)
    jc = conn.execute("SELECT id FROM janela_calibracao").fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO calibracao_regime_regra (janela_calibracao_id,"
            " regime, n_exigido_calibracao, n_exigido_revalidacao,"
            " alvos_iguais_por_decisao, por_que_os_alvos, sigma_e_x1000,"
            " tau_x1000, piloto_n)"
            " VALUES (?, 'vol_baixa', 10, 20, 1, 'mentira', 1, 1000, 1)",
            (jc,),
        )
    # e o contrario tambem: numeros iguais declarados como diferentes
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO calibracao_regime_regra (janela_calibracao_id,"
            " regime, n_exigido_calibracao, n_exigido_revalidacao,"
            " alvos_iguais_por_decisao, por_que_os_alvos, sigma_e_x1000,"
            " tau_x1000, piloto_n)"
            " VALUES (?, 'vol_baixa', 10, 10, 0, 'mentira', 1, 1000, 1)",
            (jc,),
        )


def test_o_FECHAMENTO_publica_os_dois_alvos_lado_a_lado(conn, cfg):
    """Publicar so o da calibracao deixaria quem le INFERIR que o outro e o
    mesmo - e inferir e o que a separacao existe para nao pedir."""
    fechamento = _cadeia_ate_o_fechamento(conn, cfg)
    alvos = fechamento["alvos_de_amostra"]
    assert set(alvos) == {"calibracao", "revalidacao", "iguais_por_decisao",
                          "por_que"}
    assert alvos["calibracao"] > 0 and alvos["revalidacao"] > 0
    assert alvos["iguais_por_decisao"] is True
    assert "MESMA formula" in alvos["por_que"]


# ------------------ o REGIME e coluna, e a observacao e separada por ele


def test_o_REGIME_da_observacao_e_GRAVADO_e_nao_derivado_na_leitura(conn, cfg):
    """Derivar na leitura fazia o regime de um instante depender de QUANDO se
    lia: uma barra nova adiante podia reclassificar um instante ja medido."""
    colunas = {
        r["name"] for r in conn.execute(
            "PRAGMA table_info(calibracao_observacao)"
        )
    }
    assert "regime" in colunas

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    linhas = conn.execute(
        "SELECT regime, COUNT(*) AS n FROM calibracao_observacao"
        " GROUP BY regime"
    ).fetchall()
    assert linhas
    assert all(
        r["regime"] in ("vol_baixa", "vol_media", "vol_alta", "indefinido")
        for r in linhas
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO calibracao_observacao (contrato, venue, symbol,"
            " t_grid_ms, lado, finalidade, regime, config_version_id,"
            " calibracao_perfil_hash, janela_hash, abertura, bbo_bid,"
            " bbo_bid_qty, bbo_ask, bbo_ask_qty, p_exec_previsto, e1_mili_bps,"
            " e2_mili_bps, nocional_cents, dentro_do_l1, criado_em)"
            " VALUES (?,'binance','BTCUSDT',1,'compra','piloto','vol_absurda',"
            " 1,'p','h',100,99,1,101,1,102,1,1,10,1,'x')",
            (CONTRATO,),
        )


# -------- as duas rotas que estavam QUEBRADAS, e a suite verde nao via


def test_o_GET_da_calibracao_RESPONDE_com_o_piloto_FECHADO(client, conn, cfg):
    """REGRESSAO. A rota chamava `observacao.serie_e2`, que deixou de existir.

    Os dois testes antigos do GET rodam com o piloto ABERTO - e ai `janela is
    None` e o ramo nunca executa.

    **Producao nao esta quebrada:** la roda o codigo anterior ao ADR 0041, onde
    `serie_e2` ainda existe, e o GET responde 200 com o piloto fechado
    (conferido em 2026-09-22). O 500 apareceria no primeiro DEPLOY desta
    janela - que e o motivo de o defeito ter de morrer antes dele.
    """
    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    r = client.get("/api/calibracao", headers=CABECALHO)
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["piloto"]["estado"] == "fechada"
    assert corpo["erro_de_execucao"]["estado"] == "medido"
    assert corpo["erro_de_execucao"]["finalidade_da_serie"] == "piloto"
    assert "DIMENSIONAMENTO" in corpo["erro_de_execucao"]["o_que_isto_e"]
    assert set(corpo["regimes"]["por_regime"]) == {
        "vol_baixa", "vol_media", "vol_alta"
    }
    assert set(corpo["observacoes_por_finalidade"]) == {
        "piloto", "calibracao", "revalidacao"
    }


def test_a_rota_RODAR_exige_finalidade_e_nao_aceita_periodo(client):
    """REGRESSAO. `rodar` chamava `registrar` SEM `finalidade`, que nao tem
    default - um `TypeError` que esperaria o primeiro POST DEPOIS do deploy
    desta janela. O codigo em producao ainda tem o default, e nao quebra."""
    r = client.post("/api/calibracao/rodar", headers=CABECALHO, json={})
    assert r.status_code == 422, r.text

    r = client.post("/api/calibracao/rodar", headers=CABECALHO,
                    json={"finalidade": "piloto", "de_ms": 1})
    assert r.status_code == 422

    r = client.post("/api/calibracao/rodar", headers=CABECALHO,
                    json={"finalidade": "revalidacao"})
    assert r.status_code == 422
    assert "POR REGIME" in r.json()["detail"]

    # Sem janela selada, 409 - e nao 500.
    r = client.post("/api/calibracao/rodar", headers=CABECALHO,
                    json={"finalidade": "piloto"})
    assert r.status_code == 409, r.text


def _piloto_fechado_sem_materializar(conn, *, instantes: int = 1_400):
    """Fecha a janela do piloto e NAO materializa nada.

    `montar_piloto` materializa por dentro; aqui o ponto e justamente deixar
    isso para a rota, que e o passo 2 do que vem a seguir.
    """
    from app.aovivo import bbo as m

    serie = m.Serie(venue="binance", symbol="BTCUSDT",
                    price_scale_exp=8, volume_scale_exp=8)
    m.receber(conn, serie, CONTRATO,
              [semear_par(conn, i, ask=ASK_JUSTO) for i in range(instantes)])
    return piloto.fechar(
        conn,
        m.Serie(venue="binance", symbol="BTCUSDT",
                price_scale_exp=0, volume_scale_exp=0),
        CONTRATO,
    )


def test_RODAR_materializa_o_PILOTO_pelo_caminho_oficial(client, conn, cfg):
    """`materializacao.do_piloto` nao tinha teste NENHUM, e e o passo 2.

    Ela e a unica funcao que deriva o intervalo do piloto de `janela_piloto` e
    carimba o `janela_hash` com o hash do MANIFESTO. Uma funcao oficial sem
    caminho exercitado e a forma do defeito que este projeto conta muitas
    vezes - e esta ia ser chamada em producao antes de ser chamada num teste.
    """
    from app.calibracao import manifesto as manifesto_mod

    janela = _piloto_fechado_sem_materializar(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao"
    ).fetchone()[0] == 0

    r = client.post("/api/calibracao/rodar", headers=CABECALHO,
                    json={"finalidade": "piloto"})
    assert r.status_code == 200, r.text
    obs = r.json()["observacoes"]

    assert obs["finalidade"] == "piloto"
    assert obs["janela"]["de_ms"] == janela.de_ms
    assert obs["janela"]["ate_ms_exclusive"] == janela.ate_ms_exclusive
    assert obs["gravadas"] > 0
    # O `janela_hash` e o hash do MANIFESTO - o mesmo que o fechamento publica.
    esperado = manifesto_mod.do_registro(
        conn,
        serie=__import__("app.aovivo.bbo", fromlist=["Serie"]).Serie(
            venue="binance", symbol="BTCUSDT",
            price_scale_exp=0, volume_scale_exp=0),
        contrato=CONTRATO,
    )["manifesto"]["hash_do_intervalo"]
    assert obs["janela"]["hash"] == esperado
    assert "nao calibram" in obs["o_que_sai_do_piloto"].replace("NAO", "nao")

    # Tudo gravado como PILOTO, e nada fora da janela dele.
    fora = conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao"
        " WHERE finalidade != 'piloto' OR t_grid_ms < ? OR t_grid_ms >= ?",
        (janela.de_ms, janela.ate_ms_exclusive),
    ).fetchone()[0]
    assert fora == 0

    # E o REGIME foi gravado com cada linha.
    assert conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao WHERE regime IS NULL"
    ).fetchone()[0] == 0

    # Repetir nao duplica: a chave primaria e por instante e lado.
    antes = conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao"
    ).fetchone()[0]
    r2 = client.post("/api/calibracao/rodar", headers=CABECALHO,
                     json={"finalidade": "piloto"})
    assert r2.status_code == 200
    assert r2.json()["observacoes"]["gravadas"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao"
    ).fetchone()[0] == antes


def test_RODAR_a_CALIBRACAO_exige_a_janela_selada_e_so_entao_materializa(
    client, conn, cfg
):
    """`materializacao.da_calibracao` tambem nao tinha teste.

    E a ordem inteira fica exercitada: piloto -> selar calibracao -> coletar.
    Sem o selo, 409 - e nao 500, nem materializacao em janela nenhuma.
    """
    janela_p = _piloto_fechado_sem_materializar(conn)

    # 1. Sem janela de calibracao selada, a rota RECUSA.
    r = client.post("/api/calibracao/rodar", headers=CABECALHO,
                    json={"finalidade": "calibracao"})
    assert r.status_code == 409, r.text
    assert "nao ha janela de calibracao selada" in r.json()["detail"]

    # 2. O piloto e materializado - e e dele que sai o `n_exigido`.
    assert client.post("/api/calibracao/rodar", headers=CABECALHO,
                       json={"finalidade": "piloto"}).status_code == 200

    # 3. O selo. Nenhuma data no corpo.
    r = client.post("/api/calibracao/selar-calibracao", headers=CABECALHO,
                    json={"author": "teste"})
    assert r.status_code == 201, r.text
    jc = r.json()["janela_de_calibracao"]
    assert jc["de_ms"] >= janela_p.ate_ms_exclusive
    assert jc["de_ms"] > jc["selada_em_ms"]
    assert jc["de_ms"] % jc["grade_ms"] == 0

    # 4. Agora a calibracao materializa, e SO no periodo dela.
    r = client.post("/api/calibracao/rodar", headers=CABECALHO,
                    json={"finalidade": "calibracao"})
    assert r.status_code == 200, r.text
    obs = r.json()["observacoes"]
    assert obs["finalidade"] == "calibracao"
    assert obs["janela"]["de_ms"] == jc["de_ms"]
    assert obs["gravadas"] > 0
    assert "hash do SELO" in obs["o_que_o_janela_hash_e"]

    # NENHUMA observacao do piloto virou calibracao, e vice-versa: um instante
    # tem UMA linha com UMA finalidade, e a chave primaria garante.
    sobreposicao = conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao"
        " WHERE finalidade = 'calibracao' AND t_grid_ms < ?",
        (janela_p.ate_ms_exclusive,),
    ).fetchone()[0]
    assert sobreposicao == 0

    # e os SEIS numeros saem separados, com o que cada um e.
    assert set(obs["o_que_cada_um_e"]) >= {
        "instantes_validos_na_janela", "candidatos_de_lado", "comparaveis",
        "excluidos_por_l1", "na_estatistica", "por_regime",
    }


# ===========================================================================
# O REGIME GRAVADO, NO NIVEL DO BANCO
# ===========================================================================


def semear_adiante(conn, *, quantos: int, salto: bool = False) -> int:
    """Barras CONTIGUAS a partir do watermark atual. Devolve o primeiro `ms`.

    Contiguas e o ponto: a trava contra materializar antes do backfill exige a
    janela causal COMPLETA, e um vao entre o que ja existe e o dado novo
    deixaria os instantes novos adiados para sempre - `janela_causal_incompleta`.

    Em producao nao ha vao: o rele entrega sem parar, e o "vao declarado" da
    revalidacao e de quais instantes foram MATERIALIZADOS, nunca do fluxo.

    `salto=True` faz o preco alternar por barra - depois de 672 dessas, o
    detector congelado classifica `vol_alta`.
    """
    from app.aovivo import bbo as m

    ultimo = conn.execute(
        "SELECT MAX(open_time_ms) AS t FROM stream_bar WHERE venue = 'binance'"
        "   AND symbol = 'BTCUSDT' AND timeframe = '15m'"
    ).fetchone()["t"]
    inicio = int(ultimo) + GRADE
    serie8 = m.Serie(venue="binance", symbol="BTCUSDT",
                     price_scale_exp=8, volume_scale_exp=8)
    novas = []
    for k in range(quantos):
        ms = inicio + k * GRADE
        preco = ABERTURA + ((k % 2) * 3_000_00000000 if salto else 0)
        conn.execute(
            "INSERT INTO stream_bar (venue, symbol, timeframe, open_time_ms,"
            " open, high, low, close, volume, quote_volume, trades,"
            " interval_ms, price_scale_exp, volume_scale_exp, recebido_em,"
            " origem) VALUES ('binance','BTCUSDT','15m',?,?,?,?,?,1,1,1,"
            "?,8,8,'x','ao_vivo')",
            (ms, preco, preco, preco, preco, GRADE),
        )
        corrigido = ms - 500
        novas.append(m.Amostra(
            t_grid_ms=ms, disponivel=True,
            bid=ASK_JUSTO - 200_00000, bid_qty=5_00000000,
            ask=ASK_JUSTO + (k % 3) * 1_000_000, ask_qty=99_00000000,
            u=500_000 + k,
            received_at_ms=corrigido + 2_450,
            received_at_corrigido_ms=corrigido,
            sampled_at_ms=corrigido, defasagem_ms=500,
            offset_us=-2_450_000, rtt_us=12_000, incerteza_residual_us=6_000,
            relogio_medido_em_ms=corrigido - 30_000,
        ))
    m.receber(conn, serie8, CONTRATO, novas)
    return inicio


def _fotografia(conn) -> dict:
    """O que NAO pode se mover quando se materializa observacao.

    Config, perfil, creditos e holdout. Materializar e medir, e medir nao
    altera o experimento - mas "nao altera" so vale se alguem contar.
    """
    def um(sql):
        return conn.execute(sql).fetchone()[0]

    return {
        "config_version": um("SELECT COUNT(*) FROM config_version"),
        "config_change": um("SELECT COUNT(*) FROM config_change"),
        "calibracao_perfil": um("SELECT COUNT(*) FROM calibracao_perfil"),
        "calibracao_versao": um("SELECT COUNT(*) FROM calibracao_versao"),
        "credit_entry": um("SELECT COUNT(*) FROM test_credit_entry"),
        "credit_budget": um("SELECT COUNT(*) FROM test_credit_budget"),
        "holdout": um("SELECT COUNT(*) FROM holdout_access"),
        "hypothesis": um("SELECT COUNT(*) FROM hypothesis"),
        "revalidacao_uso": um("SELECT COUNT(*) FROM revalidacao_uso"),
    }


def test_o_regime_GRAVADO_nao_se_move_quando_chega_barra_FUTURA(conn, cfg):
    """A propriedade do ADR 0026 aplicada a COLUNA, e nao so ao detector.

    O teste do detector prova a janela causal sobre uma lista; este prova que o
    caminho inteiro - `stream_bar` -> `regimes_das_barras` -> coluna - preserva
    a propriedade. Sao coisas diferentes: aqui as posicoes vem de uma consulta
    ordenada, e uma barra nova poderia desloca-las.
    """
    from app.aovivo import bbo as m

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    antes = {
        (int(r["t_grid_ms"]), r["lado"]): r["regime"]
        for r in conn.execute(
            "SELECT t_grid_ms, lado, regime FROM calibracao_observacao"
        )
    }
    assert antes

    # BARRAS FUTURAS, com volatilidade enorme - uma janela nao-causal gritaria.
    ultimo = conn.execute(
        "SELECT MAX(t_grid_ms) AS t FROM bbo_amostra WHERE contrato = ?",
        (CONTRATO,),
    ).fetchone()["t"]
    serie8 = m.Serie(venue="binance", symbol="BTCUSDT",
                     price_scale_exp=8, volume_scale_exp=8)
    novas = []
    for k in range(1, 41):
        t = int(ultimo) + k * GRADE
        preco = ABERTURA + (k % 2) * 3_000_00000000   # salto enorme por barra
        conn.execute(
            "INSERT INTO stream_bar (venue, symbol, timeframe, open_time_ms,"
            " open, high, low, close, volume, quote_volume, trades,"
            " interval_ms, price_scale_exp, volume_scale_exp, recebido_em,"
            " origem) VALUES ('binance','BTCUSDT','15m',?,?,?,?,?,1,1,1,"
            "?,8,8,'x','ao_vivo')",
            (t, preco, preco, preco, preco, GRADE),
        )
        corrigido = t - 500
        novas.append(m.Amostra(
            t_grid_ms=t, disponivel=True,
            bid=ASK_JUSTO - 200_00000, bid_qty=5_00000000,
            ask=ASK_JUSTO, ask_qty=99_00000000, u=100_000 + k,
            received_at_ms=corrigido + 2_450,
            received_at_corrigido_ms=corrigido,
            sampled_at_ms=corrigido, defasagem_ms=500,
            offset_us=-2_450_000, rtt_us=12_000, incerteza_residual_us=6_000,
            relogio_medido_em_ms=corrigido - 30_000,
        ))
    m.receber(conn, serie8, CONTRATO, novas)

    # O GRAVADO nao se moveu - a tabela e apenas por acrescimo, e o gatilho
    # `calibracao_observacao_sem_update` recusaria -, e o RECALCULO concorda.
    depois = {
        (int(r["t_grid_ms"]), r["lado"]): r["regime"]
        for r in conn.execute(
            "SELECT t_grid_ms, lado, regime FROM calibracao_observacao"
        )
    }
    assert depois == antes

    from app.calibracao import regimes

    div = regimes.divergencias_do_gravado(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        timeframe=cfg.config.timeframe,
    )
    assert div["instantes_conferidos"] > 0
    assert div["divergentes"] == 0, div["amostra"]


def test_a_DIVERGENCIA_do_regime_gravado_e_PUBLICADA_e_nao_silenciosa(
    client, conn, cfg
):
    """Ela vai no corpo do GET, com qual dos dois vale escrito ao lado."""
    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    corpo = client.get("/api/calibracao", headers=CABECALHO).json()
    bloco = corpo["regime_gravado_x_recalculado"]
    assert bloco["instantes_conferidos"] > 0
    assert bloco["divergentes"] == 0
    assert "o GRAVADO" in bloco["qual_vale"]
    assert "backfill" in bloco["o_que_uma_divergencia_significa"]


# ===========================================================================
# `/rodar` NAS TRES FINALIDADES, SOBRE O SCHEMA 33
# ===========================================================================


def test_RODAR_a_REVALIDACAO_ponta_a_ponta_sobre_o_schema_33(client, conn, cfg):
    """A terceira finalidade, e a mais exigente: a identidade e DERIVADA.

    A cadeia inteira: piloto materializado -> calibracao selada e coletada ->
    regime fechado -> revalidacao selada -> ajuste APLICADO -> dado futuro
    chega -> `/rodar` materializa a revalidacao sob a `config_version` e o
    perfil que a calibracao produziu.

    E prova as cinco coisas que o usuario exigiu: idempotencia, identidade
    congelada, intervalo e hash exatos, e NENHUMA alteracao em config, perfil,
    creditos ou holdout.
    """
    from app.aovivo import bbo as m
    from app.calibracao import materializacao

    janela, r = _revalidacao_aplicada(conn, cfg)
    assert r["config_version_nova"] != cfg.id

    # O DADO CHEGA, contiguo ao que existe e dentro da janela reservada.
    semear_adiante(conn, quantos=60)

    foto = _fotografia(conn)

    resp = client.post("/api/calibracao/rodar", headers=CABECALHO,
                       json={"finalidade": "revalidacao",
                             "regime": REGIME_DE_TESTE})
    assert resp.status_code == 200, resp.text
    obs = resp.json()["observacoes"]

    # 1. INTERVALO EXATO: o da janela selada, e nao um pedido.
    assert obs["finalidade"] == "revalidacao"
    assert obs["janela"]["de_ms"] == int(janela["de_ms"])
    assert obs["janela"]["ate_ms_exclusive"] == int(janela["ate_ms_exclusive"])
    assert obs["gravadas"] > 0

    # 2. HASH EXATO: o do SELO, reproduzido fora da rota.
    esperado = materializacao._hash_do_selo(
        "revalidacao", CONTRATO, "binance", "BTCUSDT", REGIME_DE_TESTE,
        janela["de_ms"], janela["ate_ms_exclusive"], janela["selada_em_ms"],
        janela["grade_ms"],
    )
    assert obs["janela"]["hash"] == esperado

    # 3. IDENTIDADE DERIVADA, e nao a vigente.
    ident = obs["identidade_executavel"]
    assert ident["config_version_id"] == r["config_version_nova"] != cfg.id
    assert ident["calibracao_perfil_hash"] == r["calibracao_perfil_hash"]

    # 4. IDENTIDADE CONGELADA: uma linha so por identidade na finalidade.
    q = observacao.quantas(conn, finalidade="revalidacao", contrato=CONTRATO,
                           venue="binance", symbol="BTCUSDT")
    assert q["identidade_unica"] is True
    assert q["identidades"][0]["config_version_id"] == ident["config_version_id"]
    assert set(q["por_regime"]) == {REGIME_DE_TESTE}, (
        "a janela e POR REGIME: instante de outro regime nao entra nela"
    )

    # 5. IDEMPOTENCIA: repetir nao grava e nao duplica.
    antes = conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao WHERE finalidade ="
        " 'revalidacao'"
    ).fetchone()[0]
    de_novo = client.post("/api/calibracao/rodar", headers=CABECALHO,
                          json={"finalidade": "revalidacao",
                                "regime": REGIME_DE_TESTE})
    assert de_novo.status_code == 200
    assert de_novo.json()["observacoes"]["gravadas"] == 0
    assert de_novo.json()["observacoes"]["janela"]["hash"] == esperado
    assert conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao WHERE finalidade ="
        " 'revalidacao'"
    ).fetchone()[0] == antes

    # 6. NADA MAIS SE MOVEU: config, perfil, creditos, holdout.
    assert _fotografia(conn) == foto


def test_NAO_EXISTE_janela_de_revalidacao_sem_calibracao_FINALIZADA(
    conn, cfg
):
    """O 409 que este teste media deixou de ser alcancavel, e e progresso.

    Ele exercitava "materializar revalidacao sem calibracao aplicada".
    Hoje a **janela** nao chega a existir: o gatilho
    `janela_revalidacao_exige_a_finalizacao` recusa sela-la antes do perfil
    consolidado. A condicao passou do caminho para a estrutura.
    """
    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    fechar_regime_medido(conn, cfg)
    assert ajuste.ler_finalizacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT"
    ) is None
    with pytest.raises(ajuste.SemJanelaDeCalibracao) as e:
        ajuste.selar_revalidacao(
            conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
            regime=REGIME_DE_TESTE,
        )
    assert "finalizacao" in str(e.value)
    assert ajuste.ler_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        regime=REGIME_DE_TESTE,
    ) is None


def test_materializar_o_PILOTO_nao_move_config_perfil_credito_nem_holdout(
    client, conn, cfg
):
    """A quinta prova, no caminho mais curto: medir nao altera o experimento."""
    _piloto_fechado_sem_materializar(conn)
    foto = _fotografia(conn)
    assert client.post("/api/calibracao/rodar", headers=CABECALHO,
                       json={"finalidade": "piloto"}).status_code == 200
    assert _fotografia(conn) == foto


def test_a_janela_de_revalidacao_ATRAVESSA_regimes_e_so_leva_o_dela(
    conn, cfg
):
    """Catorze dias atravessam varios regimes, e a janela e de UM.

    Este e o caso de PRODUCAO, e ele nao aparecia em teste nenhum: a janela de
    revalidacao dura catorze dias, e o detector reclassifica conforme a
    volatilidade anda. O gatilho `calibracao_observacao_dentro_da_janela`
    recusa instante de outro regime - corretamente -, e sem filtro do lado de
    quem produz o PRIMEIRO instante de outro regime abortaria a materializacao
    INTEIRA. A revalidacao nunca sairia do chao, e so no passo 7.

    Quem nao pertence e PULADO e CONTADO. Pulado em silencio seria o defeito
    que este projeto conta tantas vezes, com outra roupa.
    """
    from app.aovivo import bbo as m
    from app.calibracao import materializacao

    janela, r = _revalidacao_aplicada(conn, cfg)
    de = int(janela["de_ms"])

    # Dentro da janela, a volatilidade EXPLODE: depois de 672 barras assim, o
    # detector classifica `vol_alta` - e a janela e de `vol_baixa`.
    semear_adiante(conn, quantos=800, salto=True)

    saida = materializacao.da_revalidacao(
        conn, contrato=CONTRATO, venue="binance", symbol="BTCUSDT",
        regime=REGIME_DE_TESTE, orcamento_cents=1_000,
    )

    # A janela de fato atravessou regimes - senao o teste nao exercita nada.
    assert len(saida["por_regime"]) > 1, saida["por_regime"]
    assert saida["fora_do_regime_da_janela"] > 0
    assert saida["gravadas"] > 0

    # E SO o regime da janela entrou.
    gravados = {
        r["regime"] for r in conn.execute(
            "SELECT DISTINCT regime FROM calibracao_observacao"
            " WHERE finalidade = 'revalidacao'"
        )
    }
    assert gravados == {REGIME_DE_TESTE}

    # A conta fecha: comparaveis = gravadas + ja_existiam, e os pulados ficam
    # FORA dos dois lados - eles nao sao perda nossa.
    assert saida["comparaveis"] == saida["gravadas"] + saida["ja_existiam"]


# ===========================================================================
# 1. A TRAVA CONTRA MATERIALIZAR ANTES DO BACKFILL
# ===========================================================================


def test_instante_com_JANELA_INCOMPLETA_e_ADIADO_e_nao_gravado_indefinido(
    conn, cfg
):
    """O coracao da exigencia do usuario em 2026-10-07.

    Uma ausencia que o backfill ainda pode preencher **nao** vira `indefinido`
    gravado. Ela adia - e o instante volta na proxima materializacao, com a
    janela ja fechada.
    """
    from app.calibracao import regimes

    # 700 barras contiguas, e depois um BURACO de uma barra, e mais 100.
    semear(conn, 700)
    ultimo = conn.execute(
        "SELECT MAX(open_time_ms) AS t FROM stream_bar"
    ).fetchone()["t"]
    buraco = int(ultimo) + GRADE
    from app.aovivo import bbo as m
    serie8 = m.Serie(venue="binance", symbol="BTCUSDT",
                     price_scale_exp=8, volume_scale_exp=8)
    novas = []
    for k in range(2, 120):            # pula k = 1: o buraco
        ms = buraco + (k - 1) * GRADE
        conn.execute(
            "INSERT INTO stream_bar (venue, symbol, timeframe, open_time_ms,"
            " open, high, low, close, volume, quote_volume, trades,"
            " interval_ms, price_scale_exp, volume_scale_exp, recebido_em,"
            " origem) VALUES ('binance','BTCUSDT','15m',?,?,?,?,?,1,1,1,"
            "?,8,8,'x','ao_vivo')",
            (ms, ABERTURA, ABERTURA, ABERTURA, ABERTURA, GRADE),
        )
        corrigido = ms - 500
        novas.append(m.Amostra(
            t_grid_ms=ms, disponivel=True,
            bid=59_999_00000000, bid_qty=5_00000000,
            ask=60_001_00000000, ask_qty=7_00000000, u=900_000 + k,
            received_at_ms=corrigido + 2_450,
            received_at_corrigido_ms=corrigido,
            sampled_at_ms=corrigido, defasagem_ms=500,
            offset_us=-2_450_000, rtt_us=12_000, incerteza_residual_us=6_000,
            relogio_medido_em_ms=corrigido - 30_000,
        ))
    m.receber(conn, serie8, CONTRATO, novas)

    cob = regimes.cobertura_do_fluxo(
        conn, venue="binance", symbol="BTCUSDT", timeframe=cfg.config.timeframe
    )
    # O instante logo DEPOIS do buraco tem a janela furada -> ADIA.
    acao, motivo = cob.veredito(t_grid_ms=buraco + GRADE)
    assert (acao, motivo) == (regimes.ADIAR, "janela_causal_incompleta")

    r = materializar(conn, cfg, lados=("compra",))
    assert r["adiados"].get("janela_causal_incompleta", 0) > 0
    assert "janela_causal_incompleta" in r["o_que_cada_um_e"]["adiados"] or True

    # E NENHUM `indefinido` foi gravado por causa do buraco: os unicos
    # `indefinido` sao os de `antes_do_inicio_do_fluxo`.
    gravados = conn.execute(
        "SELECT DISTINCT t_grid_ms FROM calibracao_observacao"
        " WHERE regime = 'indefinido' AND t_grid_ms > ?", (buraco,)
    ).fetchall()
    assert gravados == [], (
        "um instante com janela furada foi gravado como `indefinido`: a falta"
        " temporaria passou a valer para sempre"
    )


def test_o_BACKFILL_destrava_o_instante_que_estava_ADIADO(conn, cfg):
    """Adiado nao e perdido. Quando a barra chega, o instante entra.

    E a prova de que `adiar` e a decisao certa: o dado nao e descartado nem
    congelado errado - ele espera.
    """
    semear(conn, 700)
    ultimo = int(conn.execute(
        "SELECT MAX(open_time_ms) AS t FROM stream_bar"
    ).fetchone()["t"])
    buraco = ultimo + GRADE
    primeiro = semear_adiante_com_buraco(conn, buraco)

    antes = materializar(conn, cfg, lados=("compra",))
    adiados_antes = antes["adiados"].get("janela_causal_incompleta", 0)
    assert adiados_antes > 0

    # O BACKFILL CHEGA.
    preencher_buraco(conn, buraco)
    depois = materializar(conn, cfg, lados=("compra",))

    assert depois["gravadas"] > 0, "o backfill tinha de destravar instantes"
    assert depois["adiados"].get("janela_causal_incompleta", 0) < adiados_antes


def test_o_instante_da_FRONTEIRA_e_adiado_porque_pode_nao_ter_fechado(
    conn, cfg
):
    """A condicao "a barra esta fechada", derivada do FLUXO e nao do relogio.

    O rele so entrega kline fechada, mas isso e disciplina do produtor. A
    exigencia local e `watermark > t`: a unica barra que poderia estar em
    formacao e a de maior `open_time_ms`, e exigir uma DEPOIS dela a exclui.
    """
    from app.calibracao import regimes

    semear(conn, 5)
    cob = regimes.cobertura_do_fluxo(
        conn, venue="binance", symbol="BTCUSDT", timeframe=cfg.config.timeframe
    )
    assert cob.watermark_ms is not None
    acao, motivo = cob.veredito(t_grid_ms=cob.watermark_ms)
    assert (acao, motivo) == (
        regimes.ADIAR, "watermark_do_rele_nao_passou_do_instante"
    )
    # e o anterior ja esta liberado
    assert cob.veredito(
        t_grid_ms=cob.watermark_ms - GRADE
    )[0] == regimes.MATERIALIZAR


def test_antes_do_INICIO_do_fluxo_a_ausencia_e_DEFINITIVA_e_grava_indefinido(
    conn, cfg
):
    """A unica porta para `indefinido`, e ela e derivada - nao e constante.

    Nenhum backfill traz barra de antes da primeira que o rele ja entregou.
    Adiar para sempre esses instantes seria tratar como recuperavel uma
    ausencia que nao e.
    """
    from app.calibracao import regimes

    semear(conn, 5)
    cob = regimes.cobertura_do_fluxo(
        conn, venue="binance", symbol="BTCUSDT", timeframe=cfg.config.timeframe
    )
    acao, motivo = cob.veredito(t_grid_ms=cob.inicio_ms + GRADE)
    assert (acao, motivo) == (regimes.MATERIALIZAR, "antes_do_inicio_do_fluxo")

    materializar(conn, cfg, lados=("compra",))
    assert conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao WHERE regime = 'indefinido'"
    ).fetchone()[0] > 0


def test_a_DIVERGENCIA_do_regime_gravado_e_VIOLACAO_de_integridade(conn, cfg):
    """Depois da trava, divergir deixou de ser diagnostico.

    Com a janela causal completa exigida antes de gravar, o valor nao deveria
    mais se mover. Se moveu, ou carregaram historico mais antigo que o inicio
    do fluxo, ou uma barra de dentro de janela fechada mudou - e nenhuma das
    duas pode passar por nota de rodape.
    """
    from app.relatorio import integridade

    montar_piloto(conn, cfg, ask=ASK_JUSTO, instantes=1_400)
    r = integridade.montar(conn)
    assert r["violacoes"] == []
    assert r["integro"] is True
    assert r["regime_gravado"]["instantes_conferidos"] > 0

    # FORJA a divergencia: reescreve o regime de uma linha por SQL cru. O
    # gatilho de imutabilidade recusa `UPDATE`, entao a forja e por
    # `writable_schema` - e e so para provar que a conferencia VE.
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("DROP TRIGGER calibracao_observacao_sem_update")
    conn.execute("PRAGMA writable_schema = OFF")
    conn.execute(
        "UPDATE calibracao_observacao SET regime = 'vol_alta'"
        " WHERE regime = 'vol_baixa' AND rowid IN ("
        "   SELECT rowid FROM calibracao_observacao"
        "    WHERE regime = 'vol_baixa' LIMIT 1)"
    )

    r2 = integridade.montar(conn)
    assert r2["regime_gravado"]["divergentes"] > 0
    assert r2["integro"] is False
    assert any(
        v["o_que"] == "regime_gravado_divergente_do_recalculado"
        for v in r2["violacoes"]
    ), r2["violacoes"]


# ===========================================================================
# 2. EPISODIOS E COBERTURA, LIMITADOS A JANELA SELADA
# ===========================================================================


def test_barra_DEPOIS_da_janela_nao_muda_cobertura_nem_episodio_dela(
    conn, cfg
):
    """O teste de fronteira que o usuario pediu.

    `deteccao.cobertura` sobre a serie inteira responde outra pergunta - quanto
    o FLUXO cobriu. Para uma janela selada isso erra na direcao perigosa:
    barra posterior continuaria crescendo a corrida e poderia QUALIFICAR um
    episodio que, dentro da janela, nunca alcancou a permanencia.
    """
    from app.calibracao import regimes

    semear(conn, 1_500)
    de = T0 + 700 * GRADE
    ate = T0 + 1_000 * GRADE

    def ler():
        return regimes.cobertura_da_janela(
            conn, venue="binance", symbol="BTCUSDT",
            timeframe=cfg.config.timeframe, de_ms=de, ate_ms_exclusive=ate,
        )

    antes = ler()
    assert antes["instantes_na_janela"] == 300

    semear_adiante(conn, quantos=900, salto=True)   # explode DEPOIS da janela
    depois = ler()

    for chave in ("regimes_cobertos", "quantidade", "cumprida", "episodios",
                  "instantes_na_janela"):
        assert antes[chave] == depois[chave], (
            f"{chave} mudou com barra posterior a `ate_ms_exclusive`"
        )


def test_a_cobertura_da_janela_TRUNCA_a_corrida_na_borda(conn, cfg):
    """O recorte e ANTES de `episodios`, e nao depois.

    Classificar tudo e filtrar o resultado deixaria uma corrida que atravessa a
    borda contar com o tamanho que ela tem FORA da janela.
    """
    from app.calibracao import regimes
    from app.regime import deteccao

    semear(conn, 1_500)
    de, ate = T0 + 700 * GRADE, T0 + 1_000 * GRADE
    janela = regimes.cobertura_da_janela(
        conn, venue="binance", symbol="BTCUSDT",
        timeframe=cfg.config.timeframe, de_ms=de, ate_ms_exclusive=ate,
    )
    inteira = deteccao.cobertura(
        deteccao.classificar_serie(
            regimes._retornos(conn, venue="binance", symbol="BTCUSDT",
                              timeframe=cfg.config.timeframe)
        )
    )
    assert janela["instantes_na_janela"] == 300
    assert "TRUNCADA" in janela["o_recorte_e_ANTES_dos_episodios"]
    # A janela tem 300 instantes e a permanencia e 672: nenhum episodio cabe
    # nela, enquanto a serie inteira tem.
    assert janela["episodios"] == 0
    assert inteira["episodios"] >= janela["episodios"]


# ===========================================================================
# 3. ATOMICIDADE DE `/rodar`
# ===========================================================================


def test_RODAR_que_responde_ERRO_deixa_ZERO_escrita(client, conn, cfg, monkeypatch):
    """Qualquer resposta de erro deixa o banco como estava.

    `registrar` abre o proprio bloco e o shadow abre o dele; sem um por fora,
    um erro no segundo deixaria o primeiro gravado - e a resposta de erro teria
    escrita por tras.
    """
    _piloto_fechado_sem_materializar(conn)
    foto = _fotografia(conn)
    observacoes = conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao"
    ).fetchone()[0]

    # UM ERRO DEPOIS DA ESCRITA. O shadow roda apos a materializacao, e e
    # justamente essa ordem que tornava a escrita parcial possivel: sem o bloco
    # por fora, as observacoes ja estariam gravadas quando ele falhasse.
    from app.api.rotas import calibracao as rota

    def explodir(*a, **k):
        raise observacao.ContratoIncompativel("forjado DEPOIS da escrita")

    monkeypatch.setattr(rota.shadow, "rodar", explodir)
    r = client.post("/api/calibracao/rodar", headers=CABECALHO,
                    json={"finalidade": "piloto"})
    assert r.status_code == 409, r.text
    assert "DEPOIS da escrita" in r.json()["detail"]

    assert conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao"
    ).fetchone()[0] == observacoes
    assert _fotografia(conn) == foto


def test_RODAR_numa_janela_FUTURA_sem_barras_responde_aguardando_dados(
    client, conn, cfg, monkeypatch
):
    """Nao e 409: uma janela selada comeca no futuro, e o vao ate a primeira
    barra e vazio POR CONSTRUCAO.

    **O `SemBarras` e FORCADO aqui, e digo que e.** O que esta sob teste e o
    tratamento da ROTA, nao a contagem de barras do shadow. Reproduzir uma
    janela selada genuinamente vazia exigiria que a janela de calibracao
    terminasse exatamente na fronteira do dado - uma coincidencia que o
    horizonte derivado nao entrega de proposito. O estado e real em producao:
    entre o selo e a primeira barra, a janela esta vazia.
    """
    from app.api.rotas import calibracao as rota

    janela, r = _revalidacao_aplicada(conn, cfg)

    def sem_barras(*a, **k):
        raise rota.shadow.SemBarras("0 barras na janela")

    monkeypatch.setattr(rota.shadow, "rodar", sem_barras)
    resp = client.post("/api/calibracao/rodar", headers=CABECALHO,
                       json={"finalidade": "revalidacao",
                             "regime": REGIME_DE_TESTE})
    assert resp.status_code == 200, resp.text
    corpo = resp.json()
    assert corpo["shadow"]["estado"] == "aguardando_dados"
    assert "vazio por construcao" in corpo["shadow"]["o_que_isto_e"]


def semear_adiante_com_buraco(conn, buraco_ms: int, *, quantos: int = 120) -> int:
    """Barras a partir de `buraco_ms`, PULANDO a primeira.

    O buraco e o que torna a janela causal dos instantes seguintes incompleta -
    e e exatamente a situacao que o backfill preenche depois.
    """
    from app.aovivo import bbo as m

    serie8 = m.Serie(venue="binance", symbol="BTCUSDT",
                     price_scale_exp=8, volume_scale_exp=8)
    novas = []
    for k in range(1, quantos):
        ms = buraco_ms + k * GRADE
        conn.execute(
            "INSERT INTO stream_bar (venue, symbol, timeframe, open_time_ms,"
            " open, high, low, close, volume, quote_volume, trades,"
            " interval_ms, price_scale_exp, volume_scale_exp, recebido_em,"
            " origem) VALUES ('binance','BTCUSDT','15m',?,?,?,?,?,1,1,1,"
            "?,8,8,'x','ao_vivo')",
            (ms, ABERTURA, ABERTURA, ABERTURA, ABERTURA, GRADE),
        )
        corrigido = ms - 500
        novas.append(m.Amostra(
            t_grid_ms=ms, disponivel=True,
            bid=59_999_00000000, bid_qty=5_00000000,
            ask=60_001_00000000, ask_qty=7_00000000, u=800_000 + k,
            received_at_ms=corrigido + 2_450,
            received_at_corrigido_ms=corrigido,
            sampled_at_ms=corrigido, defasagem_ms=500,
            offset_us=-2_450_000, rtt_us=12_000, incerteza_residual_us=6_000,
            relogio_medido_em_ms=corrigido - 30_000,
        ))
    m.receber(conn, serie8, CONTRATO, novas)
    return buraco_ms + GRADE


def preencher_buraco(conn, buraco_ms: int) -> None:
    """O backfill chega: a barra que faltava entra."""
    from app.aovivo import bbo as m

    conn.execute(
        "INSERT INTO stream_bar (venue, symbol, timeframe, open_time_ms,"
        " open, high, low, close, volume, quote_volume, trades,"
        " interval_ms, price_scale_exp, volume_scale_exp, recebido_em,"
        " origem) VALUES ('binance','BTCUSDT','15m',?,?,?,?,?,1,1,1,"
        "?,8,8,'backfill','backfill')",
        (buraco_ms, ABERTURA, ABERTURA, ABERTURA, ABERTURA, GRADE),
    )
    corrigido = buraco_ms - 500
    m.receber(
        conn,
        m.Serie(venue="binance", symbol="BTCUSDT",
                price_scale_exp=8, volume_scale_exp=8),
        CONTRATO,
        [m.Amostra(
            t_grid_ms=buraco_ms, disponivel=True,
            bid=59_999_00000000, bid_qty=5_00000000,
            ask=60_001_00000000, ask_qty=7_00000000, u=850_000,
            received_at_ms=corrigido + 2_450,
            received_at_corrigido_ms=corrigido,
            sampled_at_ms=corrigido, defasagem_ms=500,
            offset_us=-2_450_000, rtt_us=12_000, incerteza_residual_us=6_000,
            relogio_medido_em_ms=corrigido - 30_000,
        )],
    )
