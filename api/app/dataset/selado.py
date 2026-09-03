"""O caminho do validador: walk-forward e holdout selado (R27, R28, R30).

**Este e o unico modulo do sistema que le os conjuntos do validador.** Um teste
por AST varre o codigo e recusa qualquer outro arquivo que mencione
`bar_por_finalidade` com finalidade de validador - a mesma tecnica que ja
protege a fronteira entre maos rapidas e cerebro (§3.2).

## Por que o holdout tem funcao propria, e nao um parametro

Um parametro `finalidade='holdout'` em `loader.carregar` seria uma opcao a mais
num lugar onde ja se passam quatro argumentos. Aqui e uma funcao com nome
proprio, que **exige uma hipotese** e **grava o uso**. A diferenca nao e
estetica: a versao com parametro pode ser chamada por engano, e esta nao.

Secao 8.5.1: "O agente nao pode consultar o holdout. Se esse periodo for
visualizado ou utilizado para ajustar a hipotese, deixa de ser reservado e
precisa ser substituido. A separacao e garantida pela estrutura de dados e
pelas permissoes da ferramenta, nao pela disciplina do agente."

## Uso unico, imposto por `UNIQUE`

`holdout_access.hypothesis_id` e `UNIQUE`. A segunda leitura da mesma hipotese
nao entra na tabela, e sem linha na tabela nao ha leitura - a ordem das duas
coisas neste modulo e deliberada: **grava primeiro, le depois**. Ler primeiro
e gravar depois deixaria a barra ja na memoria quando a recusa chegasse, e o
dado selado teria sido consumido de qualquer forma.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from .loader import BarraCarregada
from .split import Conjunto, conjunto

log = logging.getLogger(__name__)

# `acesso = 'validador'` literal, espelhando o `acesso = 'agente'` do loader.
# Nenhum parametro amplia o alcance de nenhum dos dois.
_SQL_VALIDADOR = """
SELECT open_time_ms, close_time_ms, open, high, low, close,
       volume, quote_volume, trades
FROM bar_por_finalidade
WHERE dataset_id = :dataset_id
  AND acesso = 'validador'
  AND finalidade = :finalidade
  AND open_time_ms >= :de_ms
  AND open_time_ms <  :ate_ms
ORDER BY open_time_ms
"""

SEM_LIMITE_MS = 1 << 62


class HoldoutJaConsumido(Exception):
    """A hipotese ja gastou o unico acesso dela ao periodo selado."""


class ConjuntoAusente(Exception):
    pass


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ler(
    conn: sqlite3.Connection,
    dataset_id: int,
    finalidade: str,
    *,
    de_ms: int | None = None,
    ate_ms: int | None = None,
) -> list[BarraCarregada]:
    c: Conjunto | None = conjunto(conn, dataset_id, finalidade)
    if c is None:
        raise ConjuntoAusente(
            f"o dataset {dataset_id} nao tem o conjunto '{finalidade}';"
            " a divisao por finalidade nao foi criada"
        )
    linhas = conn.execute(
        _SQL_VALIDADOR,
        {
            "dataset_id": dataset_id,
            "finalidade": finalidade,
            "de_ms": max(c.from_ms, de_ms if de_ms is not None else c.from_ms),
            "ate_ms": min(
                c.to_ms_exclusive,
                ate_ms if ate_ms is not None else c.to_ms_exclusive,
            ),
        },
    ).fetchall()
    return [BarraCarregada(*tuple(l)) for l in linhas]


def walk_forward(
    conn: sqlite3.Connection,
    dataset_id: int,
    *,
    de_ms: int | None = None,
    ate_ms: int | None = None,
) -> list[BarraCarregada]:
    """As barras do conjunto de walk-forward, ou um recorte dele.

    Sem uso unico: o walk-forward pode ser percorrido varias vezes, e e para
    isso que ele existe - "avaliar rapidamente diferentes regimes passados sem
    apresentar dados futuros ao agente" (secao 8.5.1). Quem tem uso unico e o
    holdout.
    """
    return _ler(conn, dataset_id, "walk_forward", de_ms=de_ms, ate_ms=ate_ms)


def holdout(
    conn: sqlite3.Connection,
    dataset_id: int,
    *,
    hypothesis_id: int,
    finalidade_declarada: str,
    creditos: int = 5,
) -> list[BarraCarregada]:
    """O teste final. Uma vez por hipotese, e nunca mais.

    Os 5 creditos sao o peso de "teste out-of-sample" da secao 8.6.1 -
    "consome dados reservados, que sao finitos e nao renovaveis". Aqui eles
    sao apenas REGISTRADOS; a cobranca no ledger entra no incremento 11, e o
    campo ja existe para nao precisar de retrofit.

    **Grava antes de ler.** Se a hipotese ja consumiu o acesso, o `UNIQUE`
    recusa a insercao e nenhuma barra chega a sair do banco. Na ordem inversa,
    a recusa chegaria com o dado ja em memoria - e o periodo selado teria sido
    consumido do mesmo jeito, so que sem registro.
    """
    if not finalidade_declarada.strip():
        raise ValueError(
            "o acesso ao holdout declara finalidade (secao 8.5.1); sem ela nao"
            " ha o que auditar depois"
        )
    try:
        conn.execute(
            "INSERT INTO holdout_access (hypothesis_id, dataset_id,"
            " requested_at, solicitante, finalidade, creditos, barras_lidas)"
            " VALUES (?,?,?,'validador',?,?,0)",
            (
                hypothesis_id,
                dataset_id,
                _agora(),
                finalidade_declarada.strip(),
                creditos,
            ),
        )
    except sqlite3.IntegrityError as erro:
        anterior = conn.execute(
            "SELECT requested_at, finalidade FROM holdout_access"
            " WHERE hypothesis_id = ?",
            (hypothesis_id,),
        ).fetchone()
        if anterior is None:
            raise
        raise HoldoutJaConsumido(
            f"a hipotese {hypothesis_id} ja leu o holdout em"
            f" {anterior['requested_at']} para '{anterior['finalidade']}'."
            " O periodo selado e usado UMA vez por hipotese (secao 8.4):"
            " reler seria transforma-lo em mais um conjunto de ajuste, e ele"
            " nao se renova"
        ) from erro

    barras = _ler(conn, dataset_id, "holdout")
    log.warning(
        "holdout.consumido",
        extra={
            "dataset_id": dataset_id,
            "hypothesis_id": hypothesis_id,
            "barras": len(barras),
            "creditos": creditos,
        },
    )
    return barras


def barras_do_holdout(conn: sqlite3.Connection, dataset_id: int) -> int:
    """QUANTAS barras o periodo selado tem. Nao devolve nenhuma delas.

    Mora aqui, e nao em quem pergunta, porque a guarda do incremento 9 e
    sobre o modulo que conhece o selado - e ela nao distingue "ler o dado" de
    "ler quanto dado existe", nem deveria: uma guarda que precisa julgar
    intencao para de ser verificavel.

    O validador precisou deste numero para o quarto item de R43 - "custo de
    oportunidade do dado reservado consumido" - e a primeira versao escreveu
    a consulta dentro de si. A guarda recusou, corretamente.
    """
    c = conjunto(conn, dataset_id, "holdout")
    return c.bars if c else 0


def ja_consumiu(conn: sqlite3.Connection, hypothesis_id: int) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM holdout_access WHERE hypothesis_id = ? LIMIT 1",
            (hypothesis_id,),
        ).fetchone()
    )


def usos_do_holdout(conn: sqlite3.Connection, dataset_id: int) -> list[dict]:
    """O registro de consumo. E o que prova que o selado foi usado uma vez so."""
    return [
        {
            "hypothesis_id": int(l["hypothesis_id"]),
            "requested_at": l["requested_at"],
            "finalidade": l["finalidade"],
            "creditos": int(l["creditos"]),
            "barras_lidas": int(l["barras_lidas"]),
        }
        for l in conn.execute(
            "SELECT hypothesis_id, requested_at, finalidade, creditos,"
            " barras_lidas FROM holdout_access WHERE dataset_id = ?"
            " ORDER BY id",
            (dataset_id,),
        )
    ]
