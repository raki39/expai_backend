"""O registro das execuções repetidas de A1b. Apenas por acréscimo.

Duas coisas moram aqui: **quais índices ainda faltam** e **o que já foi
medido**. Nenhuma agregação — quem agrega é `calibre.agregar`, sobre as linhas
que esta camada devolve.

## Por que não guardar a agregação

Mesma razão do saldo (que sai do ledger) e do estado corrente (que sai das
transições): a proporção é função de linhas imutáveis, então recalcular dá a
mesma resposta para sempre, e guardar uma cópia criaria a segunda fonte de
verdade que a regra 16 proíbe.

## Por que as condições vão na linha

`lote`, `n_barras`, `semente` e `tentativas_globais` são gravados por execução
em vez de lidos da config na hora de agregar. A config pode mudar; uma
proporção que misturasse execuções de lotes de tamanhos diferentes não
descreveria calibre nenhum, e nada no número acusaria a mistura. `divergencias`
existe justamente para dizer quando isso aconteceu.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from ..config.schema import ExperimentConfig
from .calibre import DESENHOS, Uma

log = logging.getLogger(__name__)


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def gravar(
    conn: sqlite3.Connection,
    execucoes: list[Uma],
    *,
    config_version_id: int,
    semente: int,
    lote: int,
    n_barras: int,
    tentativas_globais: int,
) -> int:
    """Grava as linhas. Devolve quantas entraram.

    Uma execução já gravada é **recusada pelo UNIQUE**, e não sobrescrita:
    gravá-la de novo a faria contar duas vezes na proporção, que é o defeito
    mais fácil de produzir num registro que cresce em pedaços.
    """
    entraram = 0
    for e in execucoes:
        try:
            conn.execute(
                "INSERT INTO a1b_execucao (config_version_id, desenho, indice,"
                " semente, lote, n_barras, tentativas_globais, r_lote, v_lote,"
                " r_com_portao, v_com_portao, sinais_piso, promovidos_piso,"
                " sinais_detectavel, promovidos_detectavel, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    config_version_id, e.desenho, e.indice, semente, lote,
                    n_barras, tentativas_globais, e.r_lote, e.v_lote,
                    e.r_com_portao, e.v_com_portao, e.sinais_piso,
                    e.promovidos_piso, e.sinais_detectavel,
                    e.promovidos_detectavel, _agora(),
                ),
            )
            entraram += 1
        except sqlite3.IntegrityError:
            log.info(
                "a1b.ja_gravada",
                extra={"desenho": e.desenho, "indice": e.indice},
            )
    return entraram


def ler(conn: sqlite3.Connection, config_version_id: int) -> list[Uma]:
    return [
        Uma(
            desenho=l["desenho"],
            indice=int(l["indice"]),
            r_lote=int(l["r_lote"]),
            v_lote=int(l["v_lote"]),
            r_com_portao=int(l["r_com_portao"]),
            v_com_portao=int(l["v_com_portao"]),
            sinais_piso=int(l["sinais_piso"]),
            promovidos_piso=int(l["promovidos_piso"]),
            sinais_detectavel=int(l["sinais_detectavel"]),
            promovidos_detectavel=int(l["promovidos_detectavel"]),
        )
        for l in conn.execute(
            "SELECT * FROM a1b_execucao WHERE config_version_id = ?"
            " ORDER BY desenho, indice",
            (config_version_id,),
        )
    ]


def faltando(
    conn: sqlite3.Connection,
    *,
    config_version_id: int,
    config: ExperimentConfig,
    quantas: int,
) -> dict[str, list[int]]:
    """Os próximos índices a rodar, por desenho, no máximo `quantas` no total.

    Os desenhos avançam **alternados** — um índice de cada vez em cada — para
    que um registro parcial tenha os dois lados medidos. Rodar os 200 do
    primeiro desenho antes de começar o segundo produziria, no meio do
    caminho, um relatório com metade da resposta e nenhuma indicação de que a
    outra metade nem começou.
    """
    ja = {
        (l["desenho"], int(l["indice"]))
        for l in conn.execute(
            "SELECT desenho, indice FROM a1b_execucao"
            " WHERE config_version_id = ?",
            (config_version_id,),
        )
    }
    saida: dict[str, list[int]] = {d: [] for d in DESENHOS}
    escolhidos = 0
    for i in range(config.a1b_execucoes):
        for desenho in DESENHOS:
            if escolhidos >= quantas:
                return saida
            if (desenho, i) not in ja:
                saida[desenho].append(i)
                escolhidos += 1
    return saida


def divergencias(
    conn: sqlite3.Connection,
    *,
    config_version_id: int,
    config: ExperimentConfig,
) -> list[str]:
    """Execuções gravadas sob condições diferentes das vigentes.

    Não é erro: é o registro dizendo que a comparação atravessa uma mudança.
    §10.2.3 já invalida comparação que atravessa mudança material, e aqui a
    diferença aparece com o número em vez de sumir dentro de uma média.
    """
    linhas = list(
        conn.execute(
            "SELECT DISTINCT lote, n_barras, semente, tentativas_globais"
            "  FROM a1b_execucao WHERE config_version_id = ?",
            (config_version_id,),
        )
    )
    problemas: list[str] = []
    if len({l["lote"] for l in linhas}) > 1:
        problemas.append(
            "ha execucoes com tamanhos de lote diferentes; a proporcao"
            " mistura calibres de multiplicidades diferentes"
        )
    if len({l["n_barras"] for l in linhas}) > 1:
        problemas.append(
            "ha execucoes com horizontes diferentes; o poder medido mistura"
            " amostras de tamanhos diferentes"
        )
    for l in linhas:
        if int(l["lote"]) != config.a1b_lote:
            problemas.append(
                f"execucoes gravadas com lote {l['lote']}, e a config vigente"
                f" diz {config.a1b_lote}"
            )
    return problemas
