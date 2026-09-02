"""Pre-processamento do periodo, em Python. **Nenhuma chamada de modelo.**

Criterio 5 do incremento 5 (secao 3.6, regras 4 e 5): as estatisticas sao
calculadas em codigo e o prompt recebe o RESUMO, nunca o log bruto. Mandar
56.064 barras para o modelo seria pagar por tokens para que ele faca, pior e
de forma nao reproduzivel, uma conta que o Python faz exata e de graca.

Tudo inteiro. Retorno e amplitude em **bps**, autocorrelacao em **milesimos**.
Nao ha ponto flutuante em lugar nenhum daqui: estes numeros entram num prompt
que decide uma regra que multiplica dinheiro, e a regra 5 nao abre excecao
para o caminho indireto.

O resumo inclui o **custo declarado de uma ida e volta**. Sem ele o modelo
opinaria sobre uma serie de precos sem saber quanto custa toca-la - e o
incremento 4 mostrou que e exatamente ai que o resultado e decidido: uma
barra de 15m tem amplitude mediana de 24 bps e uma ida e volta custa 28.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from ..config.schema import ExperimentConfig
from ..dataset.loader import BarraCarregada

BPS = 10_000
MILESIMOS = 1_000


def _bps(numerador: int, denominador: int) -> int:
    """Fracao em bps, truncada para zero. Sem float."""
    if denominador == 0:
        return 0
    return numerador * BPS // denominador


def _mediana(valores: Sequence[int]) -> int:
    """Mediana sem interpolacao: com n par, o menor dos dois centrais.

    Interpolar inventaria um valor que nao esta na amostra. Para um resumo
    que o modelo vai ler, o valor observado e mais honesto que a media de
    dois observados.
    """
    if not valores:
        return 0
    ordenados = sorted(valores)
    return ordenados[(len(ordenados) - 1) // 2]


def _desvio_padrao(valores: Sequence[int]) -> int:
    """Desvio padrao populacional, em inteiro, via raiz inteira.

    `math.sqrt` devolveria float. `Decimal.sqrt` seria exato mas caro; a raiz
    inteira de Newton basta para um resumo em bps e nao introduz binario.
    """
    n = len(valores)
    if n < 2:
        return 0
    media = sum(valores) // n
    variancia = sum((v - media) ** 2 for v in valores) // n
    return _raiz_inteira(variancia)


def _raiz_inteira(n: int) -> int:
    if n <= 0:
        return 0
    x = n
    y = (x + 1) // 2
    while y < x:
        x = y
        y = (x + n // x) // 2
    return x


def _utc(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="minutes")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class ResumoDePeriodo:
    """O que o cerebro lento sabe sobre a janela. E so isto.

    Deliberadamente pequeno e deliberadamente fechado: um resumo que cresce
    a cada ideia vira o log bruto de novo, e o criterio 5 deixa de valer sem
    que nada acuse.
    """

    barras: int
    de_utc: str
    ate_utc: str
    de_ms: int
    ate_ms: int

    retorno_periodo_bps: int
    amplitude_mediana_bps: int
    amplitude_media_bps: int
    volatilidade_por_barra_bps: int
    drawdown_maximo_bps: int
    barras_de_alta_bps: int
    autocorrelacao_lag1_milesimos: int

    custo_ida_e_volta_bps: int
    barras_por_dia: int

    def como_dict(self) -> dict:
        return {
            "barras": self.barras,
            "de_utc": self.de_utc,
            "ate_utc": self.ate_utc,
            "retorno_periodo_bps": self.retorno_periodo_bps,
            "amplitude_mediana_bps": self.amplitude_mediana_bps,
            "amplitude_media_bps": self.amplitude_media_bps,
            "volatilidade_por_barra_bps": self.volatilidade_por_barra_bps,
            "drawdown_maximo_bps": self.drawdown_maximo_bps,
            "barras_de_alta_bps": self.barras_de_alta_bps,
            "autocorrelacao_lag1_milesimos": self.autocorrelacao_lag1_milesimos,
            "custo_ida_e_volta_bps": self.custo_ida_e_volta_bps,
            "barras_por_dia": self.barras_por_dia,
        }

    def como_texto(self) -> str:
        """O bloco que vai para o prompt. Uma linha por estatistica.

        Formato fixo e ordem fixa: qualquer variacao aqui invalida o prefixo
        de cache e o criterio 6 comeca a falhar sem causa aparente.
        """
        return "\n".join(
            (
                f"barras observadas: {self.barras} "
                f"({self.de_utc} a {self.ate_utc}, {self.barras_por_dia}/dia)",
                f"retorno do periodo: {self.retorno_periodo_bps} bps",
                f"amplitude por barra: mediana {self.amplitude_mediana_bps} bps, "
                f"media {self.amplitude_media_bps} bps",
                f"volatilidade dos retornos por barra: "
                f"{self.volatilidade_por_barra_bps} bps",
                f"drawdown maximo no fechamento: {self.drawdown_maximo_bps} bps",
                f"barras de alta: {self.barras_de_alta_bps} bps do total",
                f"autocorrelacao lag-1 dos retornos: "
                f"{self.autocorrelacao_lag1_milesimos} milesimos",
                f"custo declarado de uma ida e volta: "
                f"{self.custo_ida_e_volta_bps} bps",
            )
        )


def custo_ida_e_volta_bps(config: ExperimentConfig) -> int:
    """Quanto uma ida e volta custa, em bps, so pelos custos declarados.

    Derivado da config, nunca constante: se as taxas mudarem e este numero
    nao mudar junto, o resumo passa a mentir para o modelo sobre o unico
    parametro que decide se operar compensa.

    Nao inclui o efeito da referencia de execucao - com `abertura` ele e
    zero, e com `limite_adverso` depende da amplitude de cada barra, que nao
    e um parametro e sim uma propriedade do dado.
    """
    por_perna = (
        config.taker_fee_bps
        + config.spread_bps / Decimal(2)
        + config.slippage_bps
        + config.penalty_bps
    )
    return int((por_perna * 2).to_integral_value())


def resumir(
    barras: Sequence[BarraCarregada], config: ExperimentConfig
) -> ResumoDePeriodo:
    """Estatisticas do periodo. Deterministico: mesmas barras, mesmo resumo."""
    if len(barras) < 2:
        raise ValueError("resumo exige pelo menos duas barras")

    fechamentos = [b.close for b in barras]
    retornos = [
        _bps(fechamentos[i] - fechamentos[i - 1], fechamentos[i - 1])
        for i in range(1, len(fechamentos))
    ]
    amplitudes = [_bps(b.high - b.low, b.open) for b in barras]

    pico = fechamentos[0]
    drawdown = 0
    for preco in fechamentos:
        pico = max(pico, preco)
        drawdown = max(drawdown, _bps(pico - preco, pico))

    intervalo_ms = barras[1].open_time_ms - barras[0].open_time_ms

    return ResumoDePeriodo(
        barras=len(barras),
        de_utc=_utc(barras[0].open_time_ms),
        ate_utc=_utc(barras[-1].open_time_ms),
        de_ms=barras[0].open_time_ms,
        ate_ms=barras[-1].open_time_ms,
        retorno_periodo_bps=_bps(fechamentos[-1] - fechamentos[0], fechamentos[0]),
        amplitude_mediana_bps=_mediana(amplitudes),
        amplitude_media_bps=sum(amplitudes) // len(amplitudes),
        volatilidade_por_barra_bps=_desvio_padrao(retornos),
        drawdown_maximo_bps=drawdown,
        barras_de_alta_bps=_bps(sum(1 for r in retornos if r > 0), len(retornos)),
        autocorrelacao_lag1_milesimos=_autocorrelacao(retornos),
        custo_ida_e_volta_bps=custo_ida_e_volta_bps(config),
        barras_por_dia=86_400_000 // intervalo_ms if intervalo_ms else 0,
    )


def _autocorrelacao(retornos: Sequence[int]) -> int:
    """Autocorrelacao de defasagem 1, em milesimos.

    E a estatistica que separa as tres familias do catalogo: positiva sugere
    continuidade (cruzamento de medias, rompimento), negativa sugere reversao
    (banda de desvio). Perto de zero nao sugere nenhuma - e essa tambem e uma
    resposta, e a mais provavel.
    """
    n = len(retornos)
    if n < 3:
        return 0
    media = sum(retornos) // n
    numerador = sum(
        (retornos[i] - media) * (retornos[i - 1] - media) for i in range(1, n)
    )
    denominador = sum((r - media) ** 2 for r in retornos)
    if denominador == 0:
        return 0
    return numerador * MILESIMOS // denominador
