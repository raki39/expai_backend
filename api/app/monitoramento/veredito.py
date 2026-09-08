"""O estado do monitor, derivado. PURO: sem conexao, sem `sqlite3`.

ADR 0035. Cinco estados, e a ordem em que sao testados e o desenho:

    sem_conhecimento_em_uso   nao ha o que monitorar        <- a 0C mora aqui
    indisponivel_por_dados    a cobertura violou a D40
    invalidado                cruzou o limiar critico
    em_suspeita               cruzou o limiar de alerta
    sem_alarme                rodou, nao cruzou

**`indisponivel_por_dados` vem ANTES dos alarmes**, e nao depois. Com cobertura
insuficiente nem o alarme nem a ausencia dele significam o que dizem: o CUSUM
pulou barras, entao ele viu menos horizonte do que o limiar foi calibrado para
ver. Ler "invalidado" sobre metade dos dados seria afirmar mais do que se mediu.

## A garantia que o usuario exigiu por escrito

> O CUSUM pode detectar degradacao e interromper ou degradar uma execucao. A
> **ausencia de alarme NAO comprova edge e NUNCA promove candidata.**

Ela e aritmetica, e nao prudencia: nao alarmar e o comportamento de um teste
insensivel **e** o de uma estrategia que funciona, e neste horizonte a primeira
explicacao e muito mais provavel que a segunda. Por isso `sem_alarme` carrega o
proprio desmentido num campo (`comprova_edge`, sempre `False`), e por isso ele
nao entra nos seis requisitos de saida da quarentena - com guarda de codigo
varrendo `app/quarentena/veredito.py` para que continue assim.

Um campo, e nao uma frase: frase sobrevive a regressao sem mudar uma letra.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..regime.deteccao import FRACAO_MINIMA_PRESENTE
from . import cusum

SEM_CONHECIMENTO = "sem_conhecimento_em_uso"
INDISPONIVEL = "indisponivel_por_dados"
SEM_ALARME = "sem_alarme"
EM_SUSPEITA = "em_suspeita"
INVALIDADO = "invalidado"

ESTADOS: tuple[str, ...] = (
    SEM_CONHECIMENTO, INDISPONIVEL, SEM_ALARME, EM_SUSPEITA, INVALIDADO,
)

# Os dois que sao transicao na maquina de §8.1. Os outros tres sao estados DO
# MONITOR, e nao do conhecimento - `sem_alarme` nao e um estado de hipotese, e
# promove-lo a um seria dar a "nao alarmou" o peso de um veredito.
ESTADOS_DA_MAQUINA: frozenset[str] = frozenset({EM_SUSPEITA, INVALIDADO})

# A tolerancia da D40, no seu proprio nivel (ADR 0026, §3b): "um episodio so
# conta se >= 99% das suas barras existem e nenhum buraco continuo passa de 1
# hora".
#
# **IMPORTADA, e nao copiada.** Ela ja existe em `app/regime/deteccao.py`, e
# duas copias do mesmo numero divergem - o projeto conta essa historia com
# `condicoes_da_config`, com `BLOCOS` e com a consulta do ultimo run do agente.
# Aqui ela e so convertida para ppm, porque o monitor conta em inteiros.
COBERTURA_MINIMA_PPM = int(round(FRACAO_MINIMA_PRESENTE * 1_000_000))

# Uma hora, em ms. Nao e um segundo limiar: e a UNIDADE em que o de cima foi
# escrito. `BURACO_MAXIMO_BARRAS` vale 4 em `deteccao.py` porque 4 barras de 15
# min sao uma hora - e ha teste conferindo essa igualdade contra o timeframe
# configurado, para que trocar de timeframe quebre a suite em vez de deixar o
# 4 descrevendo outra coisa em silencio.
LACUNA_MAXIMA_MS = 60 * 60 * 1_000


def lacuna_maxima_em_barras(duracao_barra_ms: int) -> int:
    """Quantas barras cabem na tolerancia de 1 hora da D40.

    Derivado da duracao da barra, e nao lido do `BURACO_MAXIMO_BARRAS = 4`:
    aquele 4 e o valor a 15 minutos, e o monitor nao tem por que herdar um
    numero que para de descrever se o timeframe mudar. O teste que compara os
    dois e quem mantem as duas leituras amarradas.
    """
    if duracao_barra_ms <= 0:
        raise ValueError("duracao de barra precisa ser positiva")
    return max(1, LACUNA_MAXIMA_MS // duracao_barra_ms)


@dataclass(frozen=True)
class Limiares:
    alerta_milicents: int
    critico_milicents: int
    prob_alerta_ppm: int
    prob_critico_ppm: int


@dataclass(frozen=True)
class Veredito:
    estado: str
    motivo: str
    # `True` so quando o monitor de fato rodou sobre dado suficiente. Nao e o
    # mesmo que "nao alarmou": e "a pergunta pode ser feita".
    mediu: bool
    cruzou_alerta: bool
    cruzou_critico: bool
    cobertura_ppm: int
    barras_puladas: int
    maior_lacuna: int
    barras_avaliadas: int
    maximo_milicents: int

    @property
    def comprova_edge(self) -> bool:
        """SEMPRE `False`. E propriedade, e nao campo, para nao ser gravavel.

        Nenhum estado deste monitor comprova edge - nem `sem_alarme`, que e o
        unico que alguem poderia querer ler assim.
        """
        return False

    @property
    def promove_candidata(self) -> bool:
        """SEMPRE `False`. Mesmo motivo."""
        return False

    def como_dict(self) -> dict:
        return {
            "estado": self.estado,
            "motivo": self.motivo,
            "mediu": self.mediu,
            "cruzou_alerta": self.cruzou_alerta,
            "cruzou_critico": self.cruzou_critico,
            "cobertura_ppm": self.cobertura_ppm,
            "barras_puladas": self.barras_puladas,
            "maior_lacuna": self.maior_lacuna,
            "barras_avaliadas": self.barras_avaliadas,
            "maximo_milicents": self.maximo_milicents,
            "comprova_edge": self.comprova_edge,
            "promove_candidata": self.promove_candidata,
        }


def sem_conhecimento_em_uso(motivo: str) -> Veredito:
    """O estado da 0C. Derivado da ausencia de hipotese validada, nunca digitado.

    §8.8 monitora *"conhecimento em uso"*. Na 0C nao ha nenhum: o Portao B
    rejeitou a unica candidata e o ADR 0034 decidiu que nenhuma entra no
    forward. Entao o monitor **nao tem sujeito**, e dizer isso e diferente de
    dizer que ele rodou e nada aconteceu.
    """
    return Veredito(
        estado=SEM_CONHECIMENTO, motivo=motivo, mediu=False,
        cruzou_alerta=False, cruzou_critico=False,
        cobertura_ppm=0, barras_puladas=0, maior_lacuna=0,
        barras_avaliadas=0, maximo_milicents=0,
    )


def avaliar(
    serie: cusum.Serie,
    lim: Limiares,
    *,
    duracao_barra_ms: int,
) -> Veredito:
    """O estado, a partir da serie acumulada e dos limiares CONGELADOS.

    **Nao recebe conexao e nao consegue recalcular nada.** Os limiares entram
    como argumento porque quem os le do banco e `monitor.py`, e a assinatura e
    a garantia: nao ha como esta funcao ir buscar um limiar mais conveniente.
    """
    if lim.critico_milicents < lim.alerta_milicents:
        raise ValueError(
            "limiares nao aninhados: cruzar o critico tem de implicar ter"
            " cruzado o alerta"
        )

    achados = dict(cusum.cruzamentos(
        serie, alerta=lim.alerta_milicents, critico=lim.critico_milicents,
    ))
    cruzou_alerta = cusum.ALERTA in achados
    cruzou_critico = cusum.CRITICO in achados

    base = dict(
        cobertura_ppm=serie.cobertura_ppm,
        barras_puladas=serie.barras_puladas,
        maior_lacuna=serie.maior_lacuna,
        barras_avaliadas=serie.avaliadas,
        maximo_milicents=serie.maximo_milicents,
    )

    teto_lacuna = lacuna_maxima_em_barras(duracao_barra_ms)
    if serie.avaliadas == 0:
        return Veredito(
            estado=INDISPONIVEL,
            motivo="nenhuma barra avaliada: nao ha o que ler, nem alarme nem"
                   " ausencia dele",
            mediu=False, cruzou_alerta=False, cruzou_critico=False, **base,
        )
    if serie.cobertura_ppm < COBERTURA_MINIMA_PPM:
        return Veredito(
            estado=INDISPONIVEL,
            motivo=(
                f"cobertura {serie.cobertura_ppm} ppm abaixo do minimo"
                f" {COBERTURA_MINIMA_PPM} ppm da D40: o monitor viu menos"
                " horizonte do que o limiar foi calibrado para ver, e"
                " ausencia de alarme aqui nao e lida como nada"
            ),
            mediu=False, cruzou_alerta=cruzou_alerta,
            cruzou_critico=cruzou_critico, **base,
        )
    if serie.maior_lacuna > teto_lacuna:
        return Veredito(
            estado=INDISPONIVEL,
            motivo=(
                f"maior lacuna de {serie.maior_lacuna} barras acima do teto de"
                f" {teto_lacuna} (1 hora, derivada da duracao da barra): a"
                " tolerancia do episodio da D40 vale no seu proprio nivel"
            ),
            mediu=False, cruzou_alerta=cruzou_alerta,
            cruzou_critico=cruzou_critico, **base,
        )

    if cruzou_critico:
        p = achados[cusum.CRITICO]
        return Veredito(
            estado=INVALIDADO,
            motivo=(
                f"S = {p.s_milicents} milicents cruzou o limiar critico"
                f" {lim.critico_milicents} na barra {p.t_ms}; probabilidade"
                f" de cruzamento sob ausencia de degradacao:"
                f" {lim.prob_critico_ppm} ppm no horizonte"
            ),
            mediu=True, cruzou_alerta=True, cruzou_critico=True, **base,
        )
    if cruzou_alerta:
        p = achados[cusum.ALERTA]
        return Veredito(
            estado=EM_SUSPEITA,
            motivo=(
                f"S = {p.s_milicents} milicents cruzou o limiar de alerta"
                f" {lim.alerta_milicents} na barra {p.t_ms}; probabilidade"
                f" de cruzamento sob ausencia de degradacao:"
                f" {lim.prob_alerta_ppm} ppm no horizonte. Reteste em janela"
                " posterior e disjunta"
            ),
            mediu=True, cruzou_alerta=True, cruzou_critico=False, **base,
        )

    return Veredito(
        estado=SEM_ALARME,
        motivo=(
            f"maximo de {serie.maximo_milicents} milicents em"
            f" {serie.avaliadas} barras, abaixo do alerta"
            f" {lim.alerta_milicents}. Ausencia de alarme NAO comprova edge e"
            " NUNCA promove candidata: nao alarmar e tambem o comportamento de"
            " um teste insensivel neste horizonte"
        ),
        mediu=True, cruzou_alerta=False, cruzou_critico=False, **base,
    )
