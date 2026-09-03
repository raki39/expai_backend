"""Deflated Sharpe Ratio (R41, §8.6, critério B6 de §14.4).

> "Para estratégias de trading, aplica-se adicionalmente o **Deflated Sharpe
> Ratio (DSR)** de Bailey e López de Prado, que corrige o Sharpe observado
> pelo número efetivo de tentativas, pelo tamanho da amostra, e pela não
> normalidade dos retornos." — §8.6

> "**O DSR é uma probabilidade, não um score.** Exigir que ele 'seja positivo'
> não significa nada, já que uma probabilidade nunca é negativa. O critério é
> **DSR ≥ 0,95**." — §8.6

## A fórmula

Duas partes. Primeiro o **Sharpe esperado do melhor de N tentativas sob a
nula** — o valor que a seleção sozinha produz:

```
SR0 = sqrt(V) * [ (1 - g) * Phi^-1(1 - 1/N) + g * Phi^-1(1 - 1/(N*e)) ]
```

`V` é a variância dos Sharpes entre as tentativas, `g` é a constante de
Euler-Mascheroni (0,5772...), `N` o número de tentativas. É a aproximação do
valor esperado do máximo de N normais.

Depois o **DSR**, que é a probabilidade de o Sharpe observado superar aquele
patamar, já corrigida por assimetria e curtose:

```
DSR = Phi[ (SR - SR0) * sqrt(n - 1) / sqrt(1 - g3*SR + (g4 - 1)/4 * SR^2) ]
```

`SR` e `SR0` **por observação**, não anualizados — o `sqrt(n-1)` é que traz a
amostra para dentro. `g3` é a assimetria, `g4` a curtose **bruta** (normal =
3). Misturar convenção de curtose aqui é o erro silencioso mais fácil de
cometer, e por isso o nome do campo diz "bruta" em três lugares.

## O que cada termo faz, e por que nenhum é decorativo

| Termo | Efeito |
|---|---|
| `N` tentativas ↑ | `SR0` sobe, DSR **cai** — mais tentativas, mais fácil achar sorte |
| `n` amostra ↑ | `sqrt(n-1)` sobe, DSR **sobe** — mais dado, mais confiança |
| assimetria `g3` < 0 | denominador **cresce**, DSR cai — cauda esquerda pesada |
| curtose `g4` > 3 | denominador **cresce**, DSR cai — retorno não normal |

Ignorar assimetria e curtose daria um número maior, sempre. Há teste provando
que zerá-los muda o resultado: se não mudasse, a implementação estaria
incompleta e ninguém notaria.

## Honestidade sobre a verificação

Este módulo é conferido contra a **fórmula publicada** por Bailey e López de
Prado, reproduzida acima termo a termo, mais um caso aritmético calculado à mão
nos testes e as quatro propriedades da tabela.

**Não** afirmo reproduzir uma tabela numérica específica do artigo: não tenho o
artigo à mão para conferir dígito a dígito, e dizer "confere com o publicado"
sem ter conferido seria exatamente o tipo de afirmação que este projeto recusa.
Fica registrado como pendência de verificação — barata, e que exige o PDF.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .pvalor import phi, phi_inversa

# Euler-Mascheroni. Entra na aproximação do valor esperado do máximo de N
# normais, e não é ajustável.
GAMMA = 0.5772156649015329


class DSRImpossivel(Exception):
    """Falta insumo. Diferente de "calculou e deu baixo"."""


@dataclass(frozen=True)
class Resultado:
    dsr_milesimos: int
    sr0_por_observacao: float
    sharpe_por_observacao: float
    tentativas: int
    n: int
    assimetria: float
    curtose_bruta: float
    aprovado: bool
    limiar_milesimos: int

    def como_dict(self) -> dict:
        return {
            "dsr_milesimos": self.dsr_milesimos,
            "limiar_milesimos": self.limiar_milesimos,
            "aprovado": self.aprovado,
            "e_probabilidade": (
                "DSR e a probabilidade de o Sharpe verdadeiro ser maior que"
                " zero depois de descontada a selecao (secao 8.6). Exigir que"
                " 'seja positivo' nao significa nada"
            ),
            "insumos": {
                "sharpe_por_observacao_milionesimos": round(
                    self.sharpe_por_observacao * 1_000_000
                ),
                "sr0_por_observacao_milionesimos": round(
                    self.sr0_por_observacao * 1_000_000
                ),
                "tentativas": self.tentativas,
                "n": self.n,
                "assimetria_milesimos": round(self.assimetria * 1_000),
                "curtose_bruta_milesimos": round(self.curtose_bruta * 1_000),
            },
        }


def sharpe_esperado_do_maximo(
    *, tentativas: int, variancia_dos_sharpes: float
) -> float:
    """`SR0`: o Sharpe que a SELEÇÃO sozinha produz, com N tentativas.

    Sob a nula, o melhor de N tentativas tem Sharpe esperado positivo mesmo
    que nenhuma tenha vantagem — e é justamente esse patamar que o Sharpe
    observado tem de superar para significar algo.

    Com uma tentativa só, `Phi^-1(1 - 1/1)` é infinito. Uma tentativa não tem
    máximo a corrigir, então `SR0 = 0`: não é caso especial disfarçado, é o
    valor certo.
    """
    if tentativas < 1:
        raise DSRImpossivel("o número de tentativas precisa ser positivo")
    if variancia_dos_sharpes < 0:
        raise DSRImpossivel("variância não pode ser negativa")
    if tentativas == 1:
        return 0.0
    n = float(tentativas)
    termo = (1.0 - GAMMA) * phi_inversa(1.0 - 1.0 / n) + GAMMA * phi_inversa(
        1.0 - 1.0 / (n * math.e)
    )
    return math.sqrt(variancia_dos_sharpes) * termo


def calcular(
    *,
    sharpe_por_observacao: float,
    n: int,
    tentativas: int,
    assimetria: float,
    curtose_bruta: float,
    variancia_dos_sharpes: float | None = None,
    limiar_milesimos: int = 950,
) -> Resultado:
    """O DSR, entre 0 e 1, em milésimos.

    `variancia_dos_sharpes` é a variância dos Sharpes **entre as tentativas**.
    Quando não é informada, usa-se `1 / (n - 1)` — a variância assintótica do
    estimador de Sharpe sob a nula, que é o que se tem quando as tentativas
    individuais não foram todas registradas com o Sharpe delas.

    Isso é uma suposição, e ela é declarada: com a variância real das
    tentativas o `SR0` é maior ou menor conforme a dispersão observada, e o
    DSR muda. Na 0B o contador global dá o `N`; registrar o Sharpe de **cada**
    tentativa é o que permitiria trocar a suposição por medida, e isso é
    trabalho do incremento 12 em diante, quando o B4 produzir tentativas em
    massa.
    """
    if n < 2:
        raise DSRImpossivel(
            f"n = {n}: o DSR precisa de `sqrt(n - 1)`, e uma observação não"
            " sustenta afirmação sobre o Sharpe verdadeiro"
        )
    if tentativas < 1:
        raise DSRImpossivel("o número de tentativas precisa ser positivo")

    var = (
        variancia_dos_sharpes
        if variancia_dos_sharpes is not None
        else 1.0 / (n - 1)
    )
    sr0 = sharpe_esperado_do_maximo(
        tentativas=tentativas, variancia_dos_sharpes=var
    )

    sr = sharpe_por_observacao
    denominador_quadrado = (
        1.0 - assimetria * sr + (curtose_bruta - 1.0) / 4.0 * sr * sr
    )
    if denominador_quadrado <= 0:
        raise DSRImpossivel(
            "o ajuste de não-normalidade deu denominador não positivo"
            f" ({denominador_quadrado:.6f}): a combinação de assimetria"
            f" ({assimetria:.3f}), curtose ({curtose_bruta:.3f}) e Sharpe"
            f" ({sr:.4f}) está fora do domínio em que a fórmula vale, e"
            " devolver um número aqui seria inventar um"
        )
    z = (sr - sr0) * math.sqrt(n - 1) / math.sqrt(denominador_quadrado)
    dsr = phi(z)
    # Trunca para BAIXO: um DSR arredondado para cima aprovaria na fronteira
    # do limiar sem ter alcançado.
    milesimos = min(1_000, int(dsr * 1_000))
    return Resultado(
        dsr_milesimos=milesimos,
        sr0_por_observacao=sr0,
        sharpe_por_observacao=sr,
        tentativas=tentativas,
        n=n,
        assimetria=assimetria,
        curtose_bruta=curtose_bruta,
        aprovado=milesimos >= limiar_milesimos,
        limiar_milesimos=limiar_milesimos,
    )
