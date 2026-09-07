"""Previsto contra observado, instante a instante. Incremento 18.

§8.4.1.2, passo 2: cada ordem hipotetica registrada no instante real, com o
preco que o simulador previu - e depois comparada com o mercado que apareceu.

## Os dois erros, e por que sao dois

    E1 = ask - abertura              DIAGNOSTICO
    E2 = p_exec_previsto - ask       ALVO da calibracao

**E1 ja contem meio spread.** Calibrar `spread_bps` a partir dele e continuar
somando `spread_bps` no simulador contaria o spread DUAS VEZES, e nada
acusaria - o resultado seria um simulador exageradamente pessimista com
aparencia de calibrado. Foi o usuario quem separou os dois ao fechar a D45.

**A direcao do pessimismo e `p10(E2) >= 0`.** Na compra pagamos o **ask**;
prever menos que o ask e otimismo, e um simulador otimista transforma
estrategia ruim em estrategia aprovada.

Na venda, a simetria: recebemos o **bid**, entao pessimismo e prever receber
no maximo ele.

    E1_venda = bid - abertura
    E2_venda = bid - p_exec_previsto

## O que este modulo NAO faz

Nao ajusta parametro nenhum. Ele mede. Quem ajusta e a estimativa, e ela roda
sobre estas linhas depois de a janela do piloto FECHAR - nunca durante.

E a referencia e SEMPRE mercado observado: `bbo_amostra`, que veio do coletor.
Ha guarda de codigo conferindo que nada aqui le `execution`, `baseline_result`
ou qualquer tabela de resultado simulado (R63). Comparar simulador com
simulador daria erro zero por construcao, e o numero pareceria excelente.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from ..aovivo import bbo
from ..config.schema import ExperimentConfig
from ..dataset.loader import BarraCarregada
from ..simulador.execucao import preco_executado, preco_referencia
from ..store import bloco_atomico

log = logging.getLogger(__name__)

# bps x 1000, como `app/regime` ja usa. Um decimo de milesimo de bps e
# resolucao de sobra para um delta de 0,5 bps, e mantem tudo inteiro.
ESCALA_MILI_BPS = 1_000
BPS = Decimal(10_000)

LADOS = ("compra", "venda")


class ContratoIncompativel(Exception):
    """A config vigente nao e a que o contrato assume. NAO calibrar."""


@dataclass(frozen=True)
class Observacao:
    t_grid_ms: int
    lado: str
    abertura: int
    bbo_bid: int
    bbo_bid_qty: int
    bbo_ask: int
    bbo_ask_qty: int
    p_exec_previsto: int
    e1_mili_bps: int
    e2_mili_bps: int
    nocional_cents: int
    dentro_do_l1: bool


def _agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mili_bps(delta: int, base: int) -> int:
    """`delta / base` em bps x 1000, arredondado ao inteiro mais proximo.

    `Decimal`, nunca `float`: estes numeros entram numa estatistica de quantil
    cujo limite inferior decide se a calibracao passa, e erro de ponto
    flutuante acumulado numa cauda e o tipo de coisa que ninguem percebe.
    """
    return int(
        (Decimal(delta) * BPS * ESCALA_MILI_BPS / Decimal(base))
        .to_integral_value()
    )


def prever(
    abertura: int, lado: str, config: ExperimentConfig
) -> int:
    """O preco que o simulador preve para uma ordem neste instante.

    **Usa o nucleo compartilhado**, e nao uma copia da formula. Se este modulo
    reimplementasse `preco_executado`, a calibracao mediria a distancia entre
    duas implementacoes nossas em vez do erro do simulador - e as duas
    divergiriam no primeiro ajuste de parametro.

    `preco_referencia` recebe uma barra porque o modelo `limite_adverso`
    precisa de `high`/`low`. Sob `abertura` - que o contrato `bbo@1` exige - so
    `open` e lido, e os outros campos sao inertes.
    """
    barra = BarraCarregada(
        open_time_ms=0, close_time_ms=0,
        open=abertura, high=abertura, low=abertura, close=abertura,
        volume=0, quote_volume=0, trades=0,
    )
    ref = preco_referencia(barra, lado, config)  # type: ignore[arg-type]
    return preco_executado(ref, lado, config)  # type: ignore[arg-type]


def calcular(
    *, t_grid_ms: int, lado: str, abertura: int, amostra: sqlite3.Row,
    config: ExperimentConfig, orcamento_cents: int,
) -> Observacao:
    """Uma observacao, a partir da barra e da amostra de BBO do mesmo instante."""
    bid = int(amostra["bid"])
    ask = int(amostra["ask"])
    bid_qty = int(amostra["bid_qty"])
    ask_qty = int(amostra["ask_qty"])

    previsto = prever(abertura, lado, config)

    if lado == "compra":
        # Pagamos o ask. Pessimismo = prever pagar pelo menos ele.
        e1 = _mili_bps(ask - abertura, abertura)
        e2 = _mili_bps(previsto - ask, abertura)
        qty_cotada = ask_qty
        preco_do_lado = ask
    else:
        # Recebemos o bid. Pessimismo = prever receber no maximo ele.
        e1 = _mili_bps(bid - abertura, abertura)
        e2 = _mili_bps(bid - previsto, abertura)
        qty_cotada = bid_qty
        preco_do_lado = bid

    # CONDICAO DE VALIDADE POR TAMANHO (ADR 0027). O preco do topo de livro so
    # vale para nocional <= o tamanho cotado naquele nivel. Conferido
    # observacao a observacao, e nunca assumido - a hipotese 41 girava ~US$ 714
    # por ordem e o topo de BTC/USDT carrega muito mais, mas "muito mais"
    # costuma virar "menos" exatamente nos instantes que interessam.
    #
    # As escalas: preco e quantidade vem em 10^8, e `nocional_cents` e o
    # produto convertido a centavos. `DIVISOR_NOCIONAL` do simulador e 10^14
    # pela mesma conta (10^8 x 10^8 / 100).
    nocional_cotado_cents = qty_cotada * preco_do_lado // 10**14
    dentro = orcamento_cents <= nocional_cotado_cents

    return Observacao(
        t_grid_ms=t_grid_ms, lado=lado, abertura=abertura,
        bbo_bid=bid, bbo_bid_qty=bid_qty, bbo_ask=ask, bbo_ask_qty=ask_qty,
        p_exec_previsto=previsto, e1_mili_bps=e1, e2_mili_bps=e2,
        nocional_cents=orcamento_cents, dentro_do_l1=dentro,
    )


def registrar(
    conn: sqlite3.Connection,
    *,
    contrato: str,
    venue: str,
    symbol: str,
    config: ExperimentConfig,
    config_version_id: int,
    orcamento_cents: int,
    de_ms: int | None = None,
    ate_ms_exclusive: int | None = None,
    lados: tuple[str, ...] = LADOS,
) -> dict:
    """Pareia BBO com barra fechada e grava a observacao de cada instante.

    **Confere o contrato ANTES de olhar qualquer amostra.** Se a semantica de
    execucao vigente nao for a que o contrato assume, nada e medido - as
    amostras foram alinhadas na abertura da barra porque a D20 fixou a
    referencia ali, e usa-las sob outra semantica mediria o instante errado.

    Idempotente: reprocessar o mesmo periodo sob a mesma config nao duplica
    nada, e a chave primaria e quem garante.
    """
    try:
        c = bbo.conferir_contrato(
            conn, contrato,
            timeframe=config.timeframe,
            execution_reference=config.execution_reference,
            latency_bars=config.latency_bars,
        )
    except bbo.ContratoNaoDescreveMais as e:
        raise ContratoIncompativel(str(e)) from e

    onde = ["a.venue = ?", "a.symbol = ?", "a.contrato = ?", "a.disponivel = 1"]
    args: list[object] = [venue, symbol, contrato]
    if de_ms is not None:
        onde.append("a.t_grid_ms >= ?")
        args.append(de_ms)
    if ate_ms_exclusive is not None:
        onde.append("a.t_grid_ms < ?")
        args.append(ate_ms_exclusive)

    # O JOIN e o que torna a comparacao honesta: so ha observacao quando
    # EXISTEM a cotacao e a barra do MESMO instante. `stream_bar` e mercado
    # observado, como a amostra - nenhum dos dois e resultado simulado.
    linhas = conn.execute(
        "SELECT a.t_grid_ms, a.bid, a.ask, a.bid_qty, a.ask_qty,"
        "       b.open AS abertura"
        "  FROM bbo_amostra a"
        "  JOIN stream_bar b"
        "    ON b.venue = a.venue AND b.symbol = a.symbol"
        "   AND b.timeframe = ? AND b.open_time_ms = a.t_grid_ms"
        f" WHERE {' AND '.join(onde)}"
        " ORDER BY a.t_grid_ms",
        [c.timeframe, *args],
    ).fetchall()

    gravadas = repetidas = 0
    agora = _agora()
    with bloco_atomico(conn, "calibracao_registrar"):
        for linha in linhas:
            for lado in lados:
                o = calcular(
                    t_grid_ms=int(linha["t_grid_ms"]), lado=lado,
                    abertura=int(linha["abertura"]), amostra=linha,
                    config=config, orcamento_cents=orcamento_cents,
                )
                cur = conn.execute(
                    "INSERT OR IGNORE INTO calibracao_observacao ("
                    " contrato, venue, symbol, t_grid_ms, lado,"
                    " config_version_id, abertura, bbo_bid, bbo_bid_qty,"
                    " bbo_ask, bbo_ask_qty, p_exec_previsto, e1_mili_bps,"
                    " e2_mili_bps, nocional_cents, dentro_do_l1, criado_em)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (contrato, venue, symbol, o.t_grid_ms, o.lado,
                     config_version_id, o.abertura, o.bbo_bid, o.bbo_bid_qty,
                     o.bbo_ask, o.bbo_ask_qty, o.p_exec_previsto,
                     o.e1_mili_bps, o.e2_mili_bps, o.nocional_cents,
                     1 if o.dentro_do_l1 else 0, agora),
                )
                if cur.rowcount:
                    gravadas += 1
                else:
                    repetidas += 1

    log.info("calibracao.observacoes", extra={
        "contrato": contrato, "symbol": symbol,
        "config_version_id": config_version_id,
        "instantes": len(linhas), "gravadas": gravadas, "repetidas": repetidas,
    })
    return {
        "instantes_pareados": len(linhas),
        "gravadas": gravadas,
        "repetidas": repetidas,
        "lados": list(lados),
    }


def serie_e2(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    lado: str, config_version_id: int,
    de_ms: int | None = None, ate_ms_exclusive: int | None = None,
    apenas_dentro_do_l1: bool = True,
) -> list[int]:
    """A serie de `E2` em mili-bps, em ordem de tempo.

    **Ordenada, e nao um conjunto.** O block bootstrap precisa da ordem: ele
    reamostra BLOCOS contiguos justamente para preservar a dependencia que o
    embaralhamento destruiria.

    `apenas_dentro_do_l1` e o default porque o ADR 0027 exclui da estatistica a
    observacao cujo nocional passa do tamanho cotado - ali o preco do topo de
    livro nao descreve o que a ordem encontraria. Ela continua GRAVADA.
    """
    onde = ["contrato = ?", "venue = ?", "symbol = ?", "lado = ?",
            "config_version_id = ?"]
    args: list[object] = [contrato, venue, symbol, lado, config_version_id]
    if apenas_dentro_do_l1:
        onde.append("dentro_do_l1 = 1")
    if de_ms is not None:
        onde.append("t_grid_ms >= ?")
        args.append(de_ms)
    if ate_ms_exclusive is not None:
        onde.append("t_grid_ms < ?")
        args.append(ate_ms_exclusive)

    return [
        int(r["e2_mili_bps"])
        for r in conn.execute(
            "SELECT e2_mili_bps FROM calibracao_observacao"
            f" WHERE {' AND '.join(onde)} ORDER BY t_grid_ms",
            args,
        )
    ]
