"""Teto rigido de chamadas e de gasto (secao 3.6 regra 2, secao 12.1).

A regra do documento e literal e vale mais que qualquer conveniencia:
**ao atingir o teto, as maos rapidas continuam e o cerebro para.** Um run que
aborta ao esgotar o orcamento nao produz resultado nenhum, e o resultado sem
cerebro e informacao legitima - e exatamente o que o incremento 4 mediu.

Sao tres limites, e o mais apertado vence:

    max_llm_calls_per_run       config versionada, no banco
    max_llm_usd_per_run_cents   config versionada, no banco
    LLM_MAX_USD_ABSOLUTE        variavel de ambiente, inviolavel

O terceiro existe porque "um teto definido em variavel de configuracao e um
teto que um bug, um prompt malicioso ou um agente criativo pode contornar"
(secao 12.1). Ele e conferido AQUI tambem, e nao so na hora de gravar a
config: quem grava a config e o painel, e um teto que so o painel respeita
protege contra o painel, nao contra o resto do programa.

O estado do teto e lido do **ledger**, nunca de contador em memoria. Um
processo reiniciado no meio de um run recomecaria a contagem do zero, e um
teto que zera sozinho nao e teto.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from ..config.schema import ExperimentConfig
from ..ledger.livro import gasto_com_reflexao
from ..settings import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Veredito:
    permitido: bool
    motivo: str
    chamadas_feitas: int
    gasto_cents: int
    teto_chamadas: int
    teto_gasto_cents: int

    def como_dict(self) -> dict:
        return {
            "permitido": self.permitido,
            "motivo": self.motivo,
            "chamadas_feitas": self.chamadas_feitas,
            "gasto_cents": self.gasto_cents,
            "teto_chamadas": self.teto_chamadas,
            "teto_gasto_cents": self.teto_gasto_cents,
        }


def teto_de_gasto_cents(config: ExperimentConfig, settings: Settings) -> int:
    """O menor entre o teto operacional e o limite inviolavel do ambiente."""
    absoluto = int(
        (settings.llm_max_usd_absolute * Decimal(100)).to_integral_value()
    )
    return min(config.max_llm_usd_per_run_cents, absoluto)


def consultar(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    config: ExperimentConfig,
    settings: Settings,
    custo_previsto_cents: int = 0,
) -> Veredito:
    """Pode fazer mais uma chamada?

    `custo_previsto_cents` e uma reserva conservadora para o custo da proxima
    chamada. Sem ela, a ultima chamada de um run poderia comecar dentro do
    teto e terminar fora dele - e o teto teria sido respeitado apenas na
    intencao. Conferir antes e a unica forma de o limite ser rigido.
    """
    estado = gasto_com_reflexao(conn, run_id)
    teto_gasto = teto_de_gasto_cents(config, settings)
    feitas = estado["chamadas_com_custo"]
    gasto = estado["gasto_cents"]

    if config.max_llm_calls_per_run <= 0:
        motivo = "teto de chamadas do run e zero: o cerebro nao fala neste run"
        permitido = False
    elif feitas >= config.max_llm_calls_per_run:
        motivo = (
            f"teto de chamadas atingido: {feitas} de "
            f"{config.max_llm_calls_per_run}"
        )
        permitido = False
    elif gasto + custo_previsto_cents > teto_gasto:
        motivo = (
            f"teto de gasto atingido: {gasto} + {custo_previsto_cents} "
            f"centavos excederia {teto_gasto}"
        )
        permitido = False
    else:
        motivo = "dentro dos tetos"
        permitido = True

    veredito = Veredito(
        permitido=permitido,
        motivo=motivo,
        chamadas_feitas=feitas,
        gasto_cents=gasto,
        teto_chamadas=config.max_llm_calls_per_run,
        teto_gasto_cents=teto_gasto,
    )
    if not permitido:
        log.info("cerebro.teto", extra=veredito.como_dict())
    return veredito
