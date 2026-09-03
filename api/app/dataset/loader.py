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

3. **Toda leitura declara FINALIDADE** (R26, incremento 9). `acesso =
   'agente'` entra como LITERAL na consulta, nunca como parametro: nao existe
   argumento capaz de fazer este modulo devolver walk-forward ou holdout.

Nao existe funcao neste modulo que leia o periodo selado. Walk-forward e
holdout tem caminho proprio em `selado.py`, com uso unico por hipotese imposto
por `UNIQUE` no banco (secao 8.5.1, R28).
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


# As consultas sao constantes de modulo de proposito: fica evidente em revisao
# de onde a leitura vem.
#
# `acesso = 'agente'` e LITERAL, nunca parametro. Nao existe argumento capaz
# de fazer esta consulta devolver walk-forward ou holdout - pelo mesmo motivo
# que nao existe argumento capaz de fazer `bar_experimento` devolver a
# reserva. E a fronteira morando na estrutura, como a secao 8.5.1 exige.
_SQL_AGENTE = """
SELECT open_time_ms, close_time_ms, open, high, low, close,
       volume, quote_volume, trades
FROM bar_por_finalidade
WHERE dataset_id = :dataset_id
  AND acesso = 'agente'
  AND finalidade = :finalidade
  AND close_time_ms <= :decision_ts_ms
ORDER BY open_time_ms
"""

# Compatibilidade com dataset ainda nao dividido. A divisao e criada na
# ingestao a partir do incremento 9; datasets ingeridos antes dele nao tem
# `dataset_split`, e recusar a leitura deles quebraria a reproducao de todo
# run da 0A - que precisa continuar reproduzivel exatamente como foi (R12).
_SQL_SEM_DIVISAO = """
SELECT open_time_ms, close_time_ms, open, high, low, close,
       volume, quote_volume, trades
FROM bar_experimento
WHERE dataset_id = :dataset_id
  AND close_time_ms <= :decision_ts_ms
ORDER BY open_time_ms
"""


def esta_dividido(conn: sqlite3.Connection, dataset_id: int) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM dataset_split WHERE dataset_id = ? LIMIT 1",
            (dataset_id,),
        ).fetchone()
    )


def carregar(
    conn: sqlite3.Connection,
    dataset_id: int,
    *,
    decision_ts_ms: int,
    finalidade: str,
    ultimas: int | None = None,
) -> list[BarraCarregada]:
    """Barras visiveis para uma decisao tomada em `decision_ts_ms`.

    `finalidade` e OBRIGATORIA (R26): toda leitura declara de que conjunto
    esta lendo. Nao ha valor padrao, pelo mesmo motivo que `decision_ts_ms`
    nao tem - padrao e a forma mais comum de esquecer.

    Este caminho e o do AGENTE, e so alcanca `exploracao` e `in_sample`.
    Walk-forward e holdout tem caminho proprio, em `selado.py`.

    Por que o corte e `close_time_ms <= decision_ts_ms`, e nao
    `open_time_ms <= decision_ts_ms`:

    uma barra que ABRIU antes da decisao mas ainda nao FECHOU tem maxima,
    minima e fechamento desconhecidos naquele instante. Devolve-la seria
    entregar justamente o dado futuro que o criterio 5 existe para impedir -
    e do jeito mais dificil de perceber, porque a barra parece legitima.
    """
    if ultimas is not None and ultimas <= 0:
        raise ValueError("`ultimas` precisa ser positivo quando informado")

    from .split import exigir_do_agente

    exigir_do_agente(finalidade)

    if esta_dividido(conn, dataset_id):
        linhas = conn.execute(
            _SQL_AGENTE,
            {
                "dataset_id": dataset_id,
                "finalidade": finalidade,
                "decision_ts_ms": decision_ts_ms,
            },
        ).fetchall()
    else:
        linhas = conn.execute(
            _SQL_SEM_DIVISAO,
            {"dataset_id": dataset_id, "decision_ts_ms": decision_ts_ms},
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
            "finalidade": finalidade,
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
