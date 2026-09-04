"""Deteccao de regimes de mercado (ADR 0026).

Modulo de REGUA, e nao de resultado: ele diz em que regime um instante estava,
e nao decide nada sobre promocao. Quem decide saida de quarentena e o
validador.
"""

from .deteccao import (
    CORTE_INFERIOR_MILI_BPS,
    CORTE_SUPERIOR_MILI_BPS,
    JANELA_BARRAS,
    PERMANENCIA_BARRAS,
    REGIMES_MINIMOS,
    Classificacao,
    Episodio,
    classificar_serie,
    cobertura,
    derivar_cortes,
    do_dataset,
    episodios,
)

__all__ = [
    "CORTE_INFERIOR_MILI_BPS",
    "CORTE_SUPERIOR_MILI_BPS",
    "JANELA_BARRAS",
    "PERMANENCIA_BARRAS",
    "REGIMES_MINIMOS",
    "Classificacao",
    "Episodio",
    "classificar_serie",
    "cobertura",
    "derivar_cortes",
    "do_dataset",
    "episodios",
]
