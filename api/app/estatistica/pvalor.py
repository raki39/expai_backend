"""O p-valor que BH e BY ordenam (§8.3, §8.6).

Isto **não existia** até o incremento 11, e é o insumo sem o qual o
procedimento de FDR não roda. O veredito do incremento 8 classifica — compara
efeito contra mínimo e confere cláusulas de falseamento — mas não produz um
número ordenável, e BH e BY ordenam p-valores.

## De onde ele sai

§8.3 dá a estatística, e ela é a mesma que produz `n_minimo`:

```
t ~= Sharpe_anualizado x sqrt(anos de observacao)
```

Sob a nula de que o Sharpe verdadeiro é zero, `t` é aproximadamente normal
padrão para amostra grande. O p-valor **de uma cauda** é `1 - Phi(t)`.

**Uma cauda, e não duas.** A hipótese é direcional: o agente afirma que a regra
tem vantagem, não que ela difere de zero em qualquer direção. Um teste de duas
caudas dobraria o p-valor e recusaria vantagem real por medir a pergunta
errada. Em compensação, Sharpe observado negativo produz p-valor acima de 0,5 —
e é isso que se quer, porque a nula não é o que ele afirmou.

## Os anos são de observação EFETIVA

`t` cresce com a raiz do tempo observado, e §8.3 avisa que "aumentar a
frequência não fabrica amostra". Então o `n` que entra aqui é o `n_efetivo`
que `hipotese.poder` calcula — barras com posição aberta, descontadas pela
autocorrelação. Usar `n_bruto` inflaria `t` pela raiz da razão entre os dois, e
inflar `t` é inflar significância.

## Isto é aproximação, e o documento diz que é

§8.3: "Ela é aproximação didática, e depende de independência e
estacionariedade aproximadas. Retornos reais violam as duas em algum grau."

Não há o que consertar: uma fórmula melhor exigiria premissas que também não
valem. O que se pode fazer é não esconder — por isso o p-valor é sempre
reportado junto do `t` e do `n_efetivo` que o produziram, e nunca sozinho.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

# Uma instância, reusada. `NormalDist().cdf` é da biblioteca padrão e usa
# `erfc`, então não há aproximação nossa de tabela normal no caminho - que
# seria mais um lugar de onde um número poderia sair um pouco errado.
_NORMAL = NormalDist()


@dataclass(frozen=True)
class Teste:
    t: float
    p_valor_ppm: int
    n_efetivo: int
    anos_efetivos: float
    sharpe_anualizado_milesimos: int

    def como_dict(self) -> dict:
        return {
            "t": round(self.t, 4),
            "p_valor_ppm": self.p_valor_ppm,
            "n_efetivo": self.n_efetivo,
            "anos_efetivos_milesimos": round(self.anos_efetivos * 1_000),
            "sharpe_anualizado_milesimos": self.sharpe_anualizado_milesimos,
            "cauda": "uma, direcional: a nula e Sharpe verdadeiro <= 0",
        }


def phi(x: float) -> float:
    """CDF normal padrão."""
    return _NORMAL.cdf(x)


def phi_inversa(q: float) -> float:
    """Quantil da normal padrão. Usado pelo DSR."""
    if not 0.0 < q < 1.0:
        raise ValueError(f"quantil precisa estar em (0,1); veio {q}")
    return _NORMAL.inv_cdf(q)


def de_sharpe(
    *,
    sharpe_anualizado: float,
    n_efetivo: int,
    barras_por_ano_: int,
) -> Teste:
    """O p-valor de uma cauda para `H0: Sharpe verdadeiro <= 0`.

    `n_efetivo` zero devolve p-valor de 1: sem amostra não há evidência
    contra a nula. Devolver 0,5 (o p-valor de `t = 0`) afirmaria que se
    mediu e não se achou nada, e são coisas diferentes.
    """
    if n_efetivo <= 0:
        return Teste(
            t=0.0,
            p_valor_ppm=1_000_000,
            n_efetivo=0,
            anos_efetivos=0.0,
            sharpe_anualizado_milesimos=round(sharpe_anualizado * 1_000),
        )
    anos = n_efetivo / barras_por_ano_
    t = sharpe_anualizado * math.sqrt(anos)
    p = 1.0 - phi(t)
    return Teste(
        t=t,
        # Arredondado para CIMA: um p-valor menor que o real promove mais
        # facilmente, e a direcao em que nao se erra de graca e a outra.
        p_valor_ppm=min(1_000_000, math.ceil(p * 1_000_000)),
        n_efetivo=n_efetivo,
        anos_efetivos=anos,
        sharpe_anualizado_milesimos=round(sharpe_anualizado * 1_000),
    )
