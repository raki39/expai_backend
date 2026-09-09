"""A cópia descartável: o caminho real, num banco que vai embora.

> *"Isso preserva o caminho real e elimina o principal risco do SAVEPOINT: um
> `commit()` futuro contaminaria apenas a cópia descartável."* — o usuário,
> 2026-09-09

## A medição que escolheu este desenho

| | SAVEPOINT | cópia |
|---|---|---|
| execução da suíte | 318 ms | 805 ms (backup: **30 ms**) |
| espaço | zero | 1,0 MB do banco + 0,2 MB de WAL/SHM |
| resultados | — | **6 casos, todos idênticos** ao SAVEPOINT |
| um `commit()` no caminho | **contamina permanentemente** | commita a cópia, que é apagada |

E o backup escala **linearmente a ~4 ms/MB**, medido de 3,9 a 62,7 MB. Um banco
de produção de 200 MB copiaria em ~800 ms — para algo que roda uma vez por
identidade.

## Por que `backup()` e não `shutil.copy`

O projeto roda em `journal_mode=WAL` (`store._PRAGMAS`). Nesse modo o arquivo
principal pode estar **atrás** do `-wal`, e copiar só ele produz um banco que
perdeu transações — silenciosamente. A certificação rodaria sobre um
laboratório que não é o de produção, e nada acusaria.

## O que torna isto seguro, verificado

**Nenhum módulo do caminho abre conexão própria.** Há **uma** `sqlite3.connect`
no repositório inteiro, em `store.conectar`, e zero atribuições de conexão em
nível de módulo nos 15 arquivos do caminho — todos recebem `conn` por
parâmetro. Se um deles abrisse a sua, escreveria no banco oficial enquanto a
certificação achava que estava na cópia. Há teste varrendo isso por AST.
"""

from __future__ import annotations

import contextlib
import pathlib
import sqlite3
import tempfile
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Copia:
    """O laboratório temporário, com o custo operacional medido."""

    conn: sqlite3.Connection
    caminho: pathlib.Path
    bytes_copiados: int
    micros_para_copiar: int


@contextlib.contextmanager
def laboratorio_descartavel(oficial: sqlite3.Connection):
    """Abre uma cópia consistente, entrega a conexão dela, e apaga tudo.

    O `finally` fecha e apaga **inclusive quando a suíte levanta**: um arquivo
    de laboratório que sobrevive a uma falha é um banco com hipóteses de
    certificação dentro, esperando que alguém o confunda com o oficial.
    """
    pasta = pathlib.Path(tempfile.mkdtemp(prefix="certificacao-"))
    destino = pasta / "laboratorio.sqlite3"
    from ..store import conectar

    inicio = time.perf_counter_ns()
    alvo = conectar(destino)
    try:
        oficial.backup(alvo)
    except Exception:
        alvo.close()
        _apagar(pasta)
        raise
    micros = (time.perf_counter_ns() - inicio) // 1_000
    try:
        yield Copia(
            conn=alvo,
            caminho=destino,
            bytes_copiados=destino.stat().st_size,
            micros_para_copiar=micros,
        )
    finally:
        alvo.close()
        _apagar(pasta)


def _apagar(pasta: pathlib.Path) -> None:
    """Some com a cópia e com os auxiliares do WAL. Nunca levanta.

    Falhar ao apagar não pode derrubar uma certificação que já terminou — o
    resultado dela é o produto, e o arquivo é resíduo. Mas o resíduo fica
    **nomeado** no `prefix` do diretório, para quem for procurar.
    """
    for p in sorted(pasta.glob("*"), reverse=True):
        try:
            p.unlink()
        except OSError:
            pass
    try:
        pasta.rmdir()
    except OSError:
        pass


TABELAS_QUE_A_CERTIFICACAO_NAO_PODE_TOCAR = (
    # O universo estatistico e o orcamento. Se qualquer uma destas mudar no
    # banco OFICIAL durante uma certificacao, o certificado e recusado.
    "hypothesis",
    "hypothesis_state",
    "test_credit_entry",
    # E o resto do registro, porque contaminacao nao precisa ser estatistica
    # para ser contaminacao.
    "run",
    "execution",
    "ledger_entry",
    "ledger_transaction",
    "agent_event",
    "holdout_uso",
)


def fotografia(conn: sqlite3.Connection) -> dict[str, int]:
    """A contagem de cada tabela que a certificação não pode tocar.

    Conferida ANTES e DEPOIS no banco oficial, e qualquer diferença recusa o
    certificado. Não é a mesma coisa que confiar na cópia: é a evidência de que
    a cópia foi de fato onde a escrita aconteceu.
    """
    fora = {}
    for tabela in TABELAS_QUE_A_CERTIFICACAO_NAO_PODE_TOCAR:
        try:
            fora[tabela] = int(
                conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
            )
        except sqlite3.OperationalError:
            # Tabela que ainda nao existe neste schema: registrar a ausencia,
            # e nao omitir a linha - omitir faria a comparacao passar por
            # falta de dado.
            fora[tabela] = -1
    return fora


def conferir_intocado(antes: dict[str, int], depois: dict[str, int]) -> list[str]:
    """As diferenças, se houver. Lista vazia = nada foi tocado."""
    return [
        f"{tabela}: {antes[tabela]} antes, {depois.get(tabela)} depois"
        for tabela in antes
        if antes[tabela] != depois.get(tabela)
    ]
