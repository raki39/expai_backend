"""Shadow do B3 sobre o fluxo ao vivo. Incremento 18, §8.4.1.2 passo 2.

§11.2.1 e literal sobre o que pode rodar aqui:

    "Na 0C, `run_shadow_strategy` roda apenas o baseline B3 para calibrar o
     simulador (...). E barato e nao valida estrategia nenhuma."

## A recusa nao e um `if`

**Este modulo nao RECEBE regra.** Ele deriva o B3 da config versionada, pelo
mesmo `regra_b3` que os baselines usam. Nao existe argumento que faca outra
coisa entrar - e e o mesmo desenho que impede o caminho do agente de alcancar
o holdout: `acesso = 'agente'` entra como literal no SQL, nunca como parametro.

Uma checagem seria melhor que nada e pior que isto: ela protegeria enquanto
ninguem a removesse, e §8.5.1 ja disse que garantia que depende de boa vontade
ja foi violada.

## O que o shadow acrescenta a observacao

`observacao.registrar` mede TODO instante da grade, nos dois lados - e e essa
a base estatistica da calibracao, porque `E2` e computavel sem ordem nenhuma
ter existido.

O shadow acrescenta o **caminho completo**: regra congelada -> sinal -> ordem
no instante real -> preco previsto. Ele exercita o encanamento que a 0C
precisa ter de pe, e produz o giro do B3 sobre dado ao vivo - um numero de
sanidade que a 0A ja mediu no historico (654 idas e voltas em 56.064 barras).

**Ele nao valida estrategia nenhuma, e nao promove nada.** O B3 nao tem
pre-registro, nao consome credito e nao entra em familia.

## A defasagem e do contrato, e nao deste modulo

O sinal fecha na barra `i` e a ordem entra na abertura da barra
`i + latency_bars`. `latency_bars` vem da config e o contrato `bbo@1` o amarra
em 1 - se ele mudasse, as amostras de BBO deixariam de cair no instante da
execucao, e a `conferir_contrato` recusa antes de qualquer medicao.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from ..aovivo import bbo
from ..config.schema import ExperimentConfig
from ..dataset.loader import BarraCarregada
from ..maos_rapidas.baselines import regra_b3
from ..regra import registro
from ..regra.sinais import Sinal, avaliar
from ..store import bloco_atomico
from .observacao import ContratoIncompativel, prever

log = logging.getLogger(__name__)


class SemBarras(Exception):
    """Nao ha barra fechada suficiente no fluxo para avaliar a regra."""


@dataclass(frozen=True)
class Ordem:
    t_grid_ms: int
    sinal_em_ms: int
    lado: str
    abertura: int
    p_exec_previsto: int


def _agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def barras_do_fluxo(
    conn: sqlite3.Connection, *, venue: str, symbol: str, timeframe: str,
    de_ms: int | None = None, ate_ms_exclusive: int | None = None,
) -> list[BarraCarregada]:
    """As barras FECHADAS do fluxo, em ordem.

    Le `stream_bar`, que e mercado observado - nunca `execution` nem
    `baseline_result`. O shadow compara previsao com MERCADO; comparar com
    outro resultado simulado daria erro zero por construcao (R63).
    """
    onde = ["venue = ?", "symbol = ?", "timeframe = ?"]
    args: list[object] = [venue, symbol, timeframe]
    if de_ms is not None:
        onde.append("open_time_ms >= ?")
        args.append(de_ms)
    if ate_ms_exclusive is not None:
        onde.append("open_time_ms < ?")
        args.append(ate_ms_exclusive)

    return [
        BarraCarregada(
            open_time_ms=int(r["open_time_ms"]),
            close_time_ms=int(r["open_time_ms"]) + int(r["interval_ms"]) - 1,
            open=int(r["open"]), high=int(r["high"]), low=int(r["low"]),
            close=int(r["close"]), volume=int(r["volume"]),
            quote_volume=int(r["quote_volume"]), trades=int(r["trades"]),
        )
        for r in conn.execute(
            "SELECT open_time_ms, interval_ms, open, high, low, close,"
            "       volume, quote_volume, trades"
            "  FROM stream_bar"
            f" WHERE {' AND '.join(onde)} ORDER BY open_time_ms",
            args,
        )
    ]


def ordens_do_b3(
    barras: list[BarraCarregada], config: ExperimentConfig
) -> list[Ordem]:
    """As ordens que o B3 daria sobre estas barras. NAO grava nada.

    Separado de `rodar` de propósito: perguntar "o que o B3 faria" e uma
    pergunta legitima, e ela nao pode ter como efeito colateral gravar ordem
    hipotetica no registro.
    """
    if len(barras) <= config.latency_bars:
        raise SemBarras(
            f"{len(barras)} barras nao bastam para uma regra com latencia de "
            f"{config.latency_bars} - a ordem entra na barra seguinte ao sinal"
        )

    regra = regra_b3(config)
    sinais = avaliar(barras, regra)

    ordens: list[Ordem] = []
    for i, sinal in enumerate(sinais):
        if sinal == Sinal.NADA:
            continue
        j = i + config.latency_bars
        if j >= len(barras):
            # O sinal fechou perto demais da ponta: a barra de execucao ainda
            # nao existe. Nao e ordem perdida - ela aparece na proxima volta,
            # quando a barra fechar.
            continue
        lado = "compra" if sinal == Sinal.ENTRAR else "venda"
        abertura = barras[j].open
        ordens.append(Ordem(
            t_grid_ms=barras[j].open_time_ms,
            sinal_em_ms=barras[i].open_time_ms,
            lado=lado,
            abertura=abertura,
            p_exec_previsto=prever(abertura, lado, config),
        ))
    return ordens


def rodar(
    conn: sqlite3.Connection,
    *,
    contrato: str,
    venue: str,
    symbol: str,
    config: ExperimentConfig,
    config_version_id: int,
    de_ms: int | None = None,
    ate_ms_exclusive: int | None = None,
) -> dict:
    """Roda o shadow do B3 e registra as ordens hipoteticas.

    **Sem parametro de regra.** A assinatura e a garantia: nao ha como pedir a
    este modulo que rode outra coisa, e ha teste sobre ela.

    A regra e CONGELADA ao ser registrada, como no caminho dos baselines -
    tunar o B3 depois de ver o resultado destroi o grupo de controle, e a
    partir do congelamento isso e impossivel em vez de desaconselhado.
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

    barras = barras_do_fluxo(
        conn, venue=venue, symbol=symbol, timeframe=c.timeframe,
        de_ms=de_ms, ate_ms_exclusive=ate_ms_exclusive,
    )
    ordens = ordens_do_b3(barras, config)

    regra = regra_b3(config)
    rule_id = registro.registrar(conn, regra)
    registro.congelar(conn, rule_id)

    gravadas = repetidas = 0
    agora = _agora()
    with bloco_atomico(conn, "shadow_rodar"):
        for o in ordens:
            cur = conn.execute(
                "INSERT OR IGNORE INTO shadow_ordem ("
                " contrato, venue, symbol, t_grid_ms, config_version_id,"
                " lado, rule_id, sinal_em_ms, p_exec_previsto, abertura,"
                " criado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (contrato, venue, symbol, o.t_grid_ms, config_version_id,
                 o.lado, rule_id, o.sinal_em_ms, o.p_exec_previsto,
                 o.abertura, agora),
            )
            if cur.rowcount:
                gravadas += 1
            else:
                repetidas += 1

    log.info("shadow.rodado", extra={
        "contrato": contrato, "symbol": symbol, "rule_id": rule_id,
        "barras": len(barras), "ordens": len(ordens),
        "gravadas": gravadas, "repetidas": repetidas,
    })
    return {
        "rule_id": rule_id,
        "regra": "B3 cruzamento_medias "
                 f"{config.b3_fast}/{config.b3_slow}, congelada",
        "barras": len(barras),
        "ordens": len(ordens),
        "gravadas": gravadas,
        "repetidas": repetidas,
        "compras": sum(1 for o in ordens if o.lado == "compra"),
        "vendas": sum(1 for o in ordens if o.lado == "venda"),
    }


def resumo(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    config_version_id: int,
) -> dict:
    """Quantas ordens houve, e quantas tem mercado observado para comparar.

    O JOIN e quem responde a segunda pergunta. Guardar isso numa coluna
    duplicaria estado que ja existe - e duas fontes sobre o mesmo fato divergem
    (regra 16).
    """
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM shadow_ordem"
        " WHERE contrato = ? AND venue = ? AND symbol = ?"
        "   AND config_version_id = ?",
        (contrato, venue, symbol, config_version_id),
    ).fetchone()["n"]

    comparaveis = conn.execute(
        "SELECT COUNT(*) AS n FROM shadow_ordem o"
        "  JOIN calibracao_observacao c"
        "    ON c.contrato = o.contrato AND c.venue = o.venue"
        "   AND c.symbol = o.symbol AND c.t_grid_ms = o.t_grid_ms"
        "   AND c.lado = o.lado AND c.config_version_id = o.config_version_id"
        " WHERE o.contrato = ? AND o.venue = ? AND o.symbol = ?"
        "   AND o.config_version_id = ? AND c.dentro_do_l1 = 1",
        (contrato, venue, symbol, config_version_id),
    ).fetchone()["n"]

    return {
        "ordens": int(total),
        "comparaveis": int(comparaveis),
        "sem_mercado_observado": int(total) - int(comparaveis),
        # O giro em idas e voltas, no vocabulario que o incremento 11b fixou:
        # `operacoes` valia 244 num lugar e 488 no outro sob o mesmo rotulo.
        "idas_e_voltas": int(total) // 2,
    }
