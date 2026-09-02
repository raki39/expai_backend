"""Avaliacao da regra sobre a serie. Deterministica e sem ponto flutuante.

Duas escolhas que valem explicacao:

**Comparacao sem divisao.** Comparar duas medias moveis dividindo cada soma
pela sua janela trunca os dois lados de formas diferentes, e o cruzamento
passa a depender do resto da divisao. Comparando `soma_rapida * lenta` com
`soma_lenta * rapida` a relacao e exata, e o mesmo dado sempre produz o mesmo
cruzamento - que e o que a reprodutibilidade (R12) precisa.

**Sinal e EVENTO, nao estado.** "A media rapida esta acima" vale por centenas
de barras; "a media rapida acabou de cruzar para cima" acontece uma vez. A
regra emite o evento, e por isso a serie de sinais nao depende de o executor
lembrar de nada.

Antes de a janela minima estar cheia nao ha sinal nenhum - e isso e diferente
de sinal de ficar de fora. O indicador simplesmente ainda nao existe.
"""

from __future__ import annotations

from collections import deque
from enum import IntEnum
from math import isqrt
from typing import Sequence

from ..dataset.loader import BarraCarregada
from .schema import BandaDesvio, BreakoutCanal, CruzamentoMedias, Regra


class Sinal(IntEnum):
    NADA = 0
    ENTRAR = 1
    SAIR = 2


def avaliar(barras: Sequence[BarraCarregada], regra: Regra) -> list[Sinal]:
    """Serie de sinais, um por barra, na mesma ordem."""
    params = regra.params
    if isinstance(params, CruzamentoMedias):
        return _cruzamento(barras, params)
    if isinstance(params, BandaDesvio):
        return _banda(barras, params)
    if isinstance(params, BreakoutCanal):
        return _breakout(barras, params)
    raise ValueError(f"familia sem avaliador: {params!r}")


# ---------------------------------------------------------------------------


def _cruzamento(
    barras: Sequence[BarraCarregada], p: CruzamentoMedias
) -> list[Sinal]:
    sinais = [Sinal.NADA] * len(barras)
    r, l = p.rapida, p.lenta
    soma_r = soma_l = 0
    acima_antes: bool | None = None

    for i, barra in enumerate(barras):
        soma_r += barra.close
        soma_l += barra.close
        if i >= r:
            soma_r -= barras[i - r].close
        if i >= l:
            soma_l -= barras[i - l].close
        if i < l - 1:
            continue

        # soma_r/r > soma_l/l, sem dividir nenhum dos dois lados.
        acima = soma_r * l > soma_l * r
        if acima_antes is not None and acima != acima_antes:
            sinais[i] = Sinal.ENTRAR if acima else Sinal.SAIR
        acima_antes = acima

    return sinais


def _banda(barras: Sequence[BarraCarregada], p: BandaDesvio) -> list[Sinal]:
    """Reversao a media: entra abaixo da banda inferior, sai ao voltar a media."""
    sinais = [Sinal.NADA] * len(barras)
    n = p.periodo
    soma = soma_quadrados = 0
    abaixo_antes: bool | None = None
    acima_da_media_antes: bool | None = None

    for i, barra in enumerate(barras):
        c = barra.close
        soma += c
        soma_quadrados += c * c
        if i >= n:
            saiu = barras[i - n].close
            soma -= saiu
            soma_quadrados -= saiu * saiu
        if i < n - 1:
            continue

        # n^2 * variancia, exato em inteiros.
        variancia_escalada = n * soma_quadrados - soma * soma
        n_desvio = isqrt(variancia_escalada) if variancia_escalada > 0 else 0

        # c < media - k*desvio  <=>  c*n*1000 < soma*1000 - k*(n*desvio)
        abaixo = c * n * 1000 < soma * 1000 - p.desvios_milesimos * n_desvio
        acima_da_media = c * n > soma

        if abaixo_antes is not None and abaixo and not abaixo_antes:
            sinais[i] = Sinal.ENTRAR
        elif (
            acima_da_media_antes is not None
            and acima_da_media
            and not acima_da_media_antes
        ):
            sinais[i] = Sinal.SAIR

        abaixo_antes = abaixo
        acima_da_media_antes = acima_da_media

    return sinais


def _breakout(barras: Sequence[BarraCarregada], p: BreakoutCanal) -> list[Sinal]:
    """Rompimento do canal formado pelas `periodo` barras ANTERIORES.

    Anteriores, e nao incluindo a atual: um canal que inclui a propria barra
    nunca e rompido por ela, porque ela mesma define o extremo.
    """
    sinais = [Sinal.NADA] * len(barras)
    n = p.periodo
    # Deques monotonicos: maximo e minimo da janela em O(1) amortizado.
    maximos: deque[int] = deque()
    minimos: deque[int] = deque()

    for i, barra in enumerate(barras):
        if i >= n:
            if maximos and minimos:
                canal_alto = barras[maximos[0]].high
                canal_baixo = barras[minimos[0]].low
                if barra.close > canal_alto:
                    sinais[i] = Sinal.ENTRAR
                elif barra.close < canal_baixo:
                    sinais[i] = Sinal.SAIR

        while maximos and barras[maximos[-1]].high <= barra.high:
            maximos.pop()
        maximos.append(i)
        while minimos and barras[minimos[-1]].low >= barra.low:
            minimos.pop()
        minimos.append(i)

        if maximos[0] <= i - n:
            maximos.popleft()
        if minimos[0] <= i - n:
            minimos.popleft()

    return sinais


def stop_disparado(
    barra: BarraCarregada, preco_de_entrada: int, stop_loss_bps: int
) -> bool:
    """O fundo da barra rompeu o limite de perda?

    Usa o FUNDO, e nao o fechamento: se o preco esteve la dentro da barra, o
    limite foi rompido, mesmo que tenha voltado antes de fechar. Ignorar isso
    seria fingir que perdas intrabarra nao aconteceram.

    O que esta funcao NAO diz e por quanto a saida seria executada. Em
    fidelidade 1 nao ha como afirmar preenchimento no preco do limite; a saida
    segue o caminho pessimista normal, com latencia (ver `Regra.stop_loss_bps`).
    """
    limite = preco_de_entrada * (10_000 - stop_loss_bps) // 10_000
    return barra.low <= limite
