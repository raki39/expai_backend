"""Testes da D48: `n_minimo` contra o limiar de BY, com potencia declarada.

O que este arquivo tem de provar e diferente do de costume. Nao ha candidata
na 0C, entao nenhum destes numeros vai decidir nada hoje - e foi exatamente
por isso que o usuario mandou implementar agora, *"mesmo sem candidata"*: uma
regua construida no dia em que ha um resultado para olhar e uma regua escolhida
olhando o resultado.

Tres coisas aqui sao guardas contra NOS mesmos, e nao contra o codigo:

    a potencia nao pode ganhar um default de conveniencia
    os insumos nao podem ser ajustados para a hipotese caber
    o teto de Sharpe, a familia e o efeito minimo nao podem ser afrouxados

A quarta e a conclusao da propria D48, e ela tambem e teste: se um dia o
desenho passar a conseguir testar o efeito minimo, e um teste que avisa - nao
uma frase que continua impressa descrevendo outro mundo.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from app.estatistica import fdr
from app.hipotese import dimensionamento as dim
from app.hipotese import poder
from app.hipotese import registro as hipotese_registro
from app.hipotese.schema import (
    SHARPE_MAX_MILESIMOS,
    PreRegistroBruto,
)
from app.relatorio import viabilidade

QUINZE_MIN_MS = 900_000

# As janelas da D27 sobre os dois anos de barras de 15 minutos.
IN_SAMPLE_BARRAS = 21_024
DATASET_BARRAS = 70_080

# A dependencia MEDIDA no run 30: 11.163 barras brutas contra 11.023 efetivas.
RHO_MEDIDO_PPM = 6_300

# A volatilidade por barra, na faixa que a taxonomia da D40 congelou (cortes em
# 19,3 e 25,3 bps por barra). E insumo dos testes, e no relatorio ela e MEDIDA.
DESVIO_BPS = 22

# O que a hipotese 41 declarou: efeito minimo de US$ 500 de excesso sobre o B3
# (a clausula que disparou no run 30 foi `excesso_sobre_b3_cents < 50000`) e
# Sharpe esperado de 2,700, que e o que produz `n_minimo = 19.240` sob `t = 2`.
EFEITO_MINIMO_CENTS = 50_000
SHARPE_41_MILESIMOS = 2_700
CAPITAL_EXPOSTO_CENTS = 80_000  # US$ 1.000 de semente a 80% de fracao


def _insumos(**mudancas):
    base = dict(
        efeito_minimo_cents=EFEITO_MINIMO_CENTS,
        capital_exposto_cents=CAPITAL_EXPOSTO_CENTS,
        variancia_desvio_por_barra_bps=DESVIO_BPS,
        dependencia_rho_ppm=RHO_MEDIDO_PPM,
        horizonte_barras=IN_SAMPLE_BARRAS,
        potencia_ppm=dim.POTENCIA_ALVO_PPM,
        familia_m=48,
        fdr_alfa_bps=1_000,
    )
    base.update(mudancas)
    return base


# ---------------------------------------------------------------------------
# 1. A potencia e obrigatoria, e nao tem default
# ---------------------------------------------------------------------------


def test_dimensionar_sem_potencia_e_erro_de_chamada():
    """Sem o argumento, nem chega a rodar.

    A primeira porta: `potencia_ppm` e keyword-only sem default, entao a
    ausencia e `TypeError` no lugar de um numero silencioso.
    """
    insumos = _insumos()
    del insumos["potencia_ppm"]
    with pytest.raises(TypeError):
        dim.dimensionar(**insumos)  # type: ignore[arg-type]


def test_dimensionar_com_potencia_nula_recusa_com_o_motivo():
    """A segunda porta, e e a que um chamador distraido encontraria.

    Propagar um campo opcional que chegou vazio e o caminho mais provavel para
    a potencia voltar a ser implicita. `None` nao vira 50% nem 80%: recusa.
    """
    with pytest.raises(dim.PotenciaNaoDeclarada, match="nao tem valor padrao"):
        dim.dimensionar(**_insumos(potencia_ppm=None))


@pytest.mark.parametrize("potencia", [0, 1, 499_999, 1_000_000, 2_000_000])
def test_potencia_fora_da_faixa_util_recusa(potencia: int):
    """Abaixo de 50% o desenho espera errar mais do que acertar.

    Nao e conservadorismo: com potencia < 50% o `z_beta` fica negativo e o `t`
    exigido cai ABAIXO do limiar do proprio alfa - a amostra pedida seria menor
    que a que o controle de erro sozinho exige.
    """
    with pytest.raises(dim.PotenciaNaoDeclarada):
        dim.dimensionar(**_insumos(potencia_ppm=potencia))


def test_a_potencia_alvo_da_fase_e_oitenta_por_cento():
    """A politica, decidida pelo usuario em 2026-09-08.

    O teste existe para que mudar 80% seja uma decisao com nome e nao um ajuste
    de constante - e para que a frase do usuario fique ao lado do numero:

    > "Nao escolho 50% porque isso significaria que uma hipotese com exatamente
    > o efeito minimo verdadeiro teria aproximadamente a mesma chance de ser
    > detectada que um lancamento de moeda."
    """
    assert dim.POTENCIA_ALVO_PPM == 800_000


def test_a_politica_nao_e_o_default_da_maquina():
    """As duas coisas existem, e sao separadas de proposito.

    Se `dimensionar` caisse em `POTENCIA_ALVO_PPM` sozinho, a garantia
    "obrigatoria e sem default" seria letra morta: a chamada sem potencia
    passaria, e a decisao voltaria a ser implicita - que e o estado que a D48
    encontrou.
    """
    import inspect

    # ESTRUTURAL, e nao textual. A primeira versao desta guarda era
    # `"potencia_ppm" in fonte`, que e verdade sempre - guarda VAZIA, que
    # afirma protecao e nao protege, como `volume_gravavel` e `BLOCOS`.
    for funcao in (dim.dimensionar, dim.capacidade):
        param = inspect.signature(funcao).parameters["potencia_ppm"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{funcao.__name__}: a potencia tem de ser keyword-only, para que"
            " ninguem a passe por posicao sem perceber qual numero e"
        )
        assert param.default is inspect.Parameter.empty, (
            f"{funcao.__name__}: a potencia ganhou valor padrao"
            f" ({param.default!r}), e a garantia da D48 e que ela NAO tem"
        )


# ---------------------------------------------------------------------------
# 2. O limiar conservador: a PRIMEIRA rejeicao do BY
# ---------------------------------------------------------------------------


def test_o_limiar_da_primeira_rejeicao_do_by_e_467_ppm():
    """Conferido a mao, e nao contra o proprio resultado.

    `H(48) = 4,458797...`, `alfa = 0,10`:

        alfa / H(48)      = 0,0224275   -> 22.427 ppm (o limiar efetivo)
        alfa / (48 x H)   = 0,000467241 ->    467 ppm (a posicao 1)

    Os 467 ppm sao o numero que o Portao B da 0B publicou como "limiar da
    posicao 1", e e contra ele que a D48 manda dimensionar.
    """
    assert dim.alfa_primeira_rejeicao_ppm(
        procedimento="BY", m=48, alfa_bps=1_000
    ) == 467


def test_a_primeira_rejeicao_e_mais_apertada_que_o_limiar_efetivo():
    """E por isso que o usuario o chamou de conservador.

    O limiar efetivo (22.427 ppm) so seria alcancavel por uma hipotese que ja
    tivesse 47 companheiras rejeitadas abaixo dela. Dimensionar contra ele
    suporia o lote inteiro promovido para poder promover um - e o Portao B da
    0B promoveu **zero**.
    """
    efetivo = fdr.limiar_efetivo_ppm(procedimento="BY", m=48, alfa_bps=1_000)
    primeira = dim.alfa_primeira_rejeicao_ppm(
        procedimento="BY", m=48, alfa_bps=1_000
    )
    assert primeira < efetivo
    assert primeira * 48 <= efetivo + 48  # e a divisao por m, a menos do resto


def test_bh_na_primeira_posicao_nao_leva_a_correcao_harmonica():
    """BH e BY sao a mesma maquina, e a unica linha que os separa e H(m).

    Sem isto, um erro que aplicasse a correcao harmonica duas vezes (ou zero)
    passaria - e o limiar de dimensionamento e a coisa que decide se uma
    hipotese chega a rodar.
    """
    bh = dim.alfa_primeira_rejeicao_ppm(
        procedimento="BH", m=48, alfa_bps=1_000
    )
    assert bh == int(0.10 * 1_000_000 / 48)  # 2.083 ppm
    by = dim.alfa_primeira_rejeicao_ppm(
        procedimento="BY", m=48, alfa_bps=1_000
    )
    assert by < bh


def test_procedimento_desconhecido_recusa():
    with pytest.raises(ValueError, match="procedimento desconhecido"):
        dim.alfa_primeira_rejeicao_ppm(
            procedimento="BONFERRONI", m=48, alfa_bps=1_000
        )


# ---------------------------------------------------------------------------
# 3. `t = z_alfa + z_beta`, e a potencia implicita de hoje
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "potencia_ppm,t_esperado_milesimos",
    [
        (500_000, 3_310),  # z_beta = 0
        (800_000, 4_151),
        (900_000, 4_591),
        (950_000, 4_955),
    ],
)
def test_o_t_exigido_reproduz_a_tabela_apresentada_ao_usuario(
    potencia_ppm: int, t_esperado_milesimos: int
):
    """Os quatro valores que o usuario viu antes de escolher 80%.

    Conferido contra a tabela que a decisao usou, e nao contra a
    implementacao - o mesmo desenho do teste que confere `n_minimo` contra a
    tabela publicada de secao 8.3.
    """
    t = dim.t_exigido_micro(alfa_ppm=467, potencia_ppm=potencia_ppm)
    assert round(t / 1_000) == t_esperado_milesimos


def test_potencia_de_cinquenta_por_cento_e_z_beta_zero():
    """A demonstracao de que o `n_minimo` de hoje tem potencia implicita de 50%.

    Este e o achado que a D48 revelou e que nunca tinha sido dito: `t = 2` de
    secao 8.3 e a amostra em que o `t` **esperado** vale 2, e nada mais. Sem
    `z_beta`, metade das realizacoes cai abaixo do limiar.

    Com `z_beta = 0` o `t` exigido passa a ser o `z_alfa` puro - que e
    exatamente o formato da conta antiga, com outro alfa.
    """
    assert dim.z_de_potencia_micro(500_000) == 0
    assert dim.t_exigido_micro(
        alfa_ppm=467, potencia_ppm=500_000
    ) == dim.z_de_cauda_micro(467)


def test_a_amostra_cresce_com_o_quadrado_do_t():
    """Dobrar o `t` exigido quadruplica a amostra.

    E a razao aritmetica de o custo da D48 ser tao alto: de `t = 2` (secao 8.3)
    para `t = 4,15` (BY + 80%) o fator e `(4,15/2)^2 ~ 4,3`.
    """
    n_2 = dim._pelo_sharpe_declarado(
        sharpe_milesimos=SHARPE_41_MILESIMOS,
        duracao_barra_ms=QUINZE_MIN_MS,
        t_micro=2_000_000,
    )
    n_4 = dim._pelo_sharpe_declarado(
        sharpe_milesimos=SHARPE_41_MILESIMOS,
        duracao_barra_ms=QUINZE_MIN_MS,
        t_micro=4_000_000,
    )
    assert n_2 == 19_240  # o `n_minimo` gravado da hipotese 41
    assert abs(n_4 / n_2 - 4.0) < 0.001


def test_a_leitura_antiga_continua_calculada_e_visivel():
    """O padrao da D37: apagar a leitura anterior esconderia que houve correcao.

    82.891 e o `n_minimo` da hipotese 41 sob BY + 80% pela parametrizacao do
    Sharpe declarado. Ele fica ao lado do numero da regua nova, e as duas
    concordam no veredito - e e a concordancia que torna a conclusao robusta a
    qual das duas alguem prefira.
    """
    d = dim.dimensionar(
        **_insumos(),
        sharpe_esperado_milesimos=SHARPE_41_MILESIMOS,
        duracao_barra_ms=QUINZE_MIN_MS,
    )
    assert d.n_minimo_efetivo_pelo_sharpe == 82_891
    assert d.t_secao_8_3_micro == 2_000_000
    # As duas reguas discordam em magnitude e concordam no veredito.
    assert d.n_minimo_efetivo != d.n_minimo_efetivo_pelo_sharpe
    assert d.n_minimo_efetivo_pelo_sharpe > IN_SAMPLE_BARRAS
    assert d.veredito == dim.VEREDITO_NAO_TESTAVEL


def test_sem_o_sharpe_a_leitura_antiga_sai_None_e_nunca_zero():
    """`None` e "nao calculado"; zero seria uma conta que ninguem fez."""
    d = dim.dimensionar(**_insumos())
    assert d.n_minimo_efetivo_pelo_sharpe is None


# ---------------------------------------------------------------------------
# 4. n_efetivo, variancia e dependencia - e nao apenas barras brutas
# ---------------------------------------------------------------------------


def test_a_dependencia_converte_efetivas_em_brutas():
    """O que a conferencia de horizonte antiga nao fazia.

    `conferir_horizonte` comparava `n_minimo` (EFETIVO) contra
    `horizonte_barras` (BRUTO) - o que so vale se a autocorrelacao for zero.
    Nao e: o run 30 mediu 11.163 brutas contra 11.023 efetivas.

    Aqui a conversao e explicita e vai na direcao certa: com dependencia
    positiva, sao necessarias MAIS barras brutas que observacoes efetivas.
    """
    d = dim.dimensionar(**_insumos())
    assert d.n_bruto_necessario > d.n_minimo_efetivo
    assert d.fator_dependencia_ppm < 1_000_000


def test_sem_dependencia_bruto_e_efetivo_coincidem():
    d = dim.dimensionar(**_insumos(dependencia_rho_ppm=0))
    assert d.fator_dependencia_ppm == 1_000_000
    assert d.n_bruto_necessario == d.n_minimo_efetivo


def test_o_fator_de_dependencia_vem_de_UMA_definicao():
    """`poder.fator_ppm_de_rho`, e nao uma segunda copia da mesma conversao.

    A avaliacao desconta o observado por este fator; o dimensionamento converte
    o exigido pelo inverso dele. Duas copias divergiriam, e o resultado seria
    um `n_minimo` que nao casa com o `n_efetivo` que o mede - a forma exata de
    `condicoes_da_config` e do `SELECT MAX(run_id)`.
    """
    for rho in (0, 1_000, RHO_MEDIDO_PPM, 250_000, 900_000):
        d = dim.dimensionar(**_insumos(dependencia_rho_ppm=rho))
        assert d.fator_dependencia_ppm == poder.fator_ppm_de_rho(rho)


def test_dependencia_total_torna_o_dimensionamento_inexistente():
    """rho = 1 anula a amostra efetiva, e a resposta e recusar.

    Devolver um numero enorme afirmaria que existe uma quantidade de barras que
    resolve. Nao existe: com dependencia total, nenhuma barra acrescenta
    informacao.
    """
    with pytest.raises(dim.DimensionamentoImpossivel, match="anula a amostra"):
        dim.dimensionar(**_insumos(dependencia_rho_ppm=1_000_000))


def test_variancia_zero_recusa_em_vez_de_inventar():
    """Com desvio zero, qualquer efeito seria detectavel com uma observacao.

    O numero que sairia daqui seria ficcao, e e o mesmo motivo pelo qual a D45
    recusou fixar `sigma_e` por suposicao.
    """
    with pytest.raises(dim.DimensionamentoImpossivel, match="variancia"):
        dim.dimensionar(**_insumos(variancia_desvio_por_barra_bps=0))


def test_mais_variancia_exige_mais_amostra():
    """A variancia entra onde ela tem de entrar: padronizando o efeito."""
    baixa = dim.dimensionar(**_insumos(variancia_desvio_por_barra_bps=19))
    alta = dim.dimensionar(**_insumos(variancia_desvio_por_barra_bps=26))
    assert alta.n_minimo_efetivo > baixa.n_minimo_efetivo


def test_efeito_minimo_maior_exige_menos_amostra():
    """A direcao obvia, e ela precisa estar fixada.

    Um sinal invertido aqui faria hipoteses ambiciosas parecerem mais faceis de
    testar - e a triagem passaria a premiar exatamente quem promete mais.
    """
    modesto = dim.dimensionar(**_insumos(efeito_minimo_cents=10_000))
    ambicioso = dim.dimensionar(**_insumos(efeito_minimo_cents=200_000))
    assert ambicioso.n_minimo_efetivo < modesto.n_minimo_efetivo


def test_efeito_minimo_nulo_recusa():
    with pytest.raises(dim.DimensionamentoImpossivel, match="efeito minimo"):
        dim.dimensionar(**_insumos(efeito_minimo_cents=0))


# ---------------------------------------------------------------------------
# 5. O horizonte DECLARADO e o DISPONIVEL sao coisas diferentes
# ---------------------------------------------------------------------------


def test_mais_dado_disponivel_nao_pode_exigir_mais_amostra():
    """A armadilha que este desenho evita, e ela e sutil.

    O efeito minimo e um TOTAL declarado sobre um horizonte, entao a taxa por
    barra sai da divisao dos dois. Se o horizonte DISPONIVEL entrasse nessa
    divisao, perguntar "cabe no dataset inteiro?" diluiria o mesmo efeito por
    mais barras, derrubaria a taxa e exigiria MAIS amostra - a resposta
    pioraria justamente por haver mais dado.

    Medido: com o dataset inteiro disponivel, a exigencia e a MESMA e o deficit
    cai.
    """
    curto = dim.dimensionar(**_insumos())
    largo = dim.dimensionar(**_insumos(disponivel_barras=DATASET_BARRAS))
    assert largo.n_bruto_necessario == curto.n_bruto_necessario
    assert largo.deficit_barras < curto.deficit_barras
    assert largo.n_bruto_disponivel == DATASET_BARRAS


def test_nem_o_dataset_inteiro_alcanca_o_efeito_minimo_da_hipotese_41():
    """A conclusao da D48, com o horizonte mais generoso que existe.

    Nao e so o in-sample que nao cabe: as 70.080 barras dos dois anos inteiros
    tambem nao - e o dataset inteiro inclui a reserva selada, que uma hipotese
    nao pode usar. O deficit sobrevive ao melhor caso imaginavel.
    """
    d = dim.dimensionar(**_insumos(disponivel_barras=DATASET_BARRAS))
    assert d.veredito == dim.VEREDITO_NAO_TESTAVEL
    assert d.deficit_barras > 0


# ---------------------------------------------------------------------------
# 6. `nao_testavel_por_potencia`, com necessarias, disponiveis e deficit
# ---------------------------------------------------------------------------


def test_o_veredito_de_nao_caber_tem_nome_proprio():
    """E ele e diferente do `HorizonteInsuficiente` da secao 8.3.

    Dois motivos distintos com o mesmo nome deixariam "nao cabe sob `t = 2`" e
    "nao cabe sob BY + 80%" indistinguiveis - e sao diagnosticos diferentes,
    que pedem decisoes diferentes.
    """
    d = dim.dimensionar(**_insumos())
    assert d.veredito == "nao_testavel_por_potencia"
    assert not d.cabe


def test_o_motivo_informa_necessarias_disponiveis_e_deficit():
    """Os tres numeros que o usuario pediu, na frase que vai ao pre-registro.

    > "informar quantas observacoes seriam necessarias, quantas estao
    > disponiveis e o deficit."
    """
    d = dim.dimensionar(**_insumos())
    assert d.motivo is not None
    assert str(d.n_bruto_necessario) in d.motivo
    assert str(d.n_bruto_disponivel) in d.motivo
    assert str(d.deficit_barras) in d.motivo
    assert d.deficit_barras == d.n_bruto_necessario - d.n_bruto_disponivel


def test_quando_cabe_o_motivo_e_None_e_o_deficit_e_zero():
    """`None` porque nao ha condicao a descrever.

    O mesmo desenho do `CHECK` de `motivo_nao_testavel` na migracao 9: um
    motivo sobrando descreveria uma condicao que nao existe.
    """
    d = dim.dimensionar(
        **_insumos(efeito_minimo_cents=500_000, horizonte_barras=IN_SAMPLE_BARRAS)
    )
    assert d.cabe
    assert d.motivo is None
    assert d.deficit_barras == 0


# ---------------------------------------------------------------------------
# 7. Nada foi afrouxado para caber - e isto e a guarda contra nos
# ---------------------------------------------------------------------------


def test_os_insumos_voltam_intactos_dentro_do_resultado():
    """A forma verificavel de "nao afrouxar nada para caber".

    O usuario foi explicito:

    > "nao aumentar o teto de Sharpe, reduzir a familia ou alterar o efeito
    > minimo para faze-la caber"

    Se algum dia alguem ajustar um insumo no meio da conta para o veredito
    virar `testavel`, este teste quebra - porque o que entrou e o que voltou
    deixam de coincidir.
    """
    entrada = _insumos()
    d = dim.dimensionar(**entrada)
    saida = d.insumos.como_dict()
    for chave, valor in entrada.items():
        if chave == "horizonte_barras":
            assert saida["horizonte_declarado_barras"] == valor
            assert saida["horizonte_disponivel_barras"] == valor
            continue
        assert saida[chave] == valor, f"insumo {chave} foi alterado pela conta"


def test_o_teto_de_sharpe_do_schema_nao_foi_aumentado():
    """5,00, e o numero nao se move para uma hipotese caber.

    Sob BY + 80% o Sharpe exigido no in-sample passa de 5,00, entao a saida
    aritmeticamente mais facil seria subir o teto. O usuario nomeou isso como o
    perigo:

    > "O perigo seria reagir ao resultado aumentando o Sharpe maximo ou
    > afrouxando a estatistica so para o projeto continuar. Nao vamos fazer
    > isso."
    """
    assert SHARPE_MAX_MILESIMOS == 5_000


def test_a_familia_maxima_nao_foi_reduzida_para_afrouxar_o_limiar():
    """48, a D25. E reduzir a familia NAO e uma saida para o deficit.

    Eu tinha escrito que o ganho seria pequeno porque `H(m)` cresce
    logaritmicamente. **Estava errado**: o limiar da primeira rejeicao e
    `alfa / (m x H(m))`, com `m` no denominador direto - cortar a familia pela
    metade MAIS que dobra o limiar (467 -> 1.103 ppm).

    O ganho e pequeno por outro motivo, e ele e mais forte: `n` depende de
    `z^2`, e `z` cresce devagar quando `alfa` cresce. Medido:

    | familia | limiar | `t` | amostra relativa |
    |---|---|---|---|
    | 48 | 467 ppm | 4,151 | 1,000 |
    | 24 | 1.103 | 3,903 | 0,884 |
    | 12 | 2.685 | 3,626 | 0,763 |
    | **6** | 6.802 | 3,309 | **0,635** |

    Cortar a familia **oito vezes** compra 36% de amostra, contra um deficit de
    4,5 vezes. E por isso que "reduzir a familia" fica na lista de opcoes
    prospectivas com o custo escrito, e nao como a saida obvia.
    """
    from app.config.schema import ExperimentConfig

    assert ExperimentConfig().familia_max_hipoteses == 48

    def _n_relativo(m: int) -> float:
        alfa = dim.alfa_primeira_rejeicao_ppm(
            procedimento="BY", m=m, alfa_bps=1_000
        )
        t = dim.t_exigido_micro(alfa_ppm=alfa, potencia_ppm=800_000)
        return (t / dim.t_exigido_micro(alfa_ppm=467, potencia_ppm=800_000)) ** 2

    # O limiar sobe mais que o dobro ao cortar a familia pela metade...
    assert dim.alfa_primeira_rejeicao_ppm(
        procedimento="BY", m=24, alfa_bps=1_000
    ) > 2 * dim.alfa_primeira_rejeicao_ppm(
        procedimento="BY", m=48, alfa_bps=1_000
    )
    # ...e a amostra exigida cai pouco, porque ela depende de `z^2`.
    assert 0.87 < _n_relativo(24) < 0.90
    assert 0.62 < _n_relativo(6) < 0.65, (
        "cortar a familia oito vezes tem de continuar comprando pouca amostra:"
        " se este numero mudar, 'reduzir a familia' passou a ser uma saida real"
        " e a lista de opcoes prospectivas precisa ser relida"
    )


def test_o_modulo_de_dimensionamento_e_PURO():
    """Nao le banco e nao sabe em que fase estamos.

    Mesma guarda da quarentena e do monitor. Um dimensionamento que consultasse
    a fase teria um ramo por fase - e a D48 vale por REGUA GRAVADA NA LINHA,
    nao por data.
    """
    from tests._prosa import codigo_sem_prosa

    fonte = codigo_sem_prosa(pathlib.Path(dim.__file__))
    assert "sqlite3" not in fonte
    assert "app.fase" not in fonte and "from ..fase" not in fonte


# ---------------------------------------------------------------------------
# 8. A capacidade experimental: o que este desenho CONSEGUE testar
# ---------------------------------------------------------------------------


def _capacidade(barras: int):
    return dim.capacidade(
        barras_disponiveis=barras,
        dependencia_rho_ppm=RHO_MEDIDO_PPM,
        variancia_desvio_por_barra_bps=DESVIO_BPS,
        capital_exposto_cents=CAPITAL_EXPOSTO_CENTS,
        duracao_barra_ms=QUINZE_MIN_MS,
        potencia_ppm=dim.POTENCIA_ALVO_PPM,
        familia_m=48,
        fdr_alfa_bps=1_000,
    )


def test_o_menor_sharpe_detectavel_no_in_sample_passa_do_teto_do_schema():
    """**A conclusao da D48, em uma asserção.**

    Sob BY + 80%, o menor Sharpe anualizado detectavel nas 21.024 barras do
    in-sample fica **acima de 5,00** - que e o maximo que o schema aceita
    declarar. Entao nao existe hipotese declaravel que seja testavel ali: nao e
    que as candidatas sejam ruins, e que a regua e o horizonte sao
    incompativeis.

    Se um dia este teste falhar, alguma das tres coisas mudou - horizonte,
    variancia ou dependencia - e a conclusao da D48 precisa ser relida. E isso
    e melhor que uma frase, que sobreviveria intacta.
    """
    cap = _capacidade(IN_SAMPLE_BARRAS)
    assert cap.sharpe_anualizado_milesimos > SHARPE_MAX_MILESIMOS


def test_mais_horizonte_baixa_o_sharpe_exigido_e_sobe_o_efeito_em_centavos():
    """As duas direcoes, e elas nao se contradizem.

    O Sharpe exigido cai com `sqrt(n)` - e o numero comparavel entre
    horizontes. O efeito em centavos SOBE, porque ele e um total sobre a janela
    e a janela cresceu mais rapido do que o Sharpe caiu. Publicar so o segundo
    faria "mais dado piora" parecer verdade.
    """
    curto = _capacidade(IN_SAMPLE_BARRAS)
    longo = _capacidade(DATASET_BARRAS)
    assert longo.sharpe_anualizado_milesimos < curto.sharpe_anualizado_milesimos
    assert (
        longo.menor_efeito_detectavel_cents
        > curto.menor_efeito_detectavel_cents
    )


def test_o_menor_efeito_detectavel_no_in_sample_passa_do_capital_semente():
    """E a leitura economica do mesmo fato.

    Com 21.024 barras, o menor efeito que o desenho consegue distinguir de
    ruido e maior que os US$ 1.000 de capital semente - isto e, so uma
    estrategia que MAIS QUE DOBRASSE o capital seria detectavel. Nenhum efeito
    minimo honesto vive ali.
    """
    cap = _capacidade(IN_SAMPLE_BARRAS)
    assert cap.menor_efeito_detectavel_cents > 100_000


def test_a_capacidade_tambem_exige_a_potencia():
    with pytest.raises(dim.PotenciaNaoDeclarada):
        dim.capacidade(
            barras_disponiveis=IN_SAMPLE_BARRAS,
            dependencia_rho_ppm=0,
            variancia_desvio_por_barra_bps=DESVIO_BPS,
            capital_exposto_cents=CAPITAL_EXPOSTO_CENTS,
            duracao_barra_ms=QUINZE_MIN_MS,
            potencia_ppm=None,
            familia_m=48,
            fdr_alfa_bps=1_000,
        )


def test_a_capacidade_e_o_dimensionamento_sao_a_MESMA_conta_invertida():
    """Uma resolve o `n`, a outra resolve o efeito. Elas tem de fechar.

    Pega-se o menor efeito detectavel no horizonte, dimensiona-se contra ele, e
    o resultado tem de CABER - com folga de arredondamento, porque os dois
    lados arredondam na direcao conservadora.

    Sem este teste, as duas poderiam derivar e publicar numeros incoerentes na
    mesma tela, que e o defeito que este projeto pagou em `H(48) = 4,4580`.
    """
    cap = _capacidade(IN_SAMPLE_BARRAS)
    d = dim.dimensionar(
        **_insumos(
            efeito_minimo_cents=cap.menor_efeito_detectavel_cents,
            dependencia_rho_ppm=RHO_MEDIDO_PPM,
        )
    )
    assert d.cabe, (
        f"a inversao nao fecha: capacidade diz que"
        f" {cap.menor_efeito_detectavel_cents} centavos sao detectaveis em"
        f" {IN_SAMPLE_BARRAS} barras, e o dimensionamento pede"
        f" {d.n_bruto_necessario}"
    )


# ---------------------------------------------------------------------------
# 9. A regua fica na LINHA: a nao-retroatividade virando estrutura
# ---------------------------------------------------------------------------


@pytest.fixture
def evento(conn: sqlite3.Connection) -> tuple[int, int]:
    conn.execute(
        "INSERT INTO run (agent_id, state, config_version_id, created_at,"
        " updated_at) VALUES ('agent-0001','executando',1,'2026-09-08',"
        "'2026-09-08')"
    )
    run_id = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    conn.execute(
        "INSERT INTO agent_event (run_id, occurred_at, node, kind,"
        " cost_usd_minor, cost_usd_micro)"
        " VALUES (?, '2026-09-08', 'propor_regra', 'proposta', 0, 0)",
        (run_id,),
    )
    event_id = int(
        conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    )
    return run_id, event_id


CONDICOES = {
    "venue": "binance",
    "symbol": "BTCUSDT",
    "timeframe": "15m",
    "fidelity_level": 1,
}


def _bruto(**mudancas) -> PreRegistroBruto:
    base = {
        "enunciado": "uma abordagem qualquer, para exercitar a regua",
        "metrica_primaria": "excesso_sobre_b3_cents",
        "efeito_minimo": EFEITO_MINIMO_CENTS,
        "sharpe_esperado_milesimos": SHARPE_41_MILESIMOS,
        "criterio_parada": "n_minimo_alcancado",
        "condicoes_falseamento": [
            {
                "metrica": "excesso_sobre_b3_cents",
                "comparador": "menor_que",
                "valor": EFEITO_MINIMO_CENTS,
            }
        ],
    }
    base.update(mudancas)
    return PreRegistroBruto.model_validate(base)


def _registrar(conn, evento, dimensionamento_d48=None):
    run_id, event_id = evento
    return hipotese_registro.registrar(
        conn,
        run_id=run_id,
        agent_event_id=event_id,
        bruto=_bruto(),
        condicoes_validade=CONDICOES,
        duracao_barra_ms=QUINZE_MIN_MS,
        horizonte_barras=IN_SAMPLE_BARRAS,
        dimensionamento_d48=dimensionamento_d48,
    )


def test_sem_a_regua_nova_a_linha_diz_secao_8_3(conn, evento):
    """A verdade sobre toda linha que ja existe.

    Nenhuma das hipoteses da 0B conheceu potencia-alvo. Gravar `by_potencia`
    nelas seria afirmar uma conta que nao as dimensionou.
    """
    hid, testavel = _registrar(conn, evento)
    linha = hipotese_registro.por_id(conn, hid)
    assert linha is not None
    assert linha["regua_dimensionamento"] == dim.REGUA_SECAO_8_3
    assert linha["dimensionamento"] is None
    assert linha["n_minimo"] == 19_240
    assert testavel is True


def test_com_a_regua_nova_a_linha_diz_by_potencia_e_carrega_os_insumos(
    conn, evento
):
    """Os sete insumos ficam gravados, e nao apenas o `n_minimo` que saiu deles.

    Sem eles, `n_minimo = 95579` e um numero sem procedencia - e a D48 manda
    registrar procedimento, familia, FDR, efeito minimo, variancia, dependencia
    e potencia justamente para que a conta seja auditavel depois.
    """
    d = dim.dimensionar(**_insumos())
    hid, testavel = _registrar(conn, evento, dimensionamento_d48=d)
    linha = hipotese_registro.por_id(conn, hid)
    assert linha is not None
    assert linha["regua_dimensionamento"] == dim.REGUA_BY_POTENCIA
    assert linha["n_minimo"] == d.n_minimo_efetivo
    assert testavel is False
    assert "nao_testavel_por_potencia" in linha["motivo_nao_testavel"]

    insumos = linha["dimensionamento"]["insumos"]
    assert insumos["procedimento"] == "BY"
    assert insumos["familia_m"] == 48
    assert insumos["fdr_alfa_bps"] == 1_000
    assert insumos["efeito_minimo_cents"] == EFEITO_MINIMO_CENTS
    assert insumos["variancia_desvio_por_barra_bps"] == DESVIO_BPS
    assert insumos["dependencia_rho_ppm"] == RHO_MEDIDO_PPM
    assert insumos["potencia_ppm"] == 800_000


def test_hipotese_nova_com_potencia_diferente_de_oitenta_e_recusada(
    conn, evento
):
    """A potencia e politica da fase, e nao escolha por hipotese.

    Deixa-la variar por linha devolveria a regua movel que a D48 fechou: quem
    nao cabe a 80% escolheria 60% e caberia.
    """
    d = dim.dimensionar(**_insumos(potencia_ppm=600_000))
    with pytest.raises(dim.PotenciaNaoDeclarada, match="politica da fase"):
        _registrar(conn, evento, dimensionamento_d48=d)


def test_o_banco_recusa_regua_nova_sem_insumos(conn, evento):
    """SQL cru: a garantia mora no banco, e nao na disciplina do modulo.

    Um `by_potencia` sem `dimensionamento_json` seria a regua nova sem a
    procedencia que ela existe para exigir.
    """
    with pytest.raises(sqlite3.IntegrityError, match="andam juntos"):
        _inserir_cru(
            conn, evento, regua_dimensionamento="by_potencia",
            dimensionamento_json=None,
        )


def test_o_banco_recusa_regua_antiga_COM_insumos(conn, evento):
    """A porta do outro lado, e ela e a menos obvia.

    Uma linha `secao_8_3` carregando insumos da D48 afirmaria uma conta que nao
    dimensionou aquela hipotese - e alguem lendo o JSON concluiria que ela
    passou pela regua nova.
    """
    with pytest.raises(sqlite3.IntegrityError, match="andam juntos"):
        _inserir_cru(
            conn, evento, regua_dimensionamento="secao_8_3",
            dimensionamento_json='{"insumos": {}}',
        )


def test_o_banco_recusa_regua_desconhecida(conn, evento):
    with pytest.raises(sqlite3.IntegrityError):
        _inserir_cru(
            conn, evento, regua_dimensionamento="a_que_eu_inventei",
            dimensionamento_json=None,
        )


def test_a_regua_de_uma_hipotese_gravada_nao_pode_ser_trocada(conn, evento):
    """A nao-retroatividade da D48, e ela ja estava imposta desde a migracao 9.

    > "A mudanca da regua vale somente para hipoteses futuras."

    O gatilho que a garante nao e novo: `hypothesis_sem_update` recusa qualquer
    `UPDATE` na tabela. Este teste amarra a garantia da D48 aquela estrutura em
    vez de duplicar o gatilho - duas travas do mesmo fato divergem, e este
    projeto ja pagou por isso.
    """
    hid, _ = _registrar(conn, evento)
    with pytest.raises(sqlite3.IntegrityError, match="imutavel"):
        conn.execute(
            "UPDATE hypothesis SET regua_dimensionamento = 'by_potencia'"
            " WHERE id = ?",
            (hid,),
        )


def _inserir_cru(conn: sqlite3.Connection, evento, **campos) -> None:
    """INSERT direto, sem passar pelo modulo. E o ponto do teste."""
    import json as _json

    run_id, event_id = evento
    linha = {
        "run_id": run_id,
        "agent_event_id": event_id,
        "enunciado": "uma afirmacao qualquer",
        "agente_origem": "transacao@0b",
        "timestamp_registro": "2026-09-08T00:00:00+00:00",
        "metrica_primaria": "excesso_sobre_b3_cents",
        "efeito_minimo": 0,
        "n_minimo": 100,
        "sharpe_esperado_milesimos": 3_000,
        "criterio_parada": "fim_da_janela",
        "condicoes_validade_json": _json.dumps(CONDICOES),
        "condicoes_falseamento_json": _json.dumps(
            [
                {
                    "metrica": "excesso_sobre_b3_cents",
                    "comparador": "menor_que",
                    "valor": 0,
                }
            ]
        ),
        "testavel": 1,
        "motivo_nao_testavel": None,
        "horizonte_barras": 50_000,
        "rule_id": None,
        "supersedes": None,
        "content_hash": "abc123",
        **campos,
    }
    colunas = ", ".join(linha)
    marcas = ", ".join("?" for _ in linha)
    conn.execute(
        f"INSERT INTO hypothesis ({colunas}) VALUES ({marcas})",
        tuple(linha.values()),
    )


# ---------------------------------------------------------------------------
# 10. O relatorio de viabilidade, e a conclusao DERIVADA
# ---------------------------------------------------------------------------


def test_a_conclusao_da_d48_esta_registrada_no_texto_do_usuario():
    """A frase que o usuario mandou registrar explicitamente.

    Ela fica numa constante e vai para a resposta da rota, ao lado dos numeros
    que a produzem - uma conclusao sem os numeros ao lado e a coisa que este
    projeto mais tem registro de ver envelhecer.
    """
    assert "familia maxima atual" in viabilidade.CONCLUSAO
    assert "potencia de 80%" in viabilidade.CONCLUSAO
    assert "nao consegue testar o efeito minimo" in viabilidade.CONCLUSAO


def test_a_conclusao_e_derivada_e_nao_digitada():
    """`conclusao_sustentada` vira `False` sozinho quando deixar de ser verdade.

    Mesmo desenho do `fecha` do relatorio da 0A: doze booleanos vindos de
    consulta, e nao uma frase. Aqui a prova e pelo caminho contrario - uma
    hipotese que CABE derruba a sustentacao.
    """
    assert viabilidade._sustenta([], [{"veredito": "nao_testavel_por_potencia"}])
    assert not viabilidade._sustenta([], [{"veredito": "testavel"}])
    assert not viabilidade._sustenta(
        [],
        [
            {"veredito": "nao_testavel_por_potencia"},
            {"veredito": "testavel"},
        ],
    )
    # Sem hipotese nenhuma, nao ha o que sustentar - e afirmar a conclusao
    # sobre um banco vazio seria afirmar sem medir.
    assert not viabilidade._sustenta([], [])


def test_as_quatro_opcoes_prospectivas_estao_listadas_e_NENHUMA_escolhida():
    """> "Nenhuma dessas opcoes deve ser escolhida agora olhando resultados."

    E nao ha campo `escolhida`. Um campo com valor `None` seria um convite a
    preenche-lo na proxima vez que o numero incomodasse - a quinta pergunta do
    teste de escopo responde "sim" a isso.
    """
    opcoes = viabilidade.OPCOES_PROSPECTIVAS
    assert len(opcoes) == 4
    for o in opcoes:
        assert set(o) == {"opcao", "custo"}, (
            f"a opcao {o.get('opcao')!r} ganhou campo alem de opcao e custo:"
            " escolher agora e escolher olhando o resultado"
        )
    texto = " ".join(o["opcao"] for o in opcoes)
    assert "coletar mais dados" in texto
    assert "familias" in texto
    assert "efeito minimo maior" in texto
    assert "procedimento estatistico valido" in texto


def test_os_tres_atalhos_proibidos_estao_nomeados():
    """O usuario nomeou o perigo, e o nome fica no codigo.

    > "O perigo seria reagir ao resultado aumentando o Sharpe maximo ou
    > afrouxando a estatistica so para o projeto continuar."
    """
    fora = " ".join(viabilidade.FORA_DE_COGITACAO)
    assert "teto de Sharpe" in fora
    assert "reduzir a familia" in fora
    assert "efeito minimo de hipotese ja registrada" in fora


def test_a_rota_de_viabilidade_recusa_sem_dataset(client):
    """Sem dataset nao ha variancia medida, e o relatorio diz isso.

    `disponivel: false` com o motivo, e nao um dimensionamento sobre numeros
    inventados - que e o que a D45 recusou ao nao fixar `sigma_e`.
    """
    r = client.get("/api/relatorio/viabilidade")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["disponivel"] is False
    assert corpo["conclusao"] is None
    assert corpo["motivo"]


def test_o_relatorio_passa_a_potencia_explicitamente(client):
    """A rota nao pode herdar a potencia por default do modulo.

    O relatorio que publica a conclusao e o lugar mais provavel para o numero
    voltar a ser implicito - e a D48 existe justamente porque ele era.
    """
    import re

    import app.api.rotas.relatorio as rota

    from tests._prosa import codigo_sem_prosa

    # `codigo_sem_prosa` junta TOKENS com espaco, entao
    # `dimensionamento.POTENCIA_ALVO_PPM` sai `dimensionamento . POTENCIA_...`.
    # Procurar a substring literal daria uma guarda VAZIA que passa com o
    # defeito presente - foi assim que a guarda do `app.state.conn` mentiu no
    # incremento 17, e o regex e a correcao que ficou de lá.
    fonte = codigo_sem_prosa(pathlib.Path(rota.__file__))
    padrao = r"potencia_ppm\s*=\s*dimensionamento\s*\.\s*POTENCIA_ALVO_PPM"
    assert re.search(padrao, fonte), (
        "a rota de viabilidade tem de passar a potencia EXPLICITAMENTE"
    )
    # E a guarda nao e vazia: ela encontra o que procura no codigo bom.
    assert re.search(padrao, "potencia_ppm = dimensionamento . POTENCIA_ALVO_PPM")


def test_a_variancia_medida_arredonda_para_baixo():
    """O arredondamento que erra CONTRA a nossa conclusao.

    Um desvio menor faz o efeito parecer mais facil de detectar, entao truncar
    para baixo e o unico arredondamento que se pode fazer de graca aqui: ele
    empurra na direcao de dizer que o desenho consegue testar.
    """
    assert viabilidade._desvio_por_barra_bps([0, 100, -100, 50, -50]) == 70
    # Serie curta demais devolve zero, que quem chama trata como NAO MEDIDO.
    assert viabilidade._desvio_por_barra_bps([]) == 0
    assert viabilidade._desvio_por_barra_bps([42]) == 0
