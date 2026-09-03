"""`n_minimo` calculado antes do teste, nunca escolhido depois (R34, secao 8.3).

> "`n_minimo` e calculado antes do teste, e nao escolhido depois."
> — secao 8.3

A secao 8.3 da a formula e a tabela que ela produz:

```
t ~= Sharpe_anualizado x sqrt(anos de observacao)
```

| Sharpe verdadeiro | Tempo para t > 2 |
|---|---|
| 0,5 | ~16 anos |
| 1,0 | ~4 anos |
| 2,0 | ~1 ano |
| 3,0 | ~5 meses |

Invertendo para t >= 2: `anos = (2 / Sharpe)^2`. Os quatro valores da tabela
saem dai, e `tests/test_hipotese.py` confere os quatro - a implementacao e
verificada contra o documento, nao contra ela mesma.

## As duas ressalvas do documento, e o que cada uma obriga

**"Ela e aproximacao didatica", e depende de independencia e estacionariedade
aproximadas.** Nao ha o que consertar aqui: usar uma formula melhor exigiria
premissas que tambem nao valem. O que se pode fazer e nao esconder que e
aproximacao - por isso `n_minimo` e sempre reportado junto do Sharpe declarado
que o produziu, e nunca sozinho.

**"Aumentar a frequencia nao fabrica amostra."** Esta obriga codigo. O
documento e explicito: "mil candles autocorrelacionados nao equivalem a mil
observacoes independentes (...) o sistema deve calcular `n_efetivo` e nao
`n_bruto`". Entao `n_minimo` e expresso em observacoes EFETIVAS, e comparar
contra ele exige descontar a autocorrelacao do que foi observado -
`efetivo_de_bruto` abaixo.

## Aritmetica inteira, e por que aqui tambem

Sharpe entra em milesimos. A regra 5 fala de valores monetarios, e Sharpe nao
e um; o motivo aqui e outro: `n_minimo` vai para um pre-registro imutavel que
precisa ser reproduzivel entre maquinas (R12). Ponto flutuante em codigo que
alimenta digest e como este projeto ja se enganou - com a diferenca de que
aqui ninguem notaria, porque o erro apareceria no ultimo digito.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isqrt

# t > 2 e o limiar da tabela da secao 8.3. Nao e escolha nossa: e o numero
# que o documento usa para construir a tabela contra a qual isto e conferido.
T_ALVO = 2

MS_POR_ANO = 365_25 * 24 * 60 * 60 * 1_000 // 100  # 365,25 dias em ms


class HorizonteInsuficiente(Exception):
    """A hipotese nao cabe no horizonte disponivel: nasce NAO TESTAVEL.

    Secao 8.3: "Uma hipotese cujo `n_minimo` nao e alcancavel no horizonte
    disponivel e marcada como nao testavel e arquivada, em vez de ser testada
    mal. (...) Descobrir isso no pre-registro custa nada; descobrir depois de
    quatro meses de forward custa quatro meses."
    """


def barras_por_ano(duracao_barra_ms: int) -> int:
    """Quantas barras daquele timeframe cabem num ano de 365,25 dias.

    Derivado do timeframe, nao constante: fixar 35.064 aqui faria o numero
    parar de descrever no dia em que o timeframe mudasse, sem que nada
    acusasse. E o padrao que este projeto ja registrou sete vezes.
    """
    if duracao_barra_ms <= 0:
        raise ValueError("duracao de barra precisa ser positiva")
    return round(MS_POR_ANO / duracao_barra_ms)


def n_minimo(*, sharpe_milesimos: int, duracao_barra_ms: int) -> int:
    """Observacoes EFETIVAS necessarias para `t > 2`, no Sharpe declarado.

    `anos = (2 / Sharpe)^2`, convertido para barras. Em inteiros exatos:

        anos          = 4 * 10^6 / sharpe_milesimos^2
        n_minimo      = ceil(anos * barras_por_ano)
                      = ceil(4 * 10^6 * barras_por_ano / sharpe_milesimos^2)

    Arredondado para CIMA: pedir menos amostra do que a conta manda seria
    afrouxar o criterio na direcao de aprovar, que e exatamente a direcao em
    que nao se erra de graca.
    """
    if sharpe_milesimos <= 0:
        raise ValueError(
            "Sharpe esperado precisa ser positivo: uma hipotese que espera"
            " Sharpe zero ou negativo nao afirma nada que valha testar"
        )
    numerador = 4 * 1_000_000 * barras_por_ano(duracao_barra_ms)
    denominador = sharpe_milesimos * sharpe_milesimos
    return -(-numerador // denominador)  # ceil de divisao inteira


def anos_necessarios_milesimos(sharpe_milesimos: int) -> int:
    """`(2 / Sharpe)^2` em milesimos de ano. So para exibir e para conferir."""
    if sharpe_milesimos <= 0:
        raise ValueError("Sharpe esperado precisa ser positivo")
    return round(4 * 1_000_000 * 1_000 / (sharpe_milesimos * sharpe_milesimos))


def sharpe_minimo_testavel(
    *, duracao_barra_ms: int, horizonte_barras: int
) -> int:
    """O menor Sharpe declaravel que ainda cabe no horizonte, em milesimos.

    Inverte `n_minimo`: `sharpe = sqrt(4 * 10^6 * barras_por_ano / horizonte)`,
    arredondado para CIMA - declarar exatamente o limite deixaria `n_minimo`
    igual ao horizonte, e igualdade num arredondamento e onde se erra de
    graca.

    **Existe para ir ao prompt.** Sem este numero o modelo declara um Sharpe
    plausivel, a hipotese nasce nao testavel, e ele descobre isso depois de
    ja ter proposto - o que e a definicao de descobrir tarde. A secao 8.3
    manda o contrario: "descobrir isso no pre-registro custa nada".

    Ele tambem e o numero desconfortavel desta fase. Com dois anos de barras
    de 15 minutos e a divisao da D27, o in-sample tem 21.024 barras, e o
    Sharpe minimo testavel ali e **2,58** - alto demais para um edge honesto
    em mercado liquido. Isso nao e defeito da conta: e a secao 8.3 dizendo,
    com numero, que "apenas efeitos grandes sao detectaveis no horizonte do
    projeto".
    """
    if horizonte_barras <= 0:
        raise ValueError("horizonte precisa ser positivo")
    exigido = 4 * 1_000_000 * barras_por_ano(duracao_barra_ms)
    # Menor inteiro s tal que ceil(exigido / s^2) <= horizonte. Parte da raiz
    # inteira e ajusta: em inteiros, sem confiar no arredondamento de sqrt.
    s = max(1, isqrt(exigido // horizonte_barras))
    while -(-exigido // (s * s)) > horizonte_barras:
        s += 1
    while s > 1 and -(-exigido // ((s - 1) * (s - 1))) <= horizonte_barras:
        s -= 1
    return s


def conferir_horizonte(*, n_min: int, horizonte_barras: int) -> None:
    """Levanta `HorizonteInsuficiente` se `n_minimo` nao cabe no que existe.

    O limite superior e o horizonte inteiro: mesmo com autocorrelacao zero,
    `n_efetivo` nunca passa de `n_bruto`. Se `n_minimo` ja excede o horizonte,
    nenhuma execucao possivel alcanca a amostra - e testar assim mesmo
    produziria um numero que so pode ser inconclusivo, gastando dado
    reservado para nao aprender nada.
    """
    if n_min > horizonte_barras:
        raise HorizonteInsuficiente(
            f"n_minimo de {n_min} observacoes efetivas nao cabe no horizonte"
            f" de {horizonte_barras} barras; nem com autocorrelacao zero a"
            " amostra seria alcancada (secao 8.3)"
        )


# ---------------------------------------------------------------------------
# Do bruto ao efetivo, na avaliacao
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Efetivo:
    """O desconto aplicado, com as partes visiveis.

    O numero sozinho seria impossivel de auditar: `n_efetivo = 812` nao diz
    se veio de pouca barra ou de muita autocorrelacao, e as duas exigem
    reacoes opostas.
    """

    bruto: int
    autocorrelacao_ppm: int
    fator_ppm: int
    efetivo: int


def _autocorrelacao_lag1_ppm(serie: list[int]) -> int:
    """Autocorrelacao de defasagem 1, em partes por milhao.

    Recebe inteiros (retornos por barra em bps) e devolve inteiro. Zero
    quando a serie e curta demais ou constante - e nesse caso o desconto
    some, o que e o comportamento certo: sem evidencia de dependencia, nao se
    inventa uma.
    """
    n = len(serie)
    if n < 3:
        return 0
    media = sum(serie) / n
    desvios = [x - media for x in serie]
    denominador = sum(d * d for d in desvios)
    if denominador == 0:
        return 0
    numerador = sum(desvios[i] * desvios[i + 1] for i in range(n - 1))
    rho = numerador / denominador
    return max(-1_000_000, min(1_000_000, round(rho * 1_000_000)))


def efetivo_de_bruto(retornos_por_barra_bps: list[int], bruto: int) -> Efetivo:
    """`n_efetivo` a partir do que foi de fato observado (secao 8.3).

    Fator de inflacao de variancia de um AR(1):

        fator = (1 - rho) / (1 + rho)

    **Limitado a 1 por cima.** Autocorrelacao negativa aumentaria a amostra
    efetiva acima da bruta, e a conta ate diz isso - mas creditar amostra
    extra a partir de uma estimativa ruidosa de rho e otimismo, e o simulador
    inteiro deste projeto e construido na direcao oposta (regra 9). Descontar
    quando ha dependencia e conservador; premiar quando parece nao haver e
    apostar na estimativa.
    """
    rho_ppm = _autocorrelacao_lag1_ppm(retornos_por_barra_bps)
    if rho_ppm >= 1_000_000:
        fator_ppm = 0
    else:
        fator_ppm = round(
            (1_000_000 - rho_ppm) * 1_000_000 / (1_000_000 + rho_ppm)
        )
    fator_ppm = max(0, min(1_000_000, fator_ppm))
    return Efetivo(
        bruto=bruto,
        autocorrelacao_ppm=rho_ppm,
        fator_ppm=fator_ppm,
        efetivo=bruto * fator_ppm // 1_000_000,
    )
