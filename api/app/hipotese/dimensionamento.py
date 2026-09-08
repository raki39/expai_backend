"""`n_minimo` dimensionado contra o limiar de BY, com potencia declarada (D48).

> *"Dimensione `n_minimo` usando o limiar conservador da primeira rejeicao do
> BY, com procedimento, tamanho da familia, FDR, efeito minimo, variancia,
> dependencia e potencia estatistica registrados."* - usuario, 2026-09-08

## O que estava errado, e nao era um erro de conta

O incremento 13 mediu a divergencia e o incremento 20 a herdou: `n_minimo` sai
de `t = 2` (secao 8.3), enquanto BY na primeira posicao exige `t = 3,31`. Uma
hipotese que alcance exatamente o `n_minimo` que declarou tem p ~ 0,023 -
cinquenta vezes o limiar de 467 ppm que a promoveria.

**E ao derivar isso apareceu o que nunca tinha sido dito:** `t = 2` resolve a
amostra em que o `t` ESPERADO vale 2. Metade das realizacoes cai abaixo. O
`n_minimo` de hoje tem **potencia implicita de ~50%** - uma hipotese com
exatamente o efeito que declarou seria detectada com a chance de uma moeda.

`t = z_alfa + z_beta` e a conta inteira, e ela so existe quando alguem diz qual
e o `beta`. A secao 8.3 nao diz: ela da limiar sobre a estatistica OBSERVADA,
que e outra coisa. **Por isso a potencia e parametro obrigatorio sem default** -
sem o numero, esta maquina recusa. Mesmo desenho do ADR 0027, que declarou
formula, alvo e piloto e deixou o numero sair deles.

**A potencia-alvo e 80%**, decidida pelo usuario em 2026-09-08, e a politica
esta em `POTENCIA_ALVO_PPM`. A constante e a POLITICA; a funcao continua
exigindo o argumento. Sao coisas diferentes, e junta-las devolveria o default
que a decisao proibiu.

## As duas leituras do efeito, e por que a regua e o MINIMO

O pre-registro carrega dois numeros que falam do mesmo efeito por lados
diferentes:

| campo | o que e |
|---|---|
| `sharpe_esperado` | o que a hipotese ESPERA - otimismo declarado |
| `efeito_minimo` | o menor efeito que ainda IMPORTA economicamente |

Dimensionar pelo primeiro e o que o codigo fazia. Ele produz uma amostra capaz
de detectar o efeito que se torce para existir, e incapaz de descartar o menor
efeito que valeria a pena - que e exatamente o que separa `refutada` de
`inconclusiva` na secao 14.4. **Um teste dimensionado contra o esperado nao
consegue rejeitar o minimo, e mesmo assim escreve `refutada`.**

Entao a regua da D48 e o `efeito_minimo`, padronizado pela **variancia** - que
e o insumo que converte centavos em efeito comparavel com ruido. A leitura
antiga continua **calculada e visivel** (`n_minimo_efetivo_pelo_sharpe`), pelo
mesmo motivo que a D37 manteve a leitura da D29 na tela: apaga-la esconderia
que houve correcao.

## A base de normalizacao NAO e exposicao real

`base_de_normalizacao_cents` converte centavos em fracao, e **nada mais**. Ela
se chamava `capital_exposto_cents`, e o nome mentia: a exposicao verdadeira
varia barra a barra - a regra fica fora do mercado parte do tempo, e quando esta
dentro aplica `fracao_bps` sobre o caixa do momento.

O que o relatorio usa e `seed_capital_usd_cents`: fixo, declarado, o mesmo em
todos os horizontes da tabela. Ele **nao afirma** que a estrategia expos isso.

**E a base cancela no numero que compara horizontes.** O Sharpe anualizado nao
depende dela - ela multiplica media e desvio igualmente. Ela so aparece quando a
conta volta para centavos, e e por isso que trocar a base muda a coluna de
dolares e nao muda o veredito.

## A dependencia entra onde ela morde

`n_minimo` sempre foi expresso em observacoes EFETIVAS, e a conferencia de
horizonte comparava esse numero contra barras BRUTAS - o que so vale se a
autocorrelacao for zero. Nao e: o run 30 mediu 11.163 brutas contra 11.023
efetivas.

Aqui a conversao e explicita: `n_bruto_necessario = n_efetivo / fator`, com o
fator vindo do MESMO lugar que `hipotese.poder` usa na avaliacao. Duas
travessias do mesmo formato divergem, e este projeto ja registrou o que isso
custa.

## Onde esta maquina PARA

Ela nao afrouxa nada para caber. Nao mexe no teto de Sharpe, no tamanho da
familia nem no efeito minimo - os insumos voltam intactos dentro do resultado,
e ha teste comparando o que entrou com o que saiu. Quando nao cabe, o veredito
e `nao_testavel_por_potencia`, com necessarias, disponiveis e deficit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from statistics import NormalDist

from ..estatistica.fdr import PROCEDIMENTOS, harmonico
from . import poder

_NORMAL = NormalDist()

#: A politica para hipoteses NOVAS, decidida pelo usuario em 2026-09-08.
#:
#: > "Nao escolho 50% porque isso significaria que uma hipotese com exatamente
#: > o efeito minimo verdadeiro teria aproximadamente a mesma chance de ser
#: > detectada que um lancamento de moeda."
#:
#: E politica, e nao default: `dimensionar` continua exigindo o argumento.
POTENCIA_ALVO_PPM = 800_000

#: Abaixo de 50% a conta continua valendo e o desenho para de fazer sentido:
#: `z_beta` fica negativo, e o experimento passa a esperar errar mais vezes do
#: que acertar quando a hipotese e verdadeira.
POTENCIA_MINIMA_PPM = 500_000
POTENCIA_MAXIMA_PPM = 999_999

VEREDITO_TESTAVEL = "testavel"
VEREDITO_NAO_TESTAVEL = "nao_testavel_por_potencia"

#: A regua que dimensionou cada hipotese, gravada linha a linha.
#:
#: **Isto e a nao-retroatividade virando estrutura.** A D48 vale so para
#: hipoteses futuras; sem este campo, `n_minimo` significaria duas coisas
#: diferentes em linhas vizinhas e nada anunciaria a mudanca - que e o padrao
#: que este projeto conta vinte e quatro vezes.
REGUA_SECAO_8_3 = "secao_8_3"
REGUA_BY_POTENCIA = "by_potencia"
REGUAS = (REGUA_SECAO_8_3, REGUA_BY_POTENCIA)


#: A decisao prospectiva de capacidade experimental. **AINDA NAO TOMADA.**
#:
#: A D48 mediu que este desenho nao consegue testar o efeito minimo no
#: horizonte disponivel, e isso abriu uma decisao com quatro saidas: coletar
#: mais dados, reduzir previamente o tamanho das proximas familias, exigir
#: efeito minimo maior, ou adotar outro procedimento estatistico valido.
#:
#: > "A decisao de capacidade experimental fica como bloqueante absoluto antes
#: > de qualquer nova hipotese, nao como bloqueante da construcao do relatorio
#: > da 0C." - o usuario, 2026-09-08
#:
#: **`None` bloqueia hipotese nova, e nao bloqueia o relatorio.** A distincao e
#: exata: relatar o que se mediu nao decide nada, e registrar uma hipotese nova
#: sob uma regua cuja capacidade ninguem decidiu e comecar a gastar horizonte
#: contra uma pergunta que ja se sabe nao respondivel.
#:
#: E ela e CONSTANTE DE CODIGO, e nao campo de config, de proposito: virar este
#: valor exige commit e ADR. Um campo de config poderia ser trocado em produção
#: por quem quisesse desbloquear, e a decisao e do usuario.
DECISAO_DE_CAPACIDADE_EXPERIMENTAL: str | None = None

#: As quatro saidas, e **nenhuma escolhida**. Lista fechada: acrescentar uma
#: quinta e decisao, e nao conveniencia.
SAIDAS_DE_CAPACIDADE = (
    "coletar_mais_dados",
    "reduzir_familias_futuras",
    "exigir_efeito_minimo_maior",
    "outro_procedimento_valido",
)


class CapacidadeNaoDecidida(Exception):
    """Hipotese nova sob a regua da D48 antes da decisao de capacidade.

    Bloqueante ABSOLUTO, e nao aviso. A D48 provou que o horizonte disponivel
    nao alcanca o efeito minimo; registrar hipotese nova antes de decidir o que
    fazer sobre isso e gastar horizonte contra uma pergunta cuja resposta ja se
    conhece.
    """


class PotenciaNaoDeclarada(ValueError):
    """A potencia-alvo nao foi informada, ou esta fora da faixa util."""


class DimensionamentoImpossivel(ValueError):
    """Um insumo torna a conta indefinida, e inventar um valor seria arbitrio."""


# ---------------------------------------------------------------------------
# alfa: o limiar da PRIMEIRA rejeicao, que e o conservador
# ---------------------------------------------------------------------------


def alfa_primeira_rejeicao_ppm(
    *, procedimento: str, m: int, alfa_bps: int
) -> int:
    """O limiar que a hipotese de MENOR p-valor precisa vencer, em ppm.

    BH e BY comparam `p(k)` contra `(k/m) * alfa`, com `alfa` ja dividido por
    `H(m)` no caso de BY. Na posicao 1 isso e `alfa / (m * H(m))` - e e o
    limiar mais apertado do lote.

    **Por que a primeira posicao, e nao o limiar efetivo.** O limiar efetivo
    (`alfa/H(m)`, 22.427 ppm com m = 48) so seria alcancavel por uma hipotese
    que ja tivesse 47 companheiras rejeitadas abaixo dela. Dimensionar contra
    ele suporia o lote inteiro promovido para poder promover um - e o Portao B
    da 0B promoveu zero. A primeira rejeicao e o unico limiar que uma hipotese
    sozinha consegue enfrentar, e por isso o usuario o chamou de conservador.

    Truncado para BAIXO, como `fdr.limiar_efetivo_ppm`: um alfa maior que o
    exato promoveria mais que o procedimento autoriza, e aqui tambem pediria
    menos amostra.
    """
    if procedimento not in PROCEDIMENTOS:
        raise ValueError(f"procedimento desconhecido: {procedimento!r}")
    if m < 1:
        raise ValueError("a familia precisa ter ao menos uma hipotese")
    if alfa_bps <= 0:
        raise ValueError("o FDR alvo precisa ser positivo")
    alfa = Fraction(alfa_bps, 10_000)
    if procedimento == "BY":
        alfa = alfa / harmonico(m)
    return int(alfa * 1_000_000 / m)  # trunca: para baixo


# ---------------------------------------------------------------------------
# z: os dois quantis que somam o t exigido
# ---------------------------------------------------------------------------


def z_de_cauda_micro(p_ppm: int) -> int:
    """`Phi^-1(1 - p)` em micro, arredondado para CIMA.

    Micro, e nao float, porque este numero vai para um pre-registro imutavel
    que precisa ser reproduzivel entre maquinas (R12) - o mesmo motivo pelo
    qual `poder` trabalha em milesimos. Seis casas sao grossas o bastante para
    que a diferenca de ultimo bit de um `float` nunca alcance o valor gravado.
    """
    if not 0 < p_ppm < 1_000_000:
        raise ValueError(f"cauda precisa estar entre 0 e 1; veio {p_ppm} ppm")
    return math.ceil(_NORMAL.inv_cdf(1 - p_ppm / 1_000_000) * 1_000_000)


def z_de_potencia_micro(potencia_ppm: int) -> int:
    """`Phi^-1(potencia)` em micro, arredondado para CIMA.

    Este e o `z_beta` da conta classica de tamanho de amostra. Ele **nao
    existia** neste projeto: `t = 2` de secao 8.3 e so o lado do alfa, e usar
    apenas ele equivale a `z_beta = 0`, isto e, potencia de 50%.
    """
    exigir_potencia(potencia_ppm)
    return math.ceil(_NORMAL.inv_cdf(potencia_ppm / 1_000_000) * 1_000_000)


def exigir_decisao_de_capacidade() -> None:
    """Recusa hipotese nova enquanto a capacidade experimental nao for decidida.

    Chamada de `registro.registrar`, e so no caminho da regua nova: as linhas
    da 0B nasceram sob a secao 8.3 e nao dependem desta decisao.
    """
    if DECISAO_DE_CAPACIDADE_EXPERIMENTAL is None:
        raise CapacidadeNaoDecidida(
            "hipotese nova esta BLOQUEADA: a D48 mediu que o horizonte"
            " disponivel nao alcanca o efeito minimo, e a decisao prospectiva"
            " de capacidade experimental nao foi tomada. As quatro saidas sao"
            f" {', '.join(SAIDAS_DE_CAPACIDADE)}, e nenhuma foi escolhida -"
            " escolher agora, olhando o resultado que acabou de sair, e o que"
            " a secao 8.2 existe para impedir. O relatorio da 0C NAO depende"
            " desta decisao e segue sendo construido"
        )
    if DECISAO_DE_CAPACIDADE_EXPERIMENTAL not in SAIDAS_DE_CAPACIDADE:
        raise CapacidadeNaoDecidida(
            f"saida de capacidade desconhecida:"
            f" {DECISAO_DE_CAPACIDADE_EXPERIMENTAL!r}. A lista e fechada"
            f" ({', '.join(SAIDAS_DE_CAPACIDADE)}); acrescentar uma quinta e"
            " decisao com ADR, e nao conveniencia"
        )


def exigir_potencia(potencia_ppm: int | None) -> int:
    """Recusa a ausencia e a faixa degenerada. Nao ha valor padrao aqui."""
    if potencia_ppm is None:
        raise PotenciaNaoDeclarada(
            "a potencia-alvo e obrigatoria e nao tem valor padrao (D48): sem"
            " ela, `t = z_alfa + z_beta` fica com uma parcela por escolher, e"
            " escolhe-la depois seria fixar a regua olhando o resultado"
        )
    if not POTENCIA_MINIMA_PPM <= potencia_ppm <= POTENCIA_MAXIMA_PPM:
        raise PotenciaNaoDeclarada(
            f"potencia de {potencia_ppm} ppm fora da faixa util"
            f" [{POTENCIA_MINIMA_PPM}, {POTENCIA_MAXIMA_PPM}]: abaixo de 50% o"
            " desenho espera errar mais vezes do que acertar quando a hipotese"
            " e verdadeira"
        )
    return potencia_ppm


def t_exigido_micro(*, alfa_ppm: int, potencia_ppm: int) -> int:
    """`t = z_alfa + z_beta`, em micro.

    Com o limiar de BY na primeira posicao (467 ppm para m = 48, FDR 10%):

    | potencia | `z_beta` | `t` |
    |---|---|---|
    | 50% | 0,00 | 3,31 |
    | **80%** | **0,84** | **4,15** |
    | 90% | 1,28 | 4,59 |
    | 95% | 1,64 | 4,95 |

    E `n` cresce com `t^2`: de `t = 2` para `t = 4,15` a amostra exigida
    quadruplica.
    """
    return z_de_cauda_micro(alfa_ppm) + z_de_potencia_micro(potencia_ppm)


# ---------------------------------------------------------------------------
# os insumos, e o resultado
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Insumos:
    """Os sete insumos que a D48 manda registrar, mais o horizonte disponivel.

    Guardados dentro do resultado **intactos**. Nao e redundancia: e a forma
    verificavel de "nao afrouxar nada para caber" - ha teste comparando o que
    entrou com o que voltou, e ele quebra se algum dia alguem ajustar um insumo
    no meio da conta para o veredito virar `testavel`.
    """

    procedimento: str
    familia_m: int
    fdr_alfa_bps: int
    efeito_minimo_cents: int
    variancia_desvio_por_barra_bps: int
    dependencia_rho_ppm: int
    potencia_ppm: int
    #: **BASE DE NORMALIZACAO, e nao exposicao real.** Ela existe para converter
    #: centavos em fracao, e nada mais.
    #:
    #: O usuario mandou declarar qual das duas coisas ela e, e a resposta e a
    #: primeira. O nome anterior era `capital_exposto_cents`, e ele MENTIA: a
    #: exposicao verdadeira varia barra a barra - a regra fica fora do mercado
    #: parte do tempo (no run 30, metade da janela), e quando esta dentro aplica
    #: `fracao_bps` sobre o caixa do momento, que muda com o resultado.
    #:
    #: O relatorio usa `seed_capital_usd_cents` como base: um numero fixo,
    #: declarado, igual para todos os horizontes da tabela. **Ele nao afirma que
    #: a estrategia expos isso** - afirma que o efeito minimo esta sendo lido
    #: como fracao desse valor.
    #:
    #: E a base **cancela** no numero que compara horizontes: o Sharpe
    #: anualizado nao depende dela, porque ela multiplica media e desvio
    #: igualmente. Ela so aparece na conversao para centavos.
    base_de_normalizacao_cents: int
    #: O horizonte sobre o qual o efeito minimo foi DECLARADO. Ele define a
    #: taxa por barra, e nao a disponibilidade.
    horizonte_declarado_barras: int
    #: Quantas barras existem de fato. Por padrao e o mesmo numero, e separa-lo
    #: e o que permite perguntar "cabe no dataset inteiro?" sem mudar o efeito.
    horizonte_disponivel_barras: int

    def como_dict(self) -> dict:
        return {
            "procedimento": self.procedimento,
            "familia_m": self.familia_m,
            "fdr_alfa_bps": self.fdr_alfa_bps,
            "efeito_minimo_cents": self.efeito_minimo_cents,
            "variancia_desvio_por_barra_bps": (
                self.variancia_desvio_por_barra_bps
            ),
            "dependencia_rho_ppm": self.dependencia_rho_ppm,
            "potencia_ppm": self.potencia_ppm,
            "base_de_normalizacao_cents": self.base_de_normalizacao_cents,
            "horizonte_declarado_barras": self.horizonte_declarado_barras,
            "horizonte_disponivel_barras": self.horizonte_disponivel_barras,
        }


@dataclass(frozen=True)
class Dimensionamento:
    """O que a conta produziu, com cada degrau visivel.

    O numero sozinho seria impossivel de auditar - e este e o numero que decide
    se uma hipotese chega a rodar.
    """

    insumos: Insumos
    regua: str
    alfa_primeira_rejeicao_ppm: int
    z_alfa_micro: int
    z_beta_micro: int
    t_exigido_micro: int
    efeito_por_barra_bps_micro: int
    sharpe_por_barra_micro: int
    n_minimo_efetivo: int
    fator_dependencia_ppm: int
    n_bruto_necessario: int
    n_bruto_disponivel: int
    deficit_barras: int
    veredito: str
    #: A leitura ANTIGA, calculada e visivel (padrao da D37). Dimensiona pelo
    #: Sharpe declarado: e o numero da tabela que o usuario ja viu (82.904 para
    #: a hipotese 41 sob BY + 80%).
    n_minimo_efetivo_pelo_sharpe: int | None
    t_secao_8_3_micro: int

    @property
    def cabe(self) -> bool:
        return self.veredito == VEREDITO_TESTAVEL

    @property
    def taxa_por_barra_bps_micro(self) -> int:
        """A TAXA que o efeito minimo implica. E ela, e nao o total, que se detecta.

        Nome proprio porque o total engana. `efeito_minimo` e um total
        declarado **sobre um horizonte**, e a conta de amostra detecta a taxa
        que os dois implicam. Confundir os dois faz "US$ 500" parecer uma
        constante quando ele esta sendo escalado com a janela.
        """
        return self.efeito_por_barra_bps_micro

    @property
    def efeito_acumulado_no_cruzamento_cents(self) -> int:
        """O que a MESMA taxa acumula ao longo das barras exigidas.

        **Este e o numero que faltava, e a ausencia dele era um defeito de
        apresentacao.** Dizer "detectar o efeito minimo de 50.000 centavos
        exige 95.579 barras" le como se 50.000 centavos fossem detectaveis
        depois de 95.579 barras. Nao sao: naquele ponto a mesma taxa acumulou
        **227.309** centavos, e e esse valor que iguala o minimo detectavel.

        O que se detecta e a TAXA. O total cresce com a janela em que ela corre,
        e o cruzamento acontece quando o acumulado alcanca o detectavel - por
        construcao, porque `n_bruto_necessario` e definido como o `n` em que a
        taxa detectavel iguala a declarada.
        """
        return (
            self.efeito_por_barra_bps_micro
            * self.insumos.base_de_normalizacao_cents
            * self.n_bruto_necessario
            // (1_000_000 * 10_000)
        )

    @property
    def motivo(self) -> str | None:
        """A frase que vai para `motivo_nao_testavel`, ou `None` se cabe.

        Ela fala da TAXA de proposito. A versao anterior falava do total, e
        deixava o efeito minimo parecer um valor constante que bastaria esperar
        para medir.
        """
        if self.cabe:
            return None
        return (
            "nao_testavel_por_potencia: o efeito minimo de"
            f" {self.insumos.efeito_minimo_cents} centavos sobre"
            f" {self.insumos.horizonte_declarado_barras} barras implica a taxa"
            f" de {self.taxa_por_barra_bps_micro / 1_000_000:.6f} bps por"
            " barra; detectar ESSA TAXA com potencia de"
            f" {self.insumos.potencia_ppm / 10_000:.1f}% contra o limiar de"
            f" {self.alfa_primeira_rejeicao_ppm} ppm da primeira rejeicao do"
            f" {self.insumos.procedimento} exige {self.n_bruto_necessario}"
            f" barras; ha {self.n_bruto_disponivel}. Deficit de"
            f" {self.deficit_barras} barras. Nas"
            f" {self.n_bruto_necessario} barras exigidas a mesma taxa acumula"
            f" {self.efeito_acumulado_no_cruzamento_cents} centavos - o efeito"
            " minimo NAO e um total constante (D48)"
        )

    def como_dict(self) -> dict:
        return {
            "insumos": self.insumos.como_dict(),
            "regua": self.regua,
            "alfa_primeira_rejeicao_ppm": self.alfa_primeira_rejeicao_ppm,
            "z_alfa_micro": self.z_alfa_micro,
            "z_beta_micro": self.z_beta_micro,
            "t_exigido_micro": self.t_exigido_micro,
            "efeito_por_barra_bps_micro": self.efeito_por_barra_bps_micro,
            "taxa_por_barra_bps_micro": self.taxa_por_barra_bps_micro,
            "efeito_acumulado_no_cruzamento_cents": (
                self.efeito_acumulado_no_cruzamento_cents
            ),
            "sharpe_por_barra_micro": self.sharpe_por_barra_micro,
            "n_minimo_efetivo": self.n_minimo_efetivo,
            "fator_dependencia_ppm": self.fator_dependencia_ppm,
            "n_bruto_necessario": self.n_bruto_necessario,
            "n_bruto_disponivel": self.n_bruto_disponivel,
            "deficit_barras": self.deficit_barras,
            "veredito": self.veredito,
            "cabe": self.cabe,
            "motivo": self.motivo,
            "n_minimo_efetivo_pelo_sharpe": self.n_minimo_efetivo_pelo_sharpe,
            "t_secao_8_3_micro": self.t_secao_8_3_micro,
        }


def dimensionar(
    *,
    efeito_minimo_cents: int,
    base_de_normalizacao_cents: int,
    variancia_desvio_por_barra_bps: int,
    dependencia_rho_ppm: int,
    horizonte_barras: int,
    potencia_ppm: int | None,
    familia_m: int,
    fdr_alfa_bps: int,
    disponivel_barras: int | None = None,
    procedimento: str = "BY",
    sharpe_esperado_milesimos: int | None = None,
    duracao_barra_ms: int | None = None,
) -> Dimensionamento:
    """Quantas observacoes o desenho precisa, e se elas existem.

    `potencia_ppm` e **keyword-only e sem default**: chamar sem ela e
    `TypeError`, e passar `None` levanta `PotenciaNaoDeclarada`. As duas portas
    estao fechadas de proposito - a segunda e a que um chamador distraido
    encontraria ao propagar um campo opcional.

    A conta, em uma linha:

        efeito_por_barra = efeito_minimo / (base_de_normalizacao * horizonte)
        sharpe_por_barra = efeito_por_barra / desvio_por_barra
        n_efetivo        = ceil( (t / sharpe_por_barra)^2 )
        n_bruto          = ceil( n_efetivo / fator_de_dependencia )

    **Tudo por barra, sem passar por anualizacao.** A ida e volta para o ano e
    para tras cancela exatamente (`n = (t/S_anual)^2 * barras_por_ano =
    (t/S_barra)^2`), entao ela so acrescentaria dois arredondamentos a um
    numero que vai para registro imutavel.

    **`horizonte_barras` e `disponivel_barras` sao coisas diferentes, e
    confundi-las produz um numero que piora quando os dados aumentam.** O
    efeito minimo e um TOTAL declarado sobre um horizonte; a taxa por barra sai
    da divisao dos dois. Se o horizonte disponivel entrasse na divisao,
    perguntar "cabe no dataset inteiro?" diluiria o mesmo efeito por mais
    barras, derrubaria a taxa e exigiria MAIS amostra - a resposta pioraria
    justamente por haver mais dado. `disponivel_barras` e so o lado da
    prateleira, e por padrao e o proprio horizonte declarado.

    `sharpe_esperado_milesimos` e `duracao_barra_ms` sao opcionais e servem so
    para calcular a leitura antiga ao lado. Sem eles ela sai `None` - nunca
    zero, que afirmaria uma conta que ninguem fez.
    """
    exigir_potencia(potencia_ppm)
    assert potencia_ppm is not None  # ja garantido acima; e para o type checker
    if efeito_minimo_cents <= 0:
        raise DimensionamentoImpossivel(
            "o efeito minimo precisa ser positivo: uma hipotese cujo menor"
            " efeito relevante e zero nao afirma nada que valha testar"
        )
    if base_de_normalizacao_cents <= 0:
        raise DimensionamentoImpossivel("capital exposto precisa ser positivo")
    if variancia_desvio_por_barra_bps <= 0:
        raise DimensionamentoImpossivel(
            "a variancia precisa ser positiva e MEDIDA: com desvio zero"
            " qualquer efeito seria detectavel com uma observacao, e o numero"
            " que sairia daqui seria ficcao"
        )
    if horizonte_barras <= 0:
        raise DimensionamentoImpossivel("horizonte precisa ser positivo")
    disponivel = horizonte_barras if disponivel_barras is None else disponivel_barras
    if disponivel <= 0:
        raise DimensionamentoImpossivel("horizonte disponivel precisa ser positivo")

    alfa_ppm = alfa_primeira_rejeicao_ppm(
        procedimento=procedimento, m=familia_m, alfa_bps=fdr_alfa_bps
    )
    z_alfa = z_de_cauda_micro(alfa_ppm)
    z_beta = z_de_potencia_micro(potencia_ppm)
    t_micro = z_alfa + z_beta

    # O efeito minimo, virado taxa por barra e depois padronizado pelo desvio.
    # `Fraction` do inicio ao fim: este numero eleva ao quadrado, e um erro de
    # arredondamento aqui aparece multiplicado na amostra exigida.
    efeito_por_barra_bps = Fraction(
        efeito_minimo_cents * 10_000,
        base_de_normalizacao_cents * horizonte_barras,
    )
    sharpe_barra = efeito_por_barra_bps / variancia_desvio_por_barra_bps
    razao = Fraction(t_micro, 1_000_000) / sharpe_barra
    alvo = razao * razao
    n_efetivo = -(-alvo.numerator // alvo.denominator)  # ceil exato

    fator_ppm = poder.fator_ppm_de_rho(dependencia_rho_ppm)
    if fator_ppm <= 0:
        raise DimensionamentoImpossivel(
            f"dependencia de {dependencia_rho_ppm} ppm anula a amostra"
            " efetiva: nenhuma quantidade de barras produziria observacao"
            " independente, e o dimensionamento nao existe"
        )
    n_bruto = -(-n_efetivo * 1_000_000 // fator_ppm)

    cabe = n_bruto <= disponivel
    pelo_sharpe = None
    if sharpe_esperado_milesimos is not None and duracao_barra_ms is not None:
        pelo_sharpe = _pelo_sharpe_declarado(
            sharpe_milesimos=sharpe_esperado_milesimos,
            duracao_barra_ms=duracao_barra_ms,
            t_micro=t_micro,
        )

    return Dimensionamento(
        insumos=Insumos(
            procedimento=procedimento,
            familia_m=familia_m,
            fdr_alfa_bps=fdr_alfa_bps,
            efeito_minimo_cents=efeito_minimo_cents,
            variancia_desvio_por_barra_bps=variancia_desvio_por_barra_bps,
            dependencia_rho_ppm=dependencia_rho_ppm,
            potencia_ppm=potencia_ppm,
            base_de_normalizacao_cents=base_de_normalizacao_cents,
            horizonte_declarado_barras=horizonte_barras,
            horizonte_disponivel_barras=disponivel,
        ),
        regua=REGUA_BY_POTENCIA,
        alfa_primeira_rejeicao_ppm=alfa_ppm,
        z_alfa_micro=z_alfa,
        z_beta_micro=z_beta,
        t_exigido_micro=t_micro,
        efeito_por_barra_bps_micro=int(efeito_por_barra_bps * 1_000_000),
        sharpe_por_barra_micro=int(sharpe_barra * 1_000_000),
        n_minimo_efetivo=n_efetivo,
        fator_dependencia_ppm=fator_ppm,
        n_bruto_necessario=n_bruto,
        n_bruto_disponivel=disponivel,
        deficit_barras=max(0, n_bruto - disponivel),
        veredito=VEREDITO_TESTAVEL if cabe else VEREDITO_NAO_TESTAVEL,
        n_minimo_efetivo_pelo_sharpe=pelo_sharpe,
        t_secao_8_3_micro=poder.T_ALVO * 1_000_000,
    )


# ---------------------------------------------------------------------------
# A pergunta invertida: o que este desenho CONSEGUE testar
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capacidade:
    """O menor efeito que um horizonte consegue detectar, sob a mesma regua.

    `dimensionar` responde *"esta hipotese cabe?"*. Esta responde *"o que
    caberia?"* - e e a que sustenta uma decisao prospectiva sobre capacidade
    experimental, porque nao depende de nenhuma hipotese ja escrita. Sem ela, a
    unica forma de descobrir o limite seria propor hipoteses ate uma passar, o
    que e escolher a regua olhando o resultado pelo caminho mais longo.
    """

    barras_disponiveis: int
    fator_dependencia_ppm: int
    n_efetivo_disponivel: int
    t_exigido_micro: int
    sharpe_por_barra_micro: int
    sharpe_anualizado_milesimos: int
    efeito_por_barra_bps_micro: int
    menor_efeito_detectavel_cents: int
    #: Os tres campos da conferência dimensional, e eles so existem quando uma
    #: TAXA declarada e informada. `None` quando nao ha taxa contra que
    #: comparar - nunca zero, que afirmaria um efeito esperado de nada.
    taxa_declarada_bps_micro: int | None
    efeito_esperado_acumulado_cents: int | None
    diferenca_cents: int | None

    def como_dict(self) -> dict:
        return {
            "barras_disponiveis": self.barras_disponiveis,
            "fator_dependencia_ppm": self.fator_dependencia_ppm,
            "n_efetivo_disponivel": self.n_efetivo_disponivel,
            "t_exigido_micro": self.t_exigido_micro,
            "sharpe_por_barra_micro": self.sharpe_por_barra_micro,
            "sharpe_anualizado_milesimos": self.sharpe_anualizado_milesimos,
            "efeito_por_barra_bps_micro": self.efeito_por_barra_bps_micro,
            "menor_efeito_detectavel_cents": self.menor_efeito_detectavel_cents,
            "taxa_declarada_bps_micro": self.taxa_declarada_bps_micro,
            "efeito_esperado_acumulado_cents": (
                self.efeito_esperado_acumulado_cents
            ),
            "diferenca_cents": self.diferenca_cents,
        }


def _raiz_micro(n: int) -> int:
    """`sqrt(n)` em micro, por raiz inteira. Sem `float` no caminho do numero."""
    return math.isqrt(n * 1_000_000_000_000)


def capacidade(
    *,
    barras_disponiveis: int,
    dependencia_rho_ppm: int,
    variancia_desvio_por_barra_bps: int,
    base_de_normalizacao_cents: int,
    duracao_barra_ms: int,
    potencia_ppm: int | None,
    familia_m: int,
    fdr_alfa_bps: int,
    taxa_declarada_bps_micro: int | None = None,
    procedimento: str = "BY",
) -> Capacidade:
    """O menor efeito detectavel naquele horizonte, com a potencia declarada.

    Inverte `dimensionar`: fixa `n` no que existe e resolve o efeito.

        n_efetivo    = barras * fator_de_dependencia
        sharpe_barra = t / sqrt(n_efetivo)
        efeito_barra = sharpe_barra * desvio_por_barra
        efeito_total = efeito_barra * base_de_normalizacao * barras

    Todo arredondamento vai para CIMA: o menor efeito detectavel e uma
    afirmacao sobre o que o desenho **nao** alcanca, e errar para baixo aqui
    prometeria sensibilidade que nao existe.

    `sharpe_anualizado_milesimos` e o numero comparavel entre horizontes - o
    efeito total em centavos nao e, porque cresce com a janela sobre a qual e
    declarado.

    ## `taxa_declarada_bps_micro`, e por que ela e o insumo que faltava

    Sem ela, esta funcao publica so o menor efeito detectavel - e esse numero
    CRESCE com o horizonte, porque e um total sobre uma janela que cresceu mais
    rapido do que a sensibilidade melhorou. Lido sozinho, ele parece dizer que
    mais dado piora a situacao.

    Com a taxa, saem os tres numeros que tornam a leitura possivel: o efeito
    **esperado acumulado** naquela taxa, o **minimo detectavel**, e a
    **diferenca**. Os dois primeiros crescem, e a diferenca **encolhe** - que e
    a leitura correta. Ela cruza zero exatamente em `n_bruto_necessario`, por
    construcao.
    """
    exigir_potencia(potencia_ppm)
    assert potencia_ppm is not None
    if barras_disponiveis <= 0:
        raise DimensionamentoImpossivel("horizonte precisa ser positivo")
    if variancia_desvio_por_barra_bps <= 0:
        raise DimensionamentoImpossivel("a variancia precisa ser positiva")
    if base_de_normalizacao_cents <= 0:
        raise DimensionamentoImpossivel("capital exposto precisa ser positivo")

    alfa_ppm = alfa_primeira_rejeicao_ppm(
        procedimento=procedimento, m=familia_m, alfa_bps=fdr_alfa_bps
    )
    t_micro = t_exigido_micro(alfa_ppm=alfa_ppm, potencia_ppm=potencia_ppm)

    fator_ppm = poder.fator_ppm_de_rho(dependencia_rho_ppm)
    n_efetivo = barras_disponiveis * fator_ppm // 1_000_000  # trunca: para baixo
    if n_efetivo <= 0:
        raise DimensionamentoImpossivel(
            f"dependencia de {dependencia_rho_ppm} ppm zera a amostra efetiva"
            f" de {barras_disponiveis} barras: nao ha capacidade a reportar"
        )

    raiz_micro = _raiz_micro(n_efetivo)
    sharpe_barra_micro = -(-t_micro * 1_000_000 // raiz_micro)
    efeito_barra_micro = sharpe_barra_micro * variancia_desvio_por_barra_bps
    efeito_cents = -(
        -efeito_barra_micro * base_de_normalizacao_cents * barras_disponiveis
        // (1_000_000 * 10_000)
    )
    raiz_bpa_micro = _raiz_micro(poder.barras_por_ano(duracao_barra_ms))
    sharpe_anual_milesimos = -(
        -sharpe_barra_micro * raiz_bpa_micro // (1_000_000 * 1_000)
    )
    esperado = diferenca = None
    if taxa_declarada_bps_micro is not None:
        if taxa_declarada_bps_micro <= 0:
            raise DimensionamentoImpossivel(
                "a taxa declarada precisa ser positiva: uma taxa nula nao"
                " acumula efeito nenhum, e comparar contra ela nao informa"
            )
        # O acumulado trunca para BAIXO, ao contrario do detectavel, que
        # arredonda para cima. Os dois erram na mesma direcao - a de nao
        # prometer sensibilidade nem desempenho que nao se mediu.
        esperado = (
            taxa_declarada_bps_micro
            * base_de_normalizacao_cents
            * barras_disponiveis
            // (1_000_000 * 10_000)
        )
        diferenca = esperado - efeito_cents

    return Capacidade(
        barras_disponiveis=barras_disponiveis,
        fator_dependencia_ppm=fator_ppm,
        n_efetivo_disponivel=n_efetivo,
        t_exigido_micro=t_micro,
        sharpe_por_barra_micro=sharpe_barra_micro,
        sharpe_anualizado_milesimos=sharpe_anual_milesimos,
        efeito_por_barra_bps_micro=efeito_barra_micro,
        menor_efeito_detectavel_cents=efeito_cents,
        taxa_declarada_bps_micro=taxa_declarada_bps_micro,
        efeito_esperado_acumulado_cents=esperado,
        diferenca_cents=diferenca,
    )


def _pelo_sharpe_declarado(
    *, sharpe_milesimos: int, duracao_barra_ms: int, t_micro: int
) -> int:
    """`n_minimo` pela parametrizacao antiga, com o `t` novo.

    Serve de ponte: e o numero da tabela que o usuario ja viu (82.904 para a
    hipotese 41 sob BY + 80%). Ele difere do dimensionamento pelo efeito minimo
    porque mede coisas diferentes - o Sharpe declarado e o que a hipotese
    ESPERA, o efeito minimo e o que ainda IMPORTA -, e publicar os dois e o que
    impede a troca de regua de passar calada.
    """
    if sharpe_milesimos <= 0:
        raise DimensionamentoImpossivel("Sharpe esperado precisa ser positivo")
    bpa = poder.barras_por_ano(duracao_barra_ms)
    # n = (t / sharpe_anual)^2 * barras_por_ano. Com `t` em micro e o Sharpe em
    # milesimos, isso e `t_micro^2 * bpa / (10^6 * sharpe^2)`, exato em
    # inteiros e arredondado para cima.
    return -(-(t_micro * t_micro * bpa)
             // (1_000_000 * sharpe_milesimos * sharpe_milesimos))
