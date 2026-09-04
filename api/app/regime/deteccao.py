"""Detector de regimes de mercado. Implementa o ADR 0026, sem reinterpretar.

Tres regimes de VOLATILIDADE, pelos tercis historicos, com os cortes
CONGELADOS antes do forward. Duracao minima de sete dias CONSECUTIVOS.

**A direcao nao esta aqui, e a ausencia e o achado.** Eu havia proposto uma
grade direcao x volatilidade, seis celulas. A medicao sobre as 70.080 barras
derrubou: a celula conjunta herda a persistencia da dimensao MENOS persistente,
e direcao em janela de uma semana a 15 min vira a cada 2,4 dias - contra 10,1
dias da volatilidade sozinha. Incluir direcao destruia o unico eixo que
funcionava.

A direcao continua sendo registrada em `condicoes_validade` (o pre-registro),
mas **nao conta** para a cobertura. Quem quiser a serie dela tem de calcula-la;
este modulo nao a produz, para que ninguem a use por engano na contagem.

Tres propriedades que este modulo impoe, e cada uma fecha um modo de falha:

1. **A janela e CAUSAL.** O regime de `t` usa so barras fechadas ESTRITAMENTE
   antes de `t`. Nenhuma janela centrada - seria olhar para frente, e e a
   proibicao de secao 11.4 aplicada ao proprio detector.

2. **Nao le resultado de estrategia nenhuma.** Um classificador que olhasse o
   P&L acharia "regime novo" exatamente onde a estrategia mudou de
   comportamento, que e circular.

3. **Retorno que atravessa lacuna nao entra.** Vem `None` do loader, pela
   subsecao 3b do ADR, e a janela que perde demais sai `INDEFINIDO`.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Literal

from ..dataset import loader

Regime = Literal["vol_baixa", "vol_media", "vol_alta"]
INDEFINIDO = None

# ---------------------------------------------------------------------------
# OS NUMEROS CONGELADOS DO ADR 0026
#
# Eles NAO sao recalculados em producao. `derivar_cortes` existe para PROVAR a
# procedencia deles sobre o historico, e ha teste conferindo que a derivacao
# reproduz estes valores - mas o detector usa os congelados.
#
# O motivo esta no proprio ADR: "cortes congelados antes do forward". Um
# detector que recalculasse os tercis a cada chamada adaptaria a regua ao dado
# que fosse chegando, e "dois regimes distintos" viraria automatico.
# ---------------------------------------------------------------------------

# Tercis da volatilidade historica, em MILI-bps por barra (bps x 1000).
CORTE_INFERIOR_MILI_BPS = 19_300   # 19,3 bps - fronteira baixa/media
CORTE_SUPERIOR_MILI_BPS = 25_300   # 25,3 bps - fronteira media/alta

# Janela de classificacao: 672 barras de 15 min = exatamente 7 dias.
#
# E a MENOR janela sobre o plato medido: de 672 para cima a cobertura e plana
# (~30% aos 30 dias), de 672 para baixo desaba (480 -> 9,4%). O ADR declara
# que ela senta na borda: mexer para baixo muda o criterio, nao o afina.
JANELA_BARRAS = 672

# Permanencia minima para um episodio CONTAR: 7 dias consecutivos.
#
# Consecutivos, e nao acumulados - a leitura acumulada daria 67,4% de cobertura
# aos 30 dias contra 31,0% da adotada, mais que o dobro. Sem esta linha, duas
# implementacoes honestas do mesmo ADR dariam vereditos opostos.
PERMANENCIA_BARRAS = 672

# Cobertura minima para sair da quarentena (secao 8.5). Travamento INDEPENDENTE
# de amostra: nenhum caminho promove com um regime so.
REGIMES_MINIMOS = 2

# Tolerancia de lacuna na janela, do item 3 do ADR: >= 99% dos retornos
# presentes, e nenhum buraco continuo passando de 4 barras (1 hora).
FRACAO_MINIMA_PRESENTE = 0.99
BURACO_MAXIMO_BARRAS = 4


class DatasetSemGrade(Exception):
    """Barras insuficientes para uma janela cheia."""


@dataclass(frozen=True)
class Classificacao:
    """O regime de um instante, e por que ele e o que e."""

    open_time_ms: int
    regime: Regime | None
    vol_mili_bps: int | None
    retornos_usados: int
    retornos_ausentes: int
    motivo: str | None      # preenchido so quando o regime e INDEFINIDO


@dataclass(frozen=True)
class Episodio:
    """Corrida consecutiva no mesmo regime, com duracao >= PERMANENCIA_BARRAS."""

    regime: Regime
    inicio_ms: int
    fim_ms: int
    barras: int


def _classificar_vol(vol_mili_bps: int) -> Regime:
    if vol_mili_bps < CORTE_INFERIOR_MILI_BPS:
        return "vol_baixa"
    if vol_mili_bps > CORTE_SUPERIOR_MILI_BPS:
        return "vol_alta"
    return "vol_media"


def _maior_buraco(presentes: list[bool]) -> int:
    maior = corrido = 0
    for p in presentes:
        corrido = 0 if p else corrido + 1
        maior = max(maior, corrido)
    return maior


def _desvio_padrao_mili_bps(valores: list[int]) -> int:
    """Desvio-padrao populacional, devolvido em mili-bps inteiros.

    Inteiro na saida porque o valor e comparado com limiar CONGELADO, e ponto
    flutuante faz duas maquinas discordarem na ultima casa. A conta interna e
    em float por precisao; o arredondamento e no fim, uma vez.
    """
    n = len(valores)
    media = sum(valores) / n
    var = sum((v - media) ** 2 for v in valores) / n
    return int(round(math.sqrt(max(0.0, var))))


def classificar_serie(
    retornos: list[tuple[int, int | None]],
    *,
    janela: int = JANELA_BARRAS,
) -> list[Classificacao]:
    """Classifica cada instante a partir da janela CAUSAL que o antecede.

    `retornos[i]` e o retorno que TERMINA na barra `i`. A janela de `t` sao os
    retornos de indice `t-janela` a `t-1` - nenhum deles inclui a barra `t`,
    que no instante da decisao ainda nao fechou.
    """
    saida: list[Classificacao] = []
    for t in range(len(retornos)):
        ms = retornos[t][0]
        ini = t - janela
        if ini < 0:
            saida.append(Classificacao(ms, None, None, 0, 0, "janela_incompleta"))
            continue

        fatia = retornos[ini:t]
        validos = [r for _, r in fatia if r is not None]
        presentes = [r is not None for _, r in fatia]
        ausentes = len(fatia) - len(validos)

        if len(validos) < janela * FRACAO_MINIMA_PRESENTE:
            saida.append(Classificacao(
                ms, None, None, len(validos), ausentes, "lacuna_acima_da_tolerancia"))
            continue
        if _maior_buraco(presentes) > BURACO_MAXIMO_BARRAS:
            saida.append(Classificacao(
                ms, None, None, len(validos), ausentes, "buraco_continuo_longo"))
            continue
        if len(validos) < 2:
            saida.append(Classificacao(
                ms, None, None, len(validos), ausentes, "retornos_insuficientes"))
            continue

        vol = _desvio_padrao_mili_bps(validos)
        saida.append(Classificacao(
            ms, _classificar_vol(vol), vol, len(validos), ausentes, None))
    return saida


def episodios(
    classificacoes: Iterable[Classificacao],
    *,
    permanencia: int = PERMANENCIA_BARRAS,
) -> list[Episodio]:
    """Corridas CONSECUTIVAS no mesmo regime, com duracao >= `permanencia`.

    Corridas menores sao descartadas: sao a "variacao cosmetica" que secao 19.2
    manda nao contar como regime novo.

    **Um `INDEFINIDO` quebra a corrida.** Ele nao e ponte entre dois trechos do
    mesmo regime - se nao se observou, nao se afirma continuidade.
    """
    itens = list(classificacoes)
    saida: list[Episodio] = []
    i = 0
    while i < len(itens):
        r = itens[i].regime
        if r is None:
            i += 1
            continue
        j = i
        while j < len(itens) and itens[j].regime == r:
            j += 1
        if j - i >= permanencia:
            saida.append(Episodio(r, itens[i].open_time_ms, itens[j - 1].open_time_ms, j - i))
        i = j
    return saida


def cobertura(
    classificacoes: Iterable[Classificacao],
    *,
    permanencia: int = PERMANENCIA_BARRAS,
    minimo: int = REGIMES_MINIMOS,
) -> dict:
    """Quantos regimes distintos foram cobertos, e se a exigencia foi cumprida.

    O `cumprida` aqui NAO promove nada - quem decide saida de quarentena e o
    validador. Este modulo diz o fato; a consequencia e de quem tem
    autoridade.
    """
    eps = episodios(classificacoes, permanencia=permanencia)
    distintos = sorted({e.regime for e in eps})
    return {
        "regimes_cobertos": distintos,
        "quantidade": len(distintos),
        "minimo_exigido": minimo,
        "cumprida": len(distintos) >= minimo,
        "episodios": len(eps),
        "permanencia_barras": permanencia,
    }


def derivar_cortes(retornos: list[tuple[int, int | None]],
                   *, janela: int = JANELA_BARRAS) -> tuple[int, int]:
    """Recalcula os tercis a partir de uma serie. **NAO usada em producao.**

    Existe para provar a PROCEDENCIA dos cortes congelados: ha teste
    conferindo que, sobre o dataset historico, esta derivacao reproduz
    `CORTE_INFERIOR_MILI_BPS` e `CORTE_SUPERIOR_MILI_BPS`.

    O detector usa os congelados, e nao esta funcao. Recalcular em producao
    adaptaria a regua ao dado que fosse chegando, e "dois regimes distintos"
    viraria automatico - que e a definicao frouxa que secao 19.2 alerta.
    """
    vols: list[int] = []
    for t in range(janela, len(retornos)):
        validos = [r for _, r in retornos[t - janela:t] if r is not None]
        if len(validos) >= janela * FRACAO_MINIMA_PRESENTE and len(validos) >= 2:
            vols.append(_desvio_padrao_mili_bps(validos))
    if len(vols) < 3:
        raise DatasetSemGrade(
            f"{len(vols)} janelas cheias: nao ha como derivar tercis"
        )
    vols.sort()

    def q(p: float) -> int:
        k = (len(vols) - 1) * p
        lo, hi = math.floor(k), math.ceil(k)
        if lo == hi:
            return vols[int(k)]
        return int(round(vols[lo] + (vols[hi] - vols[lo]) * (k - lo)))

    return q(1 / 3), q(2 / 3)


def do_dataset(
    conn: sqlite3.Connection,
    dataset_id: int,
    *,
    de_ms: int | None = None,
    ate_ms: int | None = None,
) -> list[Classificacao]:
    """Classifica um intervalo do dataset.

    Le pelo `loader`, e nunca com `SELECT ... FROM bar` proprio: a fronteira do
    incremento 9 nao tem excecao, e o validador ja tentou fura-la duas vezes.

    **A janela causal precisa de 672 barras ANTES de `de_ms`**, entao a leitura
    comeca antes do intervalo pedido e as classificacoes anteriores a ele sao
    descartadas. Sem isso, os primeiros 7 dias de qualquer intervalo sairiam
    `janela_incompleta` - e um forward de 30 dias perderia um quarto da
    cobertura por artefato de borda.
    """
    if de_ms is None:
        recuo_ms = None
    else:
        from ..dataset.binance import intervalo_ms

        passo = intervalo_ms(loader.metadados(conn, dataset_id).timeframe)
        recuo_ms = de_ms - (JANELA_BARRAS + 1) * passo

    serie = loader.retornos_causais(conn, dataset_id, de_ms=recuo_ms, ate_ms=ate_ms)
    todas = classificar_serie(serie)
    if de_ms is None:
        return todas
    return [c for c in todas if c.open_time_ms >= de_ms]
