"""Os limiares, calibrados para a reserva FINITA. ADR 0035, ajustes 1 e 2.

## O que o usuario derrubou, e por que ele estava certo

Eu havia apresentado uma tabela de `ARL_0` como *"o numero que decide"*. A
correcao:

> *"ARL medio nao garante sozinho a probabilidade de alarme dentro desses 8.064
> passos."*

`ARL_0` e uma **media** de tempo ate o primeiro alarme. A pergunta da reserva e
sobre a **cauda de outra variavel** - o maximo que `S` atinge em 8.064 passos.
A conversao `P ~= 1 - exp(-N/ARL_0)` supoe alarmes aproximadamente Poisson, e
essa aproximacao e assintotica: num horizonte de 84 dias o transiente inicial
(`S_0 = 0`) pesa, e um CUSUM refletido em zero nao e um processo sem memoria.

## A calibracao, entao, e sobre a distribuicao do MAXIMO

    para cada uma de 2.000 replicas:
        reamostra por BLOCOS a serie in-sample congelada, por N barras
        roda o CUSUM com o MESMO alvo, a MESMA folga, a MESMA aritmetica
        guarda  M = max(S_t)  ao longo de TODO o horizonte
    h_alerta  = menor h com  P(M >= h) <= 0,10
    h_critico = menor h com  P(M >= h) <= 0,05

**E NAO um quantil.** A primeira versao usava `quantil(M, 0,90)` e ela tem um
buraco: a distribuicao do maximo de um CUSUM refletido tem ATOMO em zero, e
quando a serie congelada supera folgadamente o alvo o quantil 90 devolve 0 -
que nao e limiar, porque `S_0 = 0` cruzaria antes da primeira barra. Remendar
com `max(1, quantil)` produziria um monitor que alarma em qualquer barra ruim
isolada, **com 100% de falso alarme sob a aparencia de um orcamento de 10%**.
Ver `menor_limiar`.

**Os dois limiares sao aninhados POR CONSTRUCAO**, e nao por disciplina:
`menor_limiar` e monotona nao-crescente em `prob`, e o orcamento critico e
menor que o de alerta. `h_alerta <= h_critico` e propriedade da definicao.

## O limite do bootstrap, medido e declarado

H0 aqui e *"o forward se comporta como o in-sample CONGELADO"*, e nao *"vem da
mesma lei desconhecida"*: o bootstrap condiciona na amostra observada. Contra
realizacoes NOVAS da mesma lei, o limiar cruza mais que o nominal, porque a
cauda de uma amostra finita e truncada. Medido, com horizonte 1.000:

    2.000 barras calibrando  ->  61,3% cruzam
    8.000                    ->  13,3%
    21.024 (o in-sample real)->  13,3%
    60.000                   ->  10,8%

O caso ruim e a amostra curta, e ele nao e o nosso. O numero esta em
`app/relatorio/monitoramento.py` e num teste que o fixa.

## Tres coisas que este modulo nao faz

**Nao olha o forward.** A serie entra como argumento, e quem a monta e
`monitor.py` a partir do run congelado pelos sete campos do incremento 19. Este
modulo nao recebe conexao e nao tem como alcancar barra do forward.

**Nao escolhe bloco nem semente.** Politis-White para o bloco (a mesma regra do
ADR 0027, o mesmo codigo), semente 42, 2.000 repeticoes - e os tres vao
gravados junto com o limiar, porque R12 exige que ele seja reproduzivel entre
maquinas e porque um limiar sem procedencia nao e um limiar congelado, e um
numero.

**Nao recalibra.** Quem grava e `monitor.congelar`, e a tabela e imutavel por
gatilho.
"""

from __future__ import annotations

import bisect
import hashlib
import math
import random
from dataclasses import dataclass

from ..calibracao.bootstrap import (
    SerieCurtaDemais,
    comprimento_de_bloco,
    quantil,
    reamostrar_por_blocos,
)
from . import cusum

# Os dois orcamentos que o usuario fixou, em ppm, sobre o HORIZONTE FINITO.
#
# "alerta/em_suspeita: probabilidade de cruzamento sob ausencia de degradacao
#  <= 10% no horizonte; critico/invalidado: probabilidade de cruzamento <= 5%."
PROB_ALERTA_PPM = 100_000   # 10%
PROB_CRITICO_PPM = 50_000   # 5%

# O nome do procedimento, gravado ao lado do limiar. Trocar de procedimento sem
# trocar este texto faria o registro descrever uma calibracao que nao houve.
ALGORITMO = "maximo-cusum-bootstrap-estacionario-politis-romano@1"

REPETICOES = 2_000
SEMENTE = 42

# Minimo de observacoes para calibrar. O mesmo piso do bootstrap de quantil:
# abaixo dele o reamostrador devolve variacao que e do sorteio, e nao da serie.
SERIE_MINIMA = 10


@dataclass(frozen=True)
class Calibracao:
    alerta_milicents: int
    critico_milicents: int
    prob_alerta_ppm: int
    prob_critico_ppm: int
    horizonte_barras: int
    bloco: int
    repeticoes: int
    semente: int
    algoritmo: str
    serie_hash: str
    serie_n: int
    # A deriva media de `d_t` sob H0, em milicents por barra. Diagnostico, e
    # o mais importante deles: deriva POSITIVA significa que a serie que
    # calibrou nao alcancava o alvo declarado, e ai o limiar cresce com o
    # horizonte e o monitor fica honestamente surdo.
    deriva_h0_milicents_por_barra: int
    maximo_mediano_milicents: int

    def como_dict(self) -> dict:
        return {
            "alerta_milicents": self.alerta_milicents,
            "critico_milicents": self.critico_milicents,
            "prob_alerta_ppm": self.prob_alerta_ppm,
            "prob_critico_ppm": self.prob_critico_ppm,
            "horizonte_barras": self.horizonte_barras,
            "bloco": self.bloco,
            "repeticoes": self.repeticoes,
            "semente": self.semente,
            "algoritmo": self.algoritmo,
            "serie_hash": self.serie_hash,
            "serie_n": self.serie_n,
            "deriva_h0_milicents_por_barra": self.deriva_h0_milicents_por_barra,
            "maximo_mediano_milicents": self.maximo_mediano_milicents,
            "monitor_surdo_por_deriva": self.surdo_por_deriva,
        }

    @property
    def surdo_por_deriva(self) -> bool:
        """A serie que calibrou nao alcancava o alvo? Entao o monitor e surdo.

        Sob H0 a deriva e `E[d_t] = alvo - m - k`, com `m` a taxa media da
        serie congelada. Deriva positiva faz `max(S)` crescer com o horizonte,
        o quantil sobe junto, e o limiar fica tao alto que nada dispara.

        **Nao e defeito da calibracao: e leitura correta.** Ela esta dizendo
        "esta serie nunca demonstrou a taxa declarada, entao nao da para
        detectar que ela foi perdida". E `m >= alvo` e exatamente o que a
        admissao da quarentena exige (incremento 19) - a regua da quarentena e
        a sensibilidade do monitor sao a mesma condicao, vista de dois lados.
        """
        return self.deriva_h0_milicents_por_barra > 0


def hash_da_serie(serie_milicents: list[int]) -> str:
    """`sha256` da serie inteira, canonica.

    Grava-se o hash e nao a serie: 21.024 inteiros por linha de limiar seria
    duplicar o run congelado. O hash responde a pergunta que importa - "e a
    mesma serie?" - e responde por igualdade, nao por semelhanca.
    """
    corpo = ",".join(str(int(v)) for v in serie_milicents)
    return hashlib.sha256(corpo.encode("ascii")).hexdigest()


def arl0_exigido(*, prob: float, horizonte_barras: int) -> int:
    """`ARL_0` que a aproximacao de Poisson exigiria. **DIAGNOSTICO, e so.**

    `P ~= 1 - exp(-N/ARL_0)`  =>  `ARL_0 = -N / ln(1 - P)`.

    Fica no relatorio porque diz **quanto o teste e insensivel neste
    horizonte**, e essa informacao nao desaparece por ela ter deixado de ser o
    criterio. O que ela nao faz mais e decidir o limiar.
    """
    if not 0 < prob < 1:
        raise ValueError("probabilidade fora de (0,1)")
    if horizonte_barras <= 0:
        raise ValueError("horizonte precisa ser positivo")
    return int(round(-horizonte_barras / math.log(1 - prob)))


def tabela_arl0(horizonte_barras: int) -> list[dict]:
    """A tabela do ADR 0035, rotulada como diagnostico onde ela aparece."""
    return [
        {
            "orcamento_ppm": ppm,
            "arl0_exigido_barras": arl0_exigido(
                prob=ppm / 1_000_000, horizonte_barras=horizonte_barras
            ),
        }
        for ppm in (200_000, 100_000, 50_000, 10_000)
    ]


def menor_limiar(maximos: list[int], prob: float) -> int:
    """O MENOR `h >= 1` com `P(max >= h) <= prob` na distribuicao empirica.

    **Nao e um quantil, e a diferenca importa.** A distribuicao do maximo de um
    CUSUM refletido tem um ATOMO em zero - quando a serie in-sample supera
    folgadamente o alvo, a maioria das replicas termina com `max(S) = 0`. Um
    quantil 90 devolveria 0 nesse caso, e `h = 0` nao e limiar: `S_0 = 0`
    cruzaria antes da primeira barra. Corrigir isso com `max(1, quantil)`
    devolveria 1, e ai qualquer barra ruim isolada alarmaria - **um monitor com
    100% de falso alarme, sob a aparencia de um orcamento de 10%.**

    Medido: com serie de media 5.000 contra alvo 2.379 e folga 1.189, o quantil
    90 do maximo e zero e o limiar viraria 1; a fracao de replicas que cruzam 1
    passa de 30%. A definicao direta - o menor `h` cujo cruzamento cabe no
    orcamento - nao tem esse buraco, porque ela pergunta exatamente o que o
    usuario exigiu: *"probabilidade de cruzamento sob ausencia de degradacao"*.

    Conservadora nos empates: `floor(prob x n)` replicas podem cruzar, nunca
    mais. Errar para baixo do orcamento e a direcao de graca.
    """
    if not maximos:
        raise SerieCurtaDemais("nao ha replicas para calibrar limiar")
    if not 0 < prob < 1:
        raise ValueError("probabilidade fora de (0,1)")

    ordenados = sorted(maximos)
    n = len(ordenados)
    permitidas = int(prob * n)  # floor

    for h in [1] + [v + 1 for v in sorted(set(ordenados)) if v >= 1]:
        if n - bisect.bisect_left(ordenados, h) <= permitidas:
            return h
    return ordenados[-1] + 1


def calibrar(
    serie_milicents: list[int],
    *,
    alvo_milicents_por_barra: int,
    folga_milicents_por_barra: int,
    horizonte_barras: int,
    prob_alerta_ppm: int = PROB_ALERTA_PPM,
    prob_critico_ppm: int = PROB_CRITICO_PPM,
    repeticoes: int = REPETICOES,
    semente: int = SEMENTE,
    bloco: int | None = None,
) -> Calibracao:
    """Os dois limiares, dos quantis da distribuicao do maximo. PURO.

    `serie_milicents` e a serie por barra do run **congelado** - o observado
    in-sample, na mesma unidade em que o forward sera medido. Sem ela nao ha
    H0: "ausencia de degradacao" significa "continua se comportando como o
    in-sample congelado", e nada mais.
    """
    n = len(serie_milicents)
    if n < SERIE_MINIMA:
        raise SerieCurtaDemais(
            f"calibrar limiar precisa de ao menos {SERIE_MINIMA} observacoes,"
            f" veio {n}"
        )
    if horizonte_barras <= 0:
        raise ValueError("horizonte precisa ser positivo")
    if not 0 < prob_critico_ppm <= prob_alerta_ppm < 1_000_000:
        raise ValueError(
            "orcamentos precisam satisfazer 0 < critico <= alerta < 1"
            f", veio alerta={prob_alerta_ppm} critico={prob_critico_ppm}"
        )

    serie = [int(v) for v in serie_milicents]
    b = bloco if bloco is not None else comprimento_de_bloco(
        [float(v) for v in serie]
    )

    rng = random.Random(semente)
    maximos: list[float] = []
    for _ in range(repeticoes):
        replica = reamostrar_por_blocos(serie, horizonte_barras, b, rng)
        maximos.append(float(cusum.maximo_de_uma_replica(
            replica,
            alvo=alvo_milicents_por_barra,
            k=folga_milicents_por_barra,
        )))

    inteiros = [int(m) for m in maximos]
    alerta = menor_limiar(inteiros, prob_alerta_ppm / 1_000_000)
    critico = menor_limiar(inteiros, prob_critico_ppm / 1_000_000)

    # Aninhamento: `menor_limiar` e monotona nao-crescente em `prob`, e
    # `prob_critico <= prob_alerta` foi conferido acima. A assercao afirma a
    # consequencia; o banco tem o mesmo `CHECK`.
    if critico < alerta:  # pragma: no cover
        raise AssertionError(
            "limiares invertidos: isto e defeito de aritmetica, e nao um"
            " limiar valido"
        )

    media_obs = sum(serie) / n
    deriva = alvo_milicents_por_barra - media_obs - folga_milicents_por_barra

    return Calibracao(
        alerta_milicents=alerta,
        critico_milicents=critico,
        prob_alerta_ppm=prob_alerta_ppm,
        prob_critico_ppm=prob_critico_ppm,
        horizonte_barras=horizonte_barras,
        bloco=b,
        repeticoes=repeticoes,
        semente=semente,
        algoritmo=ALGORITMO,
        serie_hash=hash_da_serie(serie),
        serie_n=n,
        deriva_h0_milicents_por_barra=int(round(deriva)),
        maximo_mediano_milicents=int(round(quantil(maximos, 0.5))),
    )
