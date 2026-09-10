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
import hashlib
import pathlib
import sqlite3
import tempfile
import time
from dataclasses import dataclass


#: Pragmas de DESEMPENHO, só da conexão da cópia. Medidos em 2026-09-10
#: contra o padrão, com o resultado canônico exigido IDÊNTICO byte a byte —
#: casos, comparação de baselines, B4 e digest de cada run (21.590 bytes):
#:
#: | variante | B4 | lucro só sem custos | total |
#: |---|---|---|---|
#: | padrão | 27,3 s | 66,7 s | 102,3 s |
#: | `temp_store=MEMORY` | **14,3 s** | 61,9 s | **82,5 s** |
#: | + `mmap_size` 256 MB e `cache_size` 128 MB | 15,7 s | 62,4 s | 85,0 s |
#:
#: Fica só `temp_store`: o GROUP BY da view de saldo monta uma B-tree
#: temporária, e por padrão ela vai para ARQUIVO. `mmap` e `cache` não
#: compraram nada e custariam memória. O banco OFICIAL não é tocado — isto vale
#: só para a cópia, onde o resultado é conferido e o arquivo vai embora.
#:
#: Também medido, e RECUSADO: um índice de cobertura em `ledger_entry` e
#: `ledger_transaction` comprou 1,2x e exigiria migração. O custo do controle
#: de giro alto não é índice: é `saldo_da_conta` agregando o run inteiro a
#: cada compra, porque o saldo é sempre DERIVADO dos lançamentos (regra 16).
PRAGMAS_DA_COPIA = ("PRAGMA temp_store=MEMORY",)


@dataclass(frozen=True)
class Copia:
    """O laboratório temporário, com o custo operacional medido."""

    conn: sqlite3.Connection
    caminho: pathlib.Path
    micros_para_copiar: int

    def bytes_em_disco(self) -> int:
        """O tamanho REAL, somando o `-wal` e o `-shm`.

        **Medido em produção em 2026-09-09**: a primeira versão lia
        `destino.stat().st_size` logo depois do `backup()` e publicou
        **4.096 bytes** para uma cópia do banco de produção, que tem 70.080
        barras e 79 runs. O número é impossível, e a causa é o WAL: em
        `journal_mode=WAL` o `backup()` escreve no `-wal`, e o arquivo
        principal segue vazio até o checkpoint.

        Era a mesma forma de sempre — um campo chamado `bytes_copiados` que
        continha *o tamanho do arquivo principal antes do checkpoint*. E ele
        errava para BAIXO, subestimando o custo operacional em toda medição.

        Chamável, e não campo: o tamanho **cresce** enquanto a suíte roda, e
        congelá-lo na criação mediria o instante errado de novo.
        """
        return sum(
            p.stat().st_size
            for p in self.caminho.parent.glob(self.caminho.name + "*")
            if p.is_file()
        )


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
    for pragma in PRAGMAS_DA_COPIA:
        alvo.execute(pragma)
    try:
        yield Copia(
            conn=alvo, caminho=destino, micros_para_copiar=micros
        )
    finally:
        alvo.close()
        _apagar(pasta)


# ---------------------------------------------------------------------------
# A copia que ATRAVESSA requisicoes - o A1a parcelado (OP-1)
# ---------------------------------------------------------------------------


def criar_copia(oficial: sqlite3.Connection, destino: pathlib.Path) -> int:
    """A cópia por `backup()` consistente. Devolve os micros. Nunca sobrescreve."""
    from ..store import conectar

    if destino.exists():
        raise FileExistsError(
            f"ja existe arquivo em {destino}: uma copia nunca e sobrescrita"
        )
    inicio = time.perf_counter_ns()
    alvo = conectar(destino)
    try:
        oficial.backup(alvo)
    finally:
        alvo.close()
    return (time.perf_counter_ns() - inicio) // 1_000


def abrir_copia(caminho: pathlib.Path) -> sqlite3.Connection:
    """Abre uma cópia QUE JÁ EXISTE. **Nunca cria.**

    `sqlite3.connect` cria o arquivo quando ele não existe — e uma cópia
    perdida viraria um banco VAZIO com o mesmo nome, sobre o qual a próxima
    etapa rodaria como se nada tivesse acontecido. Com `mode=rw` a ausência
    levanta, e a execução aborta explicitamente em vez de reconstruir.
    """
    from ..store import _PRAGMAS

    if not caminho.exists():
        raise FileNotFoundError(f"a copia {caminho} nao existe")
    conn = sqlite3.connect(
        caminho.resolve().as_uri() + "?mode=rw",
        uri=True,
        timeout=5.0,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS + PRAGMAS_DA_COPIA:
        conn.execute(pragma)
    return conn


def impressao(caminho: pathlib.Path) -> str:
    """O sha256 do arquivo principal, com o WAL zerado antes.

    Com o WAL zerado, o arquivo principal contém tudo — e duas leituras sem
    escrita no meio dão os mesmos bytes. Qualquer escrita, até um `UPDATE` que
    não muda contagem nenhuma, muda a impressão. É ela que decide se uma etapa
    interrompida pode rodar de novo.
    """
    conn = abrir_copia(caminho)
    try:
        ocupado = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    finally:
        conn.close()
    if ocupado:
        raise RuntimeError(
            "o checkpoint da copia nao completou: ha outra conexao aberta nela"
        )
    h = hashlib.sha256()
    with caminho.open("rb") as arquivo:
        for pedaco in iter(lambda: arquivo.read(1 << 20), b""):
            h.update(pedaco)
    return h.hexdigest()


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
    # O nome REAL da tabela de uso do holdout. A lista dizia `holdout_uso`, que
    # nunca existiu - e a fotografia gravava -1 dos dois lados e passava.
    "holdout_access",
    # Conceder orcamento tambem e escrever credito: um `conceder` que caisse no
    # banco oficial abriria um braco de credito que ninguem pediu.
    "test_credit_budget",
)


class FotografiaCega(RuntimeError):
    """Uma tabela da lista não existe: a fotografia não vê o que devia vigiar."""


def fotografia(conn: sqlite3.Connection) -> dict[str, int]:
    """A contagem de cada tabela que a certificação não pode tocar.

    Conferida ANTES e DEPOIS no banco oficial, e qualquer diferença recusa o
    certificado. Não é a mesma coisa que confiar na cópia: é a evidência de que
    a cópia foi de fato onde a escrita aconteceu.
    """
    # Tabela ausente LEVANTA. A primeira versao gravava -1 e seguia, sob um
    # comentario dizendo que isso impedia a comparacao de "passar por falta de
    # dado" - e -1 antes igual a -1 depois PASSA. Os cinco certificados
    # selados da cv9 publicaram `holdout_uso: -1`: a tabela nunca existiu (o
    # nome real e `holdout_access`), e a conferencia de holdout passou sempre
    # sem ver nada. Uma guarda cega nao pode passar por igualdade de cegueira.
    existentes = {
        linha[0]
        for linha in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    faltando = [
        t for t in TABELAS_QUE_A_CERTIFICACAO_NAO_PODE_TOCAR if t not in existentes
    ]
    if faltando:
        raise FotografiaCega(
            f"a fotografia do banco oficial nao ve {faltando}: a tabela nao"
            " existe neste schema, e contar o que nao existe daria o mesmo"
            " numero antes e depois - uma conferencia que passa sempre"
        )
    return {
        tabela: int(conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0])
        for tabela in TABELAS_QUE_A_CERTIFICACAO_NAO_PODE_TOCAR
    }


def conferir_intocado(antes: dict[str, int], depois: dict[str, int]) -> list[str]:
    """As diferenças, se houver. Lista vazia = nada foi tocado."""
    return [
        f"{tabela}: {antes[tabela]} antes, {depois.get(tabela)} depois"
        for tabela in antes
        if antes[tabela] != depois.get(tabela)
    ]
