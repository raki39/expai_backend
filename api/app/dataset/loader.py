"""O unico caminho pelo qual o experimento le barras.

Duas garantias, ambas no SQL - nunca numa checagem que o chamador poderia
esquecer (criterios 4 e 5 do incremento 1, secao 8.4.1.2):

1. **A reserva nao sai daqui.** A leitura e da view `bar_experimento`, cuja
   definicao ja exclui `open_time_ms >= reserved_from_ms`. Nao existe
   parametro capaz de fazer a view devolver barra reservada: o corte e parte
   do que ela e.

2. **Nada posterior a decisao.** `decision_ts_ms` e obrigatorio e entra na
   clausula WHERE. Nao ha valor padrao, porque padrao e a forma mais comum de
   esquecer.

Nao existe funcao neste modulo que leia o periodo reservado. Avaliar contra
holdout e Fase 0B (secao 8.5.1); construir o acesso agora seria deixar pronta
a tentacao de olhar.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import NamedTuple

log = logging.getLogger(__name__)


class BarraCarregada(NamedTuple):
    """Barra devolvida ao experimento. Inteiros de precisao fixa."""

    open_time_ms: int
    close_time_ms: int
    open: int
    high: int
    low: int
    close: int
    volume: int
    quote_volume: int
    trades: int


@dataclass(frozen=True)
class MetadadosDataset:
    id: int
    venue: str
    symbol: str
    timeframe: str
    interval_ms: int
    start_ms: int
    end_ms: int
    reserved_from_ms: int
    bars: int
    sha256: str
    fidelity_level: int
    price_scale_exp: int
    volume_scale_exp: int


class DatasetInexistente(Exception):
    pass


def metadados(conn: sqlite3.Connection, dataset_id: int) -> MetadadosDataset:
    linha = conn.execute(
        "SELECT id, venue, symbol, timeframe, interval_ms, start_ms, end_ms,"
        " reserved_from_ms, bars, sha256, fidelity_level, price_scale_exp,"
        " volume_scale_exp FROM dataset WHERE id = ?",
        (dataset_id,),
    ).fetchone()
    if linha is None:
        raise DatasetInexistente(f"dataset {dataset_id} nao existe")
    return MetadadosDataset(**dict(linha))


def dataset_vigente(conn: sqlite3.Connection) -> MetadadosDataset | None:
    """O dataset mais recente. Na 0A ha um so (um mercado, um instrumento)."""
    linha = conn.execute("SELECT MAX(id) AS id FROM dataset").fetchone()
    if linha is None or linha["id"] is None:
        return None
    return metadados(conn, int(linha["id"]))


# A consulta e uma constante de modulo de proposito: fica evidente em revisao
# que a leitura vem de `bar_experimento` e nunca de `bar`.
_SQL = """
SELECT open_time_ms, close_time_ms, open, high, low, close,
       volume, quote_volume, trades
FROM bar_experimento
WHERE dataset_id = :dataset_id
  AND close_time_ms <= :decision_ts_ms
ORDER BY open_time_ms
"""


def carregar(
    conn: sqlite3.Connection,
    dataset_id: int,
    *,
    decision_ts_ms: int,
    ultimas: int | None = None,
) -> list[BarraCarregada]:
    """Barras visiveis para uma decisao tomada em `decision_ts_ms`.

    Por que o corte e `close_time_ms <= decision_ts_ms`, e nao
    `open_time_ms <= decision_ts_ms`:

    uma barra que ABRIU antes da decisao mas ainda nao FECHOU tem maxima,
    minima e fechamento desconhecidos naquele instante. Devolve-la seria
    entregar justamente o dado futuro que o criterio 5 existe para impedir -
    e do jeito mais dificil de perceber, porque a barra parece legitima.

    O criterio literal pede "nao retornar barra com timestamp maior que o
    decision_ts". Esta versao e mais restritiva e o satisfaz.
    """
    if ultimas is not None and ultimas <= 0:
        raise ValueError("`ultimas` precisa ser positivo quando informado")

    linhas = conn.execute(
        _SQL, {"dataset_id": dataset_id, "decision_ts_ms": decision_ts_ms}
    ).fetchall()

    barras = [BarraCarregada(*tuple(linha)) for linha in linhas]
    # O recorte e feito DEPOIS da ordenacao, no fim da serie: "as N ultimas
    # barras visiveis". Fazer no SQL exigiria inverter a ordem e reverter, o
    # que so troca clareza por nada - a serie inteira sao ~48 mil linhas.
    if ultimas is not None:
        barras = barras[-ultimas:]

    log.debug(
        "dataset.carregado",
        extra={
            "dataset_id": dataset_id,
            "decision_ts_ms": decision_ts_ms,
            "barras": len(barras),
        },
    )
    return barras


def barra_em(
    conn: sqlite3.Connection, dataset_id: int, open_time_ms: int
) -> BarraCarregada | None:
    """A barra que abre exatamente em `open_time_ms`, ou None.

    Sem guarda de `decision_ts`, e de proposito: o simulador precisa da barra
    POSTERIOR a decisao para executar nela - e isso nao e olhar o futuro, e a
    latencia acontecendo. O que a decisao pode VER continua sendo assunto de
    `carregar()`.

    Le de `bar_experimento`, entao o periodo reservado segue inalcancavel:
    executar sobre dado reservado seria contamina-lo tanto quanto le-lo.
    """
    linha = conn.execute(
        "SELECT open_time_ms, close_time_ms, open, high, low, close, volume,"
        " quote_volume, trades FROM bar_experimento"
        " WHERE dataset_id = ? AND open_time_ms = ?",
        (dataset_id, open_time_ms),
    ).fetchone()
    return BarraCarregada(*tuple(linha)) if linha else None


def proxima_barra(
    conn: sqlite3.Connection, dataset_id: int, depois_de_ms: int, saltos: int = 1
) -> BarraCarregada | None:
    """A n-esima barra apos `depois_de_ms`. E assim que a latencia e aplicada.

    Anda pela GRADE de barras existentes, e nao por aritmetica de timestamp:
    se houvesse lacuna, somar o intervalo cairia num buraco e a execucao
    aconteceria numa barra que nao existe.
    """
    if saltos < 1:
        raise ValueError("latencia precisa ser de ao menos uma barra")
    linha = conn.execute(
        "SELECT open_time_ms, close_time_ms, open, high, low, close, volume,"
        " quote_volume, trades FROM bar_experimento"
        " WHERE dataset_id = ? AND open_time_ms > ?"
        " ORDER BY open_time_ms LIMIT 1 OFFSET ?",
        (dataset_id, depois_de_ms, saltos - 1),
    ).fetchone()
    return BarraCarregada(*tuple(linha)) if linha else None


def resumo(conn: sqlite3.Connection, dataset_id: int) -> dict:
    """Contagens para o painel. Nao devolve barra reservada, so quantas sao."""
    meta = metadados(conn, dataset_id)
    disponiveis = conn.execute(
        "SELECT COUNT(*) AS n FROM bar_experimento WHERE dataset_id = ?",
        (dataset_id,),
    ).fetchone()["n"]
    return {
        "dataset_id": meta.id,
        "venue": meta.venue,
        "symbol": meta.symbol,
        "timeframe": meta.timeframe,
        "sha256": meta.sha256,
        "fidelity_level": meta.fidelity_level,
        "price_scale_exp": meta.price_scale_exp,
        "volume_scale_exp": meta.volume_scale_exp,
        "barras_total": meta.bars,
        "barras_disponiveis": int(disponiveis),
        "barras_reservadas": meta.bars - int(disponiveis),
        "start_ms": meta.start_ms,
        "end_ms": meta.end_ms,
        "reserved_from_ms": meta.reserved_from_ms,
    }
