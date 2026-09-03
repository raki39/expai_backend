"""Intervalos de confiança para A1b (§14.4, D29).

Dois, porque os dois desenhos medem coisas de forma diferente:

| Desenho | Estatística | Intervalo |
|---|---|---|
| **Nula global** | proporção de execuções com ≥1 promoção | **Wilson**, 95% |
| **Nulas + sinal** | média de `V / max(R,1)` entre execuções | **bootstrap**, 95% |

## Por que Wilson e não a aproximação normal

A proporção esperada aqui é pequena — BY com `H(48) = 4,46` promove pouco sob a
nula, por construção. O intervalo normal (`p̂ ± z·√(p̂(1-p̂)/n)`) **degenera** em
`[0, 0]` quando `p̂ = 0`, o que afirmaria certeza absoluta a partir de 200
observações. Wilson não degenera: com `0/200` ele devolve `[0, 1,8%]`, que é o
que os dados de fato sustentam.

Isso não é preciosismo: o critério do desenho 1 é sobre onde o intervalo cai, e
um intervalo que colapsa a um ponto responde qualquer pergunta com "sim".

## Por que bootstrap no segundo

`V / max(R,1)` não é uma proporção binomial — é uma média de razões, com
denominador aleatório. Não há forma fechada honesta, e o bootstrap percentil é
o que §14.4 permite sem inventar distribuição.

**Determinístico**: a reamostragem usa semente própria, então o mesmo conjunto
de execuções produz o mesmo intervalo (R12). Um IC que mudasse a cada leitura
tornaria o critério não reproduzível.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

#: z de 95%, bilateral. Em constante nomeada porque o valor aparece em duas
#: contas e um deles digitado errado moveria o critério sem mudar o texto.
Z_95 = 1.959963985


@dataclass(frozen=True)
class Intervalo:
    ponto_ppm: int
    baixo_ppm: int
    alto_ppm: int
    n: int
    metodo: str
    confianca_bps: int

    def contem_ppm(self, alvo_ppm: int) -> bool:
        return self.baixo_ppm <= alvo_ppm <= self.alto_ppm

    def como_dict(self) -> dict:
        return {
            "ponto_ppm": self.ponto_ppm,
            "baixo_ppm": self.baixo_ppm,
            "alto_ppm": self.alto_ppm,
            "n": self.n,
            "metodo": self.metodo,
            "confianca_bps": self.confianca_bps,
        }


def _z(confianca_bps: int) -> float:
    """`z` para a confiança pedida. Só 95% tem valor exato aqui.

    Fora de 95% a conta exigiria a inversa da normal, que este projeto não
    tem — e aproximá-la aqui produziria um intervalo que **parece** ser da
    confiança pedida. Recusar é a resposta honesta: a D29 fixou 95%, e outra
    confiança é decisão nova, não um parâmetro que já esteja implementado.
    """
    if confianca_bps == 9_500:
        return Z_95
    raise ValueError(
        f"so ha z tabelado para 95% (9500 bps); pedido {confianca_bps}."
        " A D29 fixou 95%, e trocar a confianca e decisao nova"
    )


def wilson(
    *, sucessos: int, n: int, confianca_bps: int = 9_500
) -> Intervalo:
    """Intervalo de Wilson para uma proporção. Não degenera em 0 nem em 1."""
    if n <= 0:
        raise ValueError("Wilson exige ao menos uma observacao")
    if not 0 <= sucessos <= n:
        raise ValueError(f"sucessos {sucessos} fora de [0, {n}]")
    z = _z(confianca_bps)
    p = sucessos / n
    denominador = 1 + z * z / n
    centro = (p + z * z / (2 * n)) / denominador
    meia = (
        z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominador
    )
    return Intervalo(
        ponto_ppm=round(p * 1_000_000),
        baixo_ppm=max(0, round((centro - meia) * 1_000_000)),
        alto_ppm=min(1_000_000, round((centro + meia) * 1_000_000)),
        n=n,
        metodo="wilson",
        confianca_bps=confianca_bps,
    )


def bootstrap_da_media(
    valores: list[float],
    *,
    reamostras: int = 2_000,
    semente: int,
    confianca_bps: int = 9_500,
) -> Intervalo:
    """IC percentil para a média, por reamostragem com reposição.

    `semente` é obrigatória: um intervalo que mudasse a cada leitura tornaria
    o critério do desenho 2 não reproduzível, e R12 vale para o Portão A como
    vale para o resto.
    """
    if not valores:
        raise ValueError("bootstrap exige ao menos um valor")
    _z(confianca_bps)  # recusa confiança não tabelada antes de trabalhar
    cauda = (10_000 - confianca_bps) / 2 / 10_000

    rng = random.Random(semente)
    n = len(valores)
    medias = sorted(
        sum(rng.choice(valores) for _ in range(n)) / n
        for _ in range(reamostras)
    )
    baixo = medias[max(0, math.floor(cauda * reamostras))]
    alto = medias[min(reamostras - 1, math.ceil((1 - cauda) * reamostras) - 1)]
    return Intervalo(
        ponto_ppm=round(sum(valores) / n * 1_000_000),
        baixo_ppm=round(baixo * 1_000_000),
        alto_ppm=round(alto * 1_000_000),
        n=n,
        metodo=f"bootstrap percentil ({reamostras} reamostras)",
        confianca_bps=confianca_bps,
    )
