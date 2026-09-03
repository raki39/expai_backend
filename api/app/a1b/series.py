"""As séries que A1b injeta: nulas, e nulas com sinal implantado.

> "**Estocástico** | Sinais aleatórios; séries permutadas; variáveis
> independentes do alvo; rótulos embaralhados; regras geradas por ruído | O
> **calibre estatístico** está fora do alvo" — §14.4

## Por que sinal trocado, e não permutação

Permutar os retornos do mercado é a nula clássica para "a ORDEM importa" —
ela destrói a estrutura temporal e preserva a distribuição marginal. Mas
**preserva também a média**, e o nosso teste é sobre Sharpe contra zero: uma
série permutada tem exatamente o mesmo Sharpe da original. Ela não é nula para
a pergunta que fazemos.

O que zera a média sem inventar distribuição é trocar o **sinal** de cada
retorno ao acaso. A volatilidade fica, as caudas gordas ficam, a média vai a
zero por construção, e a assimetria some — o que é conservador para o DSR na
direção certa (assimetria negativa o derrubaria).

## O sinal implantado é uma DERIVA, e o tamanho dela é declarado

Para medir poder é preciso um efeito de magnitude conhecida (§14.4: "sinais
sintéticos de magnitude conhecida também medem o poder do protocolo"). A
deriva sai da definição do Sharpe anualizado:

```
S = (mu / sigma) * sqrt(barras_por_ano)   ->   mu = S * sigma / sqrt(barras_por_ano)
```

`sigma` é o desvio da própria série sorteada, então o Sharpe implantado é o
pedido **daquela série**, e não uma aproximação sobre uma volatilidade suposta.

## O que estas séries NÃO exercitam, e digo que não

Elas entram no pipeline **estatístico** — momentos, `n_efetivo`, p-valor, BY,
DSR —, que é o que decide promoção no lote. Elas não passam pelo simulador,
pela avaliação de regra nem pelo ledger: uma execução repetida que rodasse o
mercado inteiro custaria minutos, e são 400 delas (D29).

Isso é uma limitação real e ela é reportada junto do número. O que cobre o
outro lado é A1a, que injeta **pelo mesmo caminho das reais** e passa por
todos esses módulos.
"""

from __future__ import annotations

import math
import random

#: O piso de barras para uma série ter quarto momento com significado. Abaixo
#: disso `sharpe.momentos` recusa, e uma série curta produziria um DSR que
#: descreve o tamanho da amostra e não a estratégia.
MINIMO_DE_BARRAS = 30

#: Unidade das séries de A1b: centésimos de bps.
#:
#: **Não é preciosismo, é a diferença entre haver sinal e não haver.** Com
#: desvio típico de ~30 bps por barra de 15 min e 35.064 barras por ano, a
#: deriva que produz Sharpe anualizado 2,58 vale
#: `2,58 × 30 / √35.064 = 0,41 bps` — que arredondado para bps inteiros vira
#: ZERO, e o sinal implantado desapareceria sem nada acusando. O poder medido
#: seria o poder contra nenhum efeito.
#:
#: Sharpe, autocorrelação, assimetria e curtose são **invariantes de escala**:
#: multiplicar a série inteira por 100 não muda nenhum deles. O que muda é só
#: o arredondamento parar de comer o efeito.
ESCALA = 100


def nula(rng: random.Random, base_bps: list[int], n: int) -> list[int]:
    """Série de média zero por construção, com as caudas da série real.

    Sorteia com reposição da série real e troca o sinal ao acaso. A troca de
    sinal é o que faz a média ir a zero **sem** supor normalidade — e supor
    normalidade aqui seria calibrar o protocolo contra um mundo que §8.3 já
    diz que não é o nosso.
    """
    if not base_bps:
        raise ValueError("serie base vazia")
    return [
        rng.choice(base_bps) * ESCALA * (1 if rng.random() < 0.5 else -1)
        for _ in range(n)
    ]


def com_sinal(
    rng: random.Random,
    base_bps: list[int],
    n: int,
    *,
    sharpe_milesimos: int,
    barras_por_ano: int,
) -> list[int]:
    """Nula mais uma deriva que produz o Sharpe anualizado pedido.

    A deriva é calculada sobre o desvio **da série sorteada**, e não sobre um
    desvio suposto: o sinal implantado precisa ter a magnitude declarada
    naquela série, senão o poder medido descreveria outra coisa.
    """
    serie = nula(rng, base_bps, n)
    if len(serie) < 2:
        return serie
    media = sum(serie) / len(serie)
    var = sum((r - media) ** 2 for r in serie) / (len(serie) - 1)
    desvio = math.sqrt(var)
    if desvio == 0:
        return serie
    mu = (sharpe_milesimos / 1_000) * desvio / math.sqrt(barras_por_ano)
    # Inteiro, como toda a série (regra 5). Na escala de centésimos de bps a
    # deriva vale dezenas de unidades, e o arredondamento custa menos de 1%
    # dela; em bps inteiros ela valeria 0,41 e viraria zero.
    return [r + round(mu) for r in serie]
