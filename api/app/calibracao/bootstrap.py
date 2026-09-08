"""Intervalo do quantil sob dependencia. ADR 0027, secao 4a.

A calibracao decide por `p10(E2)`, e a revalidacao exige **o limite inferior**
do IC unilateral de 95% desse quantil - nao o ponto. Este modulo produz esse
limite.

## Por que bootstrap, e nao a formula fechada

A variancia assintotica de um quantil amostral e `p(1-p) / (n f(q_p)^2)`, e
para a normal em p = 0,10 isso da `K = 2,92` sobre a variancia da media. **Mas
`K = 2,92` vale SOB NORMALIDADE**, e erro de execucao e assimetrico e de cauda
pesada - que e a forma exata em que a aproximacao normal erra mais.

Entao `K = 2,92` serve **so para dimensionar o piloto**, com a hipotese
declarada como hipotese, e o intervalo de verdade vem do **block bootstrap
estacionario** (Politis-Romano), que nao supoe distribuicao nenhuma e ja
carrega a dependencia.

**Uma versao anterior da derivacao usou `K = 1,71`**, que e a raiz de 2,92 - a
razao de ERROS-PADRAO onde vai a de VARIANCIAS. Subestimava a amostra em 71%,
e fica registrado porque o erro estava numa conta e nao numa frase.

## Onde a verificacao para, e eu digo que para

Implemento Politis-White (2004) **pelas formulas publicadas**, termo a termo, e
confiro por PROPRIEDADES: ruido branco da bloco curto, dependencia forte da
bloco longo, e o comprimento cresce com a persistencia. **Nao afirmo reproduzir
uma tabela numerica do artigo** - nao o tenho a mao, e dizer "confere com o
publicado" sem ter conferido seria a afirmacao inverificavel que este projeto
recusa. E o mesmo limite que o DSR declara em `app/estatistica/dsr.py`.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import TypeVar

# Os valores que o ADR 0027 fixou.
Z_95_UNILATERAL = 1.645
Z_95_BILATERAL = 1.96
K_QUANTIL_P10 = 2.92
T = TypeVar("T", int, float)

REPETICOES = 2_000

# Semente propria, para que o limite inferior seja reproduzivel (regra 13).
# Um IC que muda a cada chamada nao e um IC - e um sorteio.
SEMENTE = 42


class SerieCurtaDemais(Exception):
    """Nao ha observacao suficiente para o procedimento pedido."""


# ===========================================================================
# Autocorrelacao
# ===========================================================================


def autocovariancias(x: list[float], max_lag: int) -> list[float]:
    """`R(k)` para k = 0..max_lag, com a media amostral removida."""
    n = len(x)
    media = sum(x) / n
    d = [v - media for v in x]
    return [
        sum(d[i] * d[i + k] for i in range(n - k)) / n
        for k in range(max_lag + 1)
    ]


def autocorrelacoes(x: list[float], max_lag: int) -> list[float]:
    r = autocovariancias(x, max_lag)
    if r[0] == 0:
        # Serie constante: nao ha o que correlacionar, e dividir daria ZeroDiv.
        # Devolver zeros e o certo - e o teste do incremento 17 lembra que
        # serie constante tem desvio-padrao ZERO, o que ja fez um teste passar
        # pelo motivo errado.
        return [1.0] + [0.0] * max_lag
    return [v / r[0] for v in r]


@dataclass(frozen=True)
class Tau:
    """O tempo de autocorrelacao integrado, e o diagnostico junto."""

    tau: float
    tau_bruto: float
    janela: int
    rho_1: float
    ar1_plausivel: bool
    desvio_max_geometrico: float

    @property
    def tau_ar1(self) -> float:
        """`(1+rho)/(1-rho)` - o caso AR(1) de tau, e SO ele.

        Usa-lo sem dizer que e um caso particular foi omissao da derivacao
        original, e o usuario a apontou. Fica aqui nomeado, e so dimensiona o
        piloto - nunca substitui a estimativa HAC.
        """
        if self.rho_1 >= 1:
            return float("inf")
        return (1 + self.rho_1) / (1 - self.rho_1)


def tau_hac(x: list[float]) -> Tau:
    """`tau = 1 + 2 sum(rho_k)`, por janela de Bartlett/Newey-West.

    A largura de janela e a regra padrao de Newey-West, `L = 4(n/100)^(2/9)`,
    **declarada** em vez de escolhida caso a caso - largura escolhida depois de
    ver o resultado e a quinta pergunta do teste de escopo aplicada a uma
    estatistica.

    `tau` abaixo de 1 e possivel com autocorrelacao negativa, e significa que a
    serie carrega MAIS informacao que observacoes independentes. Para
    dimensionar amostra isso seria uma folga que nao queremos apostar: usa-se
    `max(1, tau)`, e o valor bruto fica reportado ao lado.
    """
    n = len(x)
    if n < 3:
        raise SerieCurtaDemais(f"tau precisa de ao menos 3 pontos, veio {n}")

    janela = max(1, min(n - 1, int(4 * (n / 100) ** (2 / 9))))
    rho = autocorrelacoes(x, janela)
    soma = sum((1 - k / (janela + 1)) * rho[k] for k in range(1, janela + 1))
    bruto = 1 + 2 * soma

    # A HIPOTESE AR(1), declarada e VERIFICADA - o ADR exige as duas coisas.
    # Sob AR(1), `rho_k = rho_1^k`. O desvio maximo diz se ela se sustenta.
    desvio = 0.0
    if janela >= 2 and abs(rho[1]) < 1:
        desvio = max(
            abs(rho[k] - rho[1] ** k) for k in range(2, janela + 1)
        )

    return Tau(
        tau=max(1.0, bruto), tau_bruto=bruto, janela=janela,
        rho_1=rho[1] if janela >= 1 else 0.0,
        ar1_plausivel=desvio < 0.2,
        desvio_max_geometrico=desvio,
    )


# ===========================================================================
# Comprimento de bloco: Politis-White (2004)
# ===========================================================================


def _lambda_flat_top(t: float) -> float:
    """A janela de lag "flat-top" do artigo: 1 no centro, decai ate 1."""
    a = abs(t)
    if a <= 0.5:
        return 1.0
    if a <= 1.0:
        return 2 * (1 - a)
    return 0.0


def comprimento_de_bloco(x: list[float]) -> int:
    """`b_opt` do bootstrap ESTACIONARIO, pela regra de Politis-White.

    O ADR 0027 nomeia esta regra em vez de um numero, e a razao e a de sempre:
    um comprimento escolhido a mao e um parametro que alguem pode mexer depois
    de ver o intervalo.

    Os passos, na ordem do artigo:

      1. `m` = menor lag apos o qual `K_N` autocorrelacoes seguidas ficam
         dentro da banda `2 sqrt(log10(n)/n)`;
      2. `M = 2m`, limitado;
      3. `G = sum lambda(k/M) |k| R(k)` e `g0 = sum lambda(k/M) R(k)`,
         somados de `-M` a `M`;
      4. `D_SB = 2 g0^2`;
      5. `b = (2 G^2 / D_SB)^(1/3) n^(1/3)`, limitado a `min(3 sqrt(n), n/3)`.
    """
    n = len(x)
    if n < 4:
        return 1

    k_n = max(5, int(math.ceil(math.sqrt(math.log10(n)))))
    max_lag = min(n - 1, int(math.ceil(math.sqrt(n))) + k_n)
    rho = autocorrelacoes(x, max_lag)
    banda = 2 * math.sqrt(math.log10(n) / n)

    m = 0
    for candidato in range(1, max_lag + 1):
        seguintes = range(candidato + 1, min(candidato + k_n, max_lag) + 1)
        if seguintes and all(abs(rho[k]) < banda for k in seguintes):
            m = candidato
            break
    if m == 0:
        m = max_lag

    M = min(2 * m, n - 1)
    R = autocovariancias(x, M)

    # As somas correm de -M a M; `R(-k) = R(k)`, entao o termo k != 0 conta
    # duas vezes. Escrever assim, e nao "x2 no final", deixa a simetria
    # visivel para quem confere contra o artigo.
    g0 = R[0] + 2 * sum(_lambda_flat_top(k / M) * R[k] for k in range(1, M + 1))
    G = 2 * sum(_lambda_flat_top(k / M) * k * R[k] for k in range(1, M + 1))

    if g0 == 0:
        return 1
    d_sb = 2 * g0 ** 2
    if d_sb == 0:
        return 1

    b = ((2 * G ** 2) / d_sb) ** (1 / 3) * n ** (1 / 3)
    teto = max(1, int(min(3 * math.sqrt(n), n / 3)))
    return max(1, min(teto, int(round(b))))


# ===========================================================================
# O quantil, e o limite inferior dele
# ===========================================================================


def quantil(x: list[float], p: float) -> float:
    """Quantil por interpolacao linear, sobre a serie ORDENADA.

    Definicao unica no projeto: quantil tem muitas convencoes, e trocar de
    convencao no meio e o modo de falha silencioso do procedimento - a mesma
    licao que a curtose bruta contra a excedente ja deu em `dsr.py`.
    """
    if not x:
        raise SerieCurtaDemais("quantil de serie vazia")
    s = sorted(x)
    if len(s) == 1:
        return float(s[0])
    pos = p * (len(s) - 1)
    baixo = int(math.floor(pos))
    alto = min(baixo + 1, len(s) - 1)
    peso = pos - baixo
    return s[baixo] * (1 - peso) + s[alto] * peso


@dataclass(frozen=True)
class IntervaloDoQuantil:
    ponto: float
    limite_inferior: float
    n: int
    n_efetivo: float
    bloco: int
    repeticoes: int
    tau: Tau


def reamostrar_por_blocos(
    serie: list[T], n_saida: int, bloco: int, rng: random.Random
) -> list[T]:
    """Uma replica do bootstrap ESTACIONARIO de Politis-Romano.

    A cada passo, continua o bloco corrente com probabilidade `1 - 1/b` ou
    salta para um indice sorteado com probabilidade `1/b`, circularmente.
    **Blocos contiguos** e o que preserva a dependencia que o embaralhamento
    simples destruiria - e destruir a dependencia daria intervalo estreito
    demais, na direcao de aprovar.

    **Uma definicao, num lugar so.** Ela morava dentro de
    `limite_inferior_do_quantil` e o incremento 20 passou a precisar dela para
    calibrar o limiar do CUSUM. Reescrever o reamostrador la seria a forma
    exata do defeito que `ultimo_run_do_agente` ja custou: dois lugares com a
    mesma consulta, um deles consertado.

    Generica no tipo de propósito: a serie do quantil vem em `float` e a do
    CUSUM em `int` (milicents), e o reamostrador nao tem opiniao sobre isso.
    """
    prob_salto = 1.0 / max(1, bloco)
    n = len(serie)
    idx = rng.randrange(n)
    saida: list[T] = []
    for _ in range(n_saida):
        saida.append(serie[idx])
        if rng.random() < prob_salto:
            idx = rng.randrange(n)
        else:
            idx = (idx + 1) % n
    return saida


def limite_inferior_do_quantil(
    x: list[float], p: float = 0.10, *,
    confianca: float = 0.95,
    repeticoes: int = REPETICOES,
    semente: int = SEMENTE,
    bloco: int | None = None,
) -> IntervaloDoQuantil:
    """Limite inferior UNILATERAL do quantil, por bootstrap estacionario.

    **Unilateral de proposito.** A revalidacao pergunta uma coisa so - "o
    simulador e pessimista o bastante?" -, e para isso o que importa e o piso.
    Um intervalo bilateral responderia uma pergunta que ninguem fez e ainda
    apertaria o criterio sem motivo declarado.

    O reamostrador e o de Politis-Romano: a cada passo, continua o bloco
    corrente com probabilidade `1 - 1/b` ou salta para um indice sorteado com
    probabilidade `1/b`, circularmente. **Blocos contiguos** e o que preserva a
    dependencia que o embaralhamento simples destruiria - e destruir a
    dependencia daria um intervalo estreito demais, na direcao de aprovar.
    """
    n = len(x)
    if n < 10:
        raise SerieCurtaDemais(
            f"bootstrap de quantil precisa de ao menos 10 observacoes, veio {n}"
        )

    serie = [float(v) for v in x]
    t = tau_hac(serie)
    b = bloco if bloco is not None else comprimento_de_bloco(serie)

    rng = random.Random(semente)
    quantis: list[float] = []
    for _ in range(repeticoes):
        quantis.append(quantil(reamostrar_por_blocos(serie, n, b, rng), p))

    return IntervaloDoQuantil(
        ponto=quantil(serie, p),
        limite_inferior=quantil(quantis, 1 - confianca),
        n=n,
        n_efetivo=n / t.tau,
        bloco=b,
        repeticoes=repeticoes,
        tau=t,
    )


# ===========================================================================
# Dimensionamento do piloto
# ===========================================================================


@dataclass(frozen=True)
class TamanhoNecessario:
    n_efetivo: int
    n_bruto: int
    sigma_e: float
    delta: float
    tau: float
    z: float
    k: float


def tamanho_necessario(
    *, sigma_e: float, delta: float, tau: float,
    z: float = Z_95_BILATERAL, k: float = K_QUANTIL_P10,
) -> TamanhoNecessario:
    """`n_efetivo >= K (z sigma / delta)^2` e `n_bruto = n_efetivo * tau`.

    **`delta` NAO e relaxavel.** O usuario retirou essa saida ao fechar a D45:
    relaxar porque nao cabe na reserva e ajustar a regua ao CALENDARIO, e §8.3
    ja resolveu essa familia - o que nao cabe no horizonte e **arquivado, nao
    testado mal**. Um regime que nao alcancar o `n` permanece NAO CALIBRADO, e
    isso vira condicao de validade.

    A funcao nao conhece calendario nenhum, e e assim que ela fica incapaz de
    negociar consigo mesma.
    """
    if delta <= 0:
        raise ValueError("delta tem de ser positivo")
    if sigma_e < 0:
        raise ValueError("sigma_e nao pode ser negativo")
    tau = max(1.0, tau)
    n_ef = math.ceil(k * (z * sigma_e / delta) ** 2)
    return TamanhoNecessario(
        n_efetivo=n_ef, n_bruto=math.ceil(n_ef * tau),
        sigma_e=sigma_e, delta=delta, tau=tau, z=z, k=k,
    )


def desvio_padrao(x: list[float]) -> float:
    """Desvio-padrao amostral (n-1)."""
    n = len(x)
    if n < 2:
        raise SerieCurtaDemais(f"desvio-padrao precisa de 2 pontos, veio {n}")
    media = sum(x) / n
    return math.sqrt(sum((v - media) ** 2 for v in x) / (n - 1))
