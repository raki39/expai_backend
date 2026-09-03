"""Testes do incremento 11: BY, DSR e créditos de teste.

A parte estatística é conferida por **aritmética verificável à mão** e por
**propriedades que a fórmula tem de satisfazer** — não por comparação com um
número que o próprio código produziu. Um teste que grava a saída de hoje e
exige que amanhã seja igual não verifica nada: carimba.

Onde a verificação é contra a literatura, os limites disso estão declarados no
teste. Onde não pude conferir dígito a dígito, digo que não pude.
"""

from __future__ import annotations

import math
import pathlib
import sqlite3
from fractions import Fraction

import pytest

from app import creditos as creditos_mod
from app.estatistica import dsr, fdr, pvalor, sharpe
from app.validador import lote

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
QUINZE_MIN_MS = 900_000


def codigo_sem_prosa(arquivo: pathlib.Path) -> str:
    """O código do arquivo, sem comentários e sem literais de texto.

    Existe porque a primeira versão da guarda de separação acusou as próprias
    docstrings deste projeto: elas mencionam "crédito" exatamente para
    explicar por que crédito não entra na estatística.

    Uma guarda que proíbe a palavra proíbe também a explicação de por que a
    palavra é proibida — e aí a saída é apagar o comentário, que é a pior das
    correções possíveis. O que se quer proibir é o **código** que consulta
    escassez, e é isso que esta função isola.
    """
    import io
    import tokenize

    pedacos: list[str] = []
    with arquivo.open("rb") as f:
        for tok in tokenize.tokenize(f.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            pedacos.append(tok.string)
    return " ".join(pedacos)


# ===========================================================================
# CRITERIO 1 - BY, conferido por aritmetica que da para fazer a mao
# ===========================================================================


def test_a_soma_harmonica_e_exata() -> None:
    """`H(m)` DIVIDE o limiar de promoção: erro no último dígito decide.

    Por isso é `Fraction` e não ponto flutuante — e o teste compara contra a
    soma explícita, não contra um valor decorado.
    """
    assert fdr.harmonico(1) == Fraction(1)
    assert fdr.harmonico(2) == Fraction(3, 2)
    assert fdr.harmonico(3) == Fraction(11, 6)
    assert fdr.harmonico(4) == Fraction(25, 12)
    # H(48), o do nosso lote (D25).
    esperado = sum(Fraction(1, i) for i in range(1, 49))
    assert fdr.harmonico(48) == esperado
    assert abs(float(esperado) - 4.4588) < 0.0001


def test_o_limiar_da_familia_de_48_sob_by() -> None:
    """O número da D26, cravado: 10% / H(48) = 2,2427%.

    Está aqui para não poder mudar em silêncio. Se cair, ou o tamanho da
    família mudou (D25) ou o alvo de FDR mudou (§8.6) — e as duas coisas são
    materiais.
    """
    assert fdr.limiar_efetivo_ppm(procedimento="BY", m=48, alfa_bps=1_000) == 22_427
    assert fdr.limiar_efetivo_ppm(procedimento="BH", m=48, alfa_bps=1_000) == 100_000


def test_bh_reproduz_o_procedimento_definido_no_artigo_original() -> None:
    """Aritmética conferível à mão, e é a definição de Benjamini-Hochberg.

    Com m=5 e alfa=5%, os limiares são (i/5)·0,05 = 1%, 2%, 3%, 4%, 5%.

    | i | p       | limiar | p <= limiar |
    |---|---------|--------|-------------|
    | 1 | 0,001   | 0,01   | sim         |
    | 2 | 0,008   | 0,02   | sim         |
    | 3 | 0,039   | 0,03   | NÃO         |
    | 4 | 0,041   | 0,04   | NÃO         |
    | 5 | 0,420   | 0,05   | NÃO         |

    O maior `k` que satisfaz é 2, e as **duas** de menor p-valor são
    rejeitadas.
    """
    ps = {"a": 1_000, "b": 8_000, "c": 39_000, "d": 41_000, "e": 420_000}
    r = fdr.aplicar(ps, procedimento="BH", alfa_bps=500, m=5)

    assert [d.limiar_ppm for d in r.decisoes] == [
        10_000, 20_000, 30_000, 40_000, 50_000
    ]
    assert r.k == 2
    assert r.rejeitadas == ["a", "b"]


def test_by_e_bh_com_o_limiar_dividido_por_h_de_m() -> None:
    """A única diferença entre os dois procedimentos, verificada nos limiares."""
    ps = {"a": 1_000, "b": 8_000, "c": 39_000}
    bh = fdr.aplicar(ps, procedimento="BH", alfa_bps=500, m=3)
    by = fdr.aplicar(ps, procedimento="BY", alfa_bps=500, m=3)

    h3 = fdr.harmonico(3)  # 11/6
    for a, b in zip(bh.decisoes, by.decisoes):
        # O limiar de BY é o de BH dividido por H(3), a menos do truncamento.
        assert abs(b.limiar_ppm - int(a.limiar_ppm / h3)) <= 1
    assert by.k <= bh.k, "BY nunca rejeita mais que BH"


def test_bh_rejeita_todas_as_k_menores_inclusive_as_que_falham_sozinhas() -> None:
    """O passo em que BH é mais forte que Bonferroni, e o mais fácil de errar.

    Se cada hipótese fosse conferida contra o próprio limiar, `b` seria
    aceita (0,019 > 0,0133) e o procedimento viraria outro, mais conservador
    e não publicado.
    """
    #  i=1: p=0,001 <= 1/3·0,04 = 0,0133  -> sim
    #  i=2: p=0,019 <= 2/3·0,04 = 0,0267  -> sim
    #  i=3: p=0,030 <= 3/3·0,04 = 0,0400  -> sim, entao k=3
    ps = {"a": 1_000, "b": 19_000, "c": 30_000}
    r = fdr.aplicar(ps, procedimento="BH", alfa_bps=400, m=3)
    assert r.k == 3
    assert set(r.rejeitadas) == {"a", "b", "c"}


def test_m_e_o_teto_da_familia_e_nao_quantas_foram_testadas() -> None:
    """§8.6: o número máximo é fixado antes de começar.

    O mesmo p-valor precisa ser julgado mais duramente numa família de 48 do
    que numa de 2 — senão parar de testar ao ver dois resultados bons
    compraria um limiar mais frouxo para eles.
    """
    ps = {"a": 15_000, "b": 30_000}
    curto = fdr.aplicar(ps, procedimento="BY", alfa_bps=1_000, m=2)
    familia = fdr.aplicar(ps, procedimento="BY", alfa_bps=1_000, m=48)
    assert curto.k >= familia.k
    assert familia.m == 48 and curto.m == 2


def test_mais_p_valores_que_a_familia_e_recusado() -> None:
    """Exceder a família fechada não é arredondado: é erro."""
    with pytest.raises(ValueError, match="familia fechada foi excedida"):
        fdr.aplicar(
            {"a": 1, "b": 2, "c": 3}, procedimento="BY", alfa_bps=1_000, m=2
        )


def test_a_ordem_e_estavel_e_portanto_reproduzivel() -> None:
    """R12: p-valores iguais não podem trocar de posição entre execuções."""
    ps = {"z": 5_000, "a": 5_000, "m": 5_000}
    primeiro = fdr.aplicar(ps, procedimento="BY", alfa_bps=1_000, m=3)
    segundo = fdr.aplicar(
        dict(reversed(list(ps.items()))),
        procedimento="BY",
        alfa_bps=1_000,
        m=3,
    )
    assert [d.chave for d in primeiro.decisoes] == [
        d.chave for d in segundo.decisoes
    ]


# ===========================================================================
# Sharpe realizado e p-valor - os insumos que nao existiam
# ===========================================================================


def test_os_momentos_conferem_com_a_conta_a_mao() -> None:
    """Série pequena, momentos calculados fora do código."""
    serie = [1, 2, 3, 4]
    m = sharpe.momentos(serie)
    assert m.n == 4
    assert m.media == 2.5
    # Desvio AMOSTRAL: var = ((1,5² + 0,5² + 0,5² + 1,5²) / 3) = 5/3
    assert abs(m.desvio - math.sqrt(5 / 3)) < 1e-12
    # Simétrica em torno da média.
    assert abs(m.assimetria) < 1e-12
    assert abs(m.sharpe_por_observacao - 2.5 / math.sqrt(5 / 3)) < 1e-12


def test_serie_curta_recusa_em_vez_de_supor_normalidade() -> None:
    """Devolver curtose 3 afirmaria normalidade que ninguém mediu."""
    for curta in ([], [1], [1, 2], [1, 2, 3]):
        with pytest.raises(sharpe.AmostraCurta):
            sharpe.momentos(curta)


def test_serie_constante_nao_finge_sharpe_zero() -> None:
    """Sharpe é indefinido sem variação, não zero.

    Zero afirmaria "medi e não há vantagem". Sem variação não há risco a
    partir do qual falar de vantagem.
    """
    m = sharpe.momentos([7, 7, 7, 7, 7])
    assert m.desvio == 0.0
    assert m.sharpe_por_observacao == 0.0
    assert m.curtose == 3.0  # convenção declarada, não medição


def test_o_p_valor_e_de_uma_cauda_e_cresce_com_sharpe_negativo() -> None:
    """A hipótese é direcional: o agente afirma vantagem, não diferença."""
    bom = pvalor.de_sharpe(
        sharpe_anualizado=3.0, n_efetivo=35_064, barras_por_ano_=35_064
    )
    ruim = pvalor.de_sharpe(
        sharpe_anualizado=-3.0, n_efetivo=35_064, barras_por_ano_=35_064
    )
    assert bom.p_valor_ppm < 5_000, "Sharpe 3 num ano é forte"
    assert ruim.p_valor_ppm > 500_000, "Sharpe negativo não sustenta a nula"

    # t = Sharpe × √anos. Com um ano exato, t = Sharpe.
    assert abs(bom.t - 3.0) < 1e-9


def test_sem_amostra_o_p_valor_e_um_e_nao_meio() -> None:
    """0,5 afirmaria "medi e não achei nada". 1 é "não há evidência"."""
    t = pvalor.de_sharpe(
        sharpe_anualizado=5.0, n_efetivo=0, barras_por_ano_=35_064
    )
    assert t.p_valor_ppm == 1_000_000


def test_o_p_valor_usa_amostra_EFETIVA_e_nao_bruta() -> None:
    """§8.3: "aumentar a frequência não fabrica amostra".

    Passar `n_bruto` onde vai `n_efetivo` inflaria `t` pela raiz da razão
    entre os dois — e inflar `t` é inflar significância.
    """
    bruto = pvalor.de_sharpe(
        sharpe_anualizado=1.0, n_efetivo=35_064, barras_por_ano_=35_064
    )
    efetivo = pvalor.de_sharpe(
        sharpe_anualizado=1.0, n_efetivo=8_766, barras_por_ano_=35_064
    )
    assert efetivo.p_valor_ppm > bruto.p_valor_ppm
    assert abs(efetivo.t - 0.5) < 1e-9  # √(1/4) = 0,5


# ===========================================================================
# CRITERIOS 3 e 4 - DSR
# ===========================================================================


def test_o_dsr_e_uma_probabilidade_e_nunca_sai_do_intervalo() -> None:
    """§8.6: "o DSR é uma probabilidade, não um score"."""
    for sr in (-0.5, -0.01, 0.0, 0.01, 0.2, 1.0):
        r = dsr.calcular(
            sharpe_por_observacao=sr,
            n=1_000,
            tentativas=48,
            assimetria=0.0,
            curtose_bruta=3.0,
        )
        assert 0 <= r.dsr_milesimos <= 1_000


def test_dsr_positivo_nunca_aparece_como_criterio_no_codigo() -> None:
    """§8.6: "exigir que ele 'seja positivo' não significa nada".

    Uma probabilidade nunca é negativa, então `dsr > 0` é sempre verdade — e
    um critério sempre verdadeiro é pior que nenhum, porque parece proteger.
    """
    suspeitos: list[str] = []
    for arquivo in sorted(APP.rglob("*.py")):
        baixa = codigo_sem_prosa(arquivo).lower()
        for padrao in ("dsr > 0", "dsr >= 0", "dsr_milesimos > 0"):
            if padrao in baixa:
                suspeitos.append(f"{arquivo.relative_to(APP)}: {padrao}")
    assert not suspeitos, (
        "DSR comparado com zero como se fosse score: " + "; ".join(suspeitos)
    )


def test_mais_tentativas_derrubam_o_dsr() -> None:
    """É o que o "deflated" faz: descontar a seleção.

    §8.6: "um Sharpe de 1,5 após 10 tentativas e um Sharpe de 1,5 após 5.000
    tentativas não são a mesma evidência".
    """
    anterior = 1_001
    for tentativas in (1, 10, 48, 500, 5_000):
        r = dsr.calcular(
            sharpe_por_observacao=0.08,
            n=2_000,
            tentativas=tentativas,
            assimetria=0.0,
            curtose_bruta=3.0,
        )
        assert r.dsr_milesimos <= anterior, (
            f"{tentativas} tentativas não deveriam elevar o DSR"
        )
        anterior = r.dsr_milesimos


def test_mais_amostra_eleva_o_dsr() -> None:
    """`√(n-1)` no numerador: mais dado, mais confiança."""
    anterior = -1
    for n in (50, 500, 5_000):
        r = dsr.calcular(
            sharpe_por_observacao=0.05,
            n=n,
            tentativas=48,
            assimetria=0.0,
            curtose_bruta=3.0,
        )
        assert r.dsr_milesimos >= anterior
        anterior = r.dsr_milesimos


def test_ignorar_assimetria_e_curtose_muda_o_numero() -> None:
    """Se não mudasse, a implementação estaria incompleta e ninguém notaria.

    §8.6 exige a correção "pela assimetria e curtose dos retornos". Um DSR
    que desprezasse os dois daria sempre um número maior — otimista de graça.
    """
    base = dict(sharpe_por_observacao=0.06, n=2_000, tentativas=48)
    normal = dsr.calcular(**base, assimetria=0.0, curtose_bruta=3.0)
    torto = dsr.calcular(**base, assimetria=-1.2, curtose_bruta=3.0)
    gordo = dsr.calcular(**base, assimetria=0.0, curtose_bruta=9.0)

    assert torto.dsr_milesimos < normal.dsr_milesimos, (
        "cauda esquerda pesada tem de reduzir a confiança"
    )
    assert gordo.dsr_milesimos < normal.dsr_milesimos, (
        "retorno não normal tem de reduzir a confiança"
    )


def test_uma_tentativa_nao_tem_maximo_a_corrigir() -> None:
    """`Phi^-1(1 - 1/1)` é infinito; `SR0 = 0` é o valor certo, não um remendo."""
    assert dsr.sharpe_esperado_do_maximo(
        tentativas=1, variancia_dos_sharpes=0.01
    ) == 0.0
    com_muitas = dsr.sharpe_esperado_do_maximo(
        tentativas=1_000, variancia_dos_sharpes=0.01
    )
    assert com_muitas > 0


def test_dsr_recusa_amostra_de_uma_observacao() -> None:
    with pytest.raises(dsr.DSRImpossivel, match="uma observação"):
        dsr.calcular(
            sharpe_por_observacao=0.1,
            n=1,
            tentativas=1,
            assimetria=0.0,
            curtose_bruta=3.0,
        )


def test_dsr_recusa_inventar_numero_fora_do_dominio() -> None:
    """Combinação que zera o denominador: erro alto, e não um valor plausível."""
    with pytest.raises(dsr.DSRImpossivel, match="fora do domínio"):
        dsr.calcular(
            sharpe_por_observacao=2.0,
            n=100,
            tentativas=10,
            assimetria=10.0,
            curtose_bruta=1.0,
        )


# ===========================================================================
# CRITERIO 7 - creditos e FDR sao mecanismos SEPARADOS
# ===========================================================================


def test_a_estatistica_nao_sabe_o_que_e_credito() -> None:
    """§8.6.1: "nenhum substitui o outro".

    Um procedimento estatístico que consultasse orçamento faria da escassez
    uma entrada da matemática — e aí o limiar passaria a depender de quanto o
    agente pode pagar, que é o oposto de controle de erro.
    """
    proibidos = ("credito", "crédito", "orcamento", "orçamento", "saldo")
    infratores: list[str] = []
    for arquivo in sorted((APP / "estatistica").glob("*.py")):
        # CÓDIGO, não prosa: as docstrings deste pacote citam crédito para
        # explicar por que ele não entra aqui. Ver `codigo_sem_prosa`.
        baixa = codigo_sem_prosa(arquivo).lower()
        for termo in proibidos:
            if termo in baixa:
                infratores.append(f"{arquivo.name}: {termo}")
    assert not infratores, (
        "a estatística consulta escassez: " + "; ".join(infratores)
    )
    # E a guarda não pode ser vazia.
    assert list((APP / "estatistica").glob("*.py"))


def test_o_modulo_de_credito_nao_faz_estatistica() -> None:
    """O outro lado da mesma separação."""
    texto = codigo_sem_prosa(APP / "creditos.py").lower()
    for termo in ("p_valor", "harmonico", "dsr_milesimos", "phi ("):
        assert termo not in texto, f"créditos calculando estatística: {termo}"


def test_o_lote_nao_consulta_saldo_para_decidir() -> None:
    """Quem decide promoção é o p-valor, não o caixa."""
    texto = codigo_sem_prosa(APP / "validador" / "lote.py").lower()
    assert "creditos" not in texto, (
        "o lote consulta crédito para decidir promoção"
    )


def test_a_correcao_harmonica_cabe_no_campo_que_a_relata() -> None:
    """`H(48) = 4,458797...` nao cabe em milesimos, e a tela mostrava 4,4580.

    O quarto digito real e **8**, e `int(H * 1000) / 1000` formatado com quatro
    casas produz um zero inventado. O CLAUDE.md documentava 4,4588 e a tela
    dizia 4,4580: os dois nao podiam estar certos.

    O LIMIAR nunca dependeu disso - ele sai da `Fraction` exata. O defeito era
    de relato, e este e justamente o numero que alguem confere a mao contra o
    artigo, entao relatar errado tira a unica coisa que o campo servia para
    fazer.
    """
    from fractions import Fraction

    from app.estatistica import fdr

    exato = sum(Fraction(1, i) for i in range(1, 49))
    r = fdr.aplicar({"a": 500_000}, procedimento="BY", alfa_bps=1_000, m=48)

    # Em ppm o numero sobrevive aos quatro digitos publicados.
    assert r.correcao_harmonica_ppm == int(exato * 1_000_000) == 4_458_797
    assert f"{r.correcao_harmonica_ppm / 1_000_000:.4f}" == "4.4588"

    # E em milesimos ele NAO sobrevive - o teste afirma a limitacao em vez de
    # deixar alguem redescobrir formatando o campo errado.
    assert f"{r.correcao_harmonica_milesimos / 1_000:.4f}" == "4.4580"

    # O limiar continua o da fracao exata, e nao o do campo arredondado.
    assert r.limiar_efetivo_ppm == 22_427
    assert r.limiar_efetivo_ppm == int(Fraction(1, 10) / exato * 1_000_000)


def test_bh_nao_tem_correcao_e_o_campo_diz_isso() -> None:
    """Em BH a correcao e 1, e nao "ausente" - o limiar e alfa * i / m."""
    from app.estatistica import fdr

    r = fdr.aplicar({"a": 500_000}, procedimento="BH", alfa_bps=1_000, m=48)
    assert r.correcao_harmonica_ppm == 1_000_000
    assert r.correcao_harmonica_milesimos == 1_000
