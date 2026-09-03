"""Roda A1b em pedaços e devolve o calibre acumulado.

Este módulo é a costura: pega a série base do dataset, pergunta ao registro
quais índices faltam, roda esse pedaço, grava, e agrega tudo o que já existe.

## A série base é o mercado, e não uma normal

`series.nula` sorteia com reposição dos retornos **reais** do in-sample e troca
o sinal. Gerar de uma normal calibraria o protocolo contra um mundo que §8.3 já
diz que não é o nosso — *"retornos reais violam independência e
estacionariedade em algum grau"* — e o DSR existe justamente porque assimetria
e curtose importam.

## O horizonte, e o que ele NÃO é

As séries têm o tamanho do **in-sample inteiro**, que é o horizonte contra o
qual `n_minimo` é calculado. Uma hipótese real observa menos que isso: só as
barras em que ela esteve com posição aberta — no run 30 foram 11.163 de 21.024.

Então o poder medido aqui é um **limite superior** do poder sobre uma hipótese
real. Está dito porque o número, sozinho, pareceria descrever o caso concreto.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from ..config.schema import ExperimentConfig
from ..dataset import loader, split
from ..validador import contador
from . import calibre, registro

log = logging.getLogger(__name__)

#: Teto de execuções por requisição. A ~0,85 s cada, 50 dão ~45 s — longe do
#: timeout e longe de um clique que não termina.
MAX_POR_PEDIDO = 50


class SeparacaoAusente(Exception):
    """Sem os quatro conjuntos de §8.5.1 não há in-sample de onde tirar a série."""


@dataclass(frozen=True)
class Insumos:
    base_bps: list[int]
    n_barras: int
    duracao_barra_ms: int
    tentativas_globais: int


def insumos(conn: sqlite3.Connection, dataset_id: int) -> Insumos:
    """A série base, o horizonte e o contador — tudo do banco."""
    conjunto = split.conjunto(conn, dataset_id, "in_sample")
    if conjunto is None:
        raise SeparacaoAusente(
            f"o dataset {dataset_id} nao tem conjunto de in-sample; rode a"
            " separacao da secao 8.5.1 antes de A1b"
        )
    base = loader.retornos_bps_entre(
        conn, dataset_id, conjunto.from_ms, conjunto.to_ms_exclusive
    )
    meta = loader.metadados(conn, dataset_id)
    return Insumos(
        base_bps=base,
        n_barras=conjunto.bars,
        duracao_barra_ms=meta.interval_ms,
        # O MESMO contador global que o lote real usa no DSR (§8.6). Um
        # calibre que deflacionasse por outro numero mediria um DSR que
        # ninguem enfrenta.
        tentativas_globais=max(1, contador.total(conn)),
    )


def rodar(
    conn: sqlite3.Connection,
    *,
    dataset_id: int,
    config: ExperimentConfig,
    config_version_id: int,
    quantas: int,
    semente: int | None = None,
) -> dict:
    """Roda o próximo pedaço e devolve o estado do calibre depois dele."""
    ins = insumos(conn, dataset_id)
    base = config.default_seed if semente is None else semente
    alvos = registro.faltando(
        conn,
        config_version_id=config_version_id,
        config=config,
        quantas=max(1, min(quantas, MAX_POR_PEDIDO)),
    )
    feitas, mags, cpu = calibre.rodar(
        base_bps=ins.base_bps,
        config=config,
        duracao_barra_ms=ins.duracao_barra_ms,
        n_barras=ins.n_barras,
        tentativas_globais=ins.tentativas_globais,
        indices=alvos,
        semente=base,
    )
    gravadas = registro.gravar(
        conn,
        feitas,
        config_version_id=config_version_id,
        semente=base,
        lote=config.a1b_lote,
        n_barras=ins.n_barras,
        tentativas_globais=ins.tentativas_globais,
    )
    log.info(
        "a1b.rodou",
        extra={"pedidas": len(feitas), "gravadas": gravadas, "cpu": cpu},
    )
    return {
        **resumo(conn, config_version_id, config, dataset_id=dataset_id),
        "rodadas_agora": len(feitas),
        "gravadas_agora": gravadas,
        "cpu_micros": cpu,
        "magnitudes": mags.como_dict(),
    }


def resumo(
    conn: sqlite3.Connection,
    config_version_id: int,
    config: ExperimentConfig,
    *,
    dataset_id: int | None = None,
) -> dict:
    """O calibre acumulado, derivado das linhas gravadas."""
    execucoes = registro.ler(conn, config_version_id)
    saida = {
        "braco": "a1b",
        "config_version_id": config_version_id,
        "execucoes_pedidas_por_desenho": config.a1b_execucoes,
        "lote_por_execucao": config.a1b_lote,
        "sinais_implantados_por_lote": config.a1b_sinais_implantados,
        "ic_bps": config.a1b_ic_bps,
        "procedimento": config.fdr_procedimento,
        "gravadas": len(execucoes),
        "desenhos": {
            d: calibre.agregar(execucoes, desenho=d, config=config)
            for d in calibre.DESENHOS
        },
        "divergencias": registro.divergencias(
            conn, config_version_id=config_version_id, config=config
        ),
        "limite_declarado": (
            "estas execucoes exercitam o pipeline ESTATISTICO - momentos,"
            " n_efetivo, p-valor, BY e DSR -, que e o que decide promocao no"
            " lote. Elas nao passam pelo simulador, pela avaliacao de regra"
            " nem pelo ledger: quem cobre esse lado e A1a, injetado pelo mesmo"
            " caminho das reais. E o horizonte usado e o in-sample INTEIRO,"
            " enquanto uma hipotese real observa so as barras em que esteve"
            " com posicao aberta - entao o poder medido e limite superior"
        ),
    }
    if dataset_id is not None:
        try:
            ins = insumos(conn, dataset_id)
        except SeparacaoAusente:
            return saida
        saida["magnitudes"] = calibre.magnitudes(
            config=config,
            duracao_barra_ms=ins.duracao_barra_ms,
            n_barras=ins.n_barras,
        ).como_dict()
        saida["horizonte_barras"] = ins.n_barras
        saida["tentativas_globais_no_dsr"] = ins.tentativas_globais
    return saida
