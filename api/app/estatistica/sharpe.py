"""Sharpe REALIZADO, assimetria e curtose — os insumos que faltavam.

Até o incremento 10 só existia `sharpe_esperado_milesimos`, que é **declaração**
do agente no pré-registro. Nada calculava o observado.

Isso bloqueava duas coisas do incremento 11 ao mesmo tempo:

- **o p-valor**, que BH e BY precisam ordenar, sai da estatística t, e a t sai
  do Sharpe observado (§8.3: `t ≈ Sharpe_anualizado × √anos`);
- **o DSR**, que corrige o Sharpe observado "pelo número efetivo de tentativas,
  pelo tamanho da amostra, e pela **assimetria e curtose** dos retornos"
  (§8.6).

## Por que ponto flutuante aqui, e por que isso não fere a regra 5

A regra 5 é sobre **valor monetário**: nenhuma coluna de dinheiro em ponto
flutuante. Sharpe, assimetria e curtose não são dinheiro — são estatísticas
adimensionais de uma série, e calcular momentos de terceira e quarta ordem em
inteiros exigiria aritmética de precisão arbitrária para ganhar nada.

O que **é** preservado: a entrada é inteira (retornos em bps), a saída
persistida é inteira (milésimos), e nenhum desses números entra em digest de
reprodutibilidade. O ponto flutuante vive só entre as duas pontas.

## Sharpe de uma série sem excesso sobre taxa livre de risco

Não há taxa livre de risco no cálculo. Isso é deliberado e vale dizer: o
projeto compara **sempre contra baseline** (regra 14), e o baseline relevante
aqui é o zero — a pergunta que o DSR responde é "o Sharpe verdadeiro é maior
que zero depois de descontada a seleção?" (§8.6). Enfiar uma taxa livre de
risco mudaria a hipótese nula sem que ninguém tivesse decidido isso.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Um ano de 365,25 dias em ms, igual ao de `hipotese.poder`. O mesmo número em
# dois lugares é como este projeto já se enganou; aqui ele é importado.
from ..hipotese.poder import barras_por_ano


@dataclass(frozen=True)
class Momentos:
    """Os quatro números que o DSR precisa, mais o que os produziu."""

    n: int
    media: float
    desvio: float
    assimetria: float
    curtose: float  # BRUTA, não excesso: normal = 3,0

    @property
    def sharpe_por_observacao(self) -> float:
        return 0.0 if self.desvio == 0 else self.media / self.desvio

    def sharpe_anualizado(self, duracao_barra_ms: int) -> float:
        """`SR_por_obs × √(observações por ano)`."""
        return self.sharpe_por_observacao * math.sqrt(
            barras_por_ano(duracao_barra_ms)
        )

    def como_dict(self, duracao_barra_ms: int) -> dict:
        return {
            "n": self.n,
            "sharpe_por_observacao_milionesimos": round(
                self.sharpe_por_observacao * 1_000_000
            ),
            "sharpe_anualizado_milesimos": round(
                self.sharpe_anualizado(duracao_barra_ms) * 1_000
            ),
            "assimetria_milesimos": round(self.assimetria * 1_000),
            "curtose_milesimos": round(self.curtose * 1_000),
        }


class AmostraCurta(Exception):
    """Menos de quatro observações não tem quarto momento."""


def momentos(retornos_bps: list[int]) -> Momentos:
    """Média, desvio, assimetria e curtose de uma série de retornos.

    Desvio **amostral** (`n-1`): a série é uma amostra de um processo, não a
    população dele. Usar `n` subestimaria a variância e portanto inflaria o
    Sharpe — erro na direção de aprovar.

    Curtose **bruta**, não excesso. A fórmula do DSR de Bailey e López de
    Prado usa `γ4` com normal = 3, e subtrair 3 aqui faria o termo
    `(γ4 - 1)/4` valer o que não devia. Um número que muda de convenção no
    meio do caminho é o padrão que este projeto já registrou oito vezes — por
    isso está no nome e no comentário.
    """
    n = len(retornos_bps)
    if n < 4:
        raise AmostraCurta(
            f"{n} observações não bastam: assimetria pede 3 e curtose pede 4."
            " Devolver zero seria afirmar simetria e normalidade que ninguém"
            " mediu"
        )
    media = sum(retornos_bps) / n
    desvios = [x - media for x in retornos_bps]
    var = sum(d * d for d in desvios) / (n - 1)
    desvio = math.sqrt(var)
    if desvio == 0:
        # Série constante: Sharpe é indefinido, não zero. Zero afirmaria
        # "sem vantagem"; indefinido é "não há variação a partir da qual
        # falar de risco".
        return Momentos(
            n=n, media=media, desvio=0.0, assimetria=0.0, curtose=3.0
        )
    m3 = sum(d**3 for d in desvios) / n
    m4 = sum(d**4 for d in desvios) / n
    sigma_pop = math.sqrt(sum(d * d for d in desvios) / n)
    return Momentos(
        n=n,
        media=media,
        desvio=desvio,
        assimetria=m3 / sigma_pop**3,
        curtose=m4 / sigma_pop**4,
    )


def anos_de_observacao(n: int, duracao_barra_ms: int) -> float:
    return n / barras_por_ano(duracao_barra_ms)
