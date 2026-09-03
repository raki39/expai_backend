"""Acesso ao SQLite no volume persistente.

Regras que valem para todo o projeto:

- O `runner` e o unico dono de escrita durante um run. A API le.
- WAL ligado, `busy_timeout` definido. Com um processo, um escritor e
  replicas proibidas pela plataforma, nao ha escrita concorrente.
- Migracao roda no boot, nunca no build.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .migrations import MIGRACOES

log = logging.getLogger(__name__)

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
)


def conectar(db_path: Path) -> sqlite3.Connection:
    """Abre conexao com os pragmas do projeto aplicados."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        timeout=5.0,
        isolation_level=None,  # autocommit; transacoes sao explicitas
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


# ---------------------------------------------------------------------------
# Uma conexao POR THREAD
# ---------------------------------------------------------------------------
#
# O FastAPI roda endpoint sincrono no threadpool, e o painel dispara catorze
# requisicoes em paralelo a cada carga. Com uma conexao unica de processo -
# que era o desenho - varias threads usavam o MESMO `sqlite3.Connection` ao
# mesmo tempo. Medido: 3 falhas em 208 requisicoes concorrentes, `500` em
# `/api/curva` e `503` em `/api/config`, ~1,4%.
#
# A suite nunca veria: `TestClient` chama uma rota de cada vez, e o defeito so
# existe quando duas chamadas se cruzam. E mais uma vez o que o CLAUDE.md ja
# registra - o que a suite nao consegue observar, ela nao protege.
#
# Conexao por thread e o modo normal de operar SQLite: com `journal_mode=WAL`,
# varios leitores e um escritor convivem sem se bloquear, e `busy_timeout`
# cobre a espera de escrita. Nada aqui vira concorrencia de ESCRITA: na 0A o
# run e atomico (ADR 0018) e ha um agente so.
_local = threading.local()


def conexao_do_thread(db_path: Path) -> sqlite3.Connection:
    """A conexao desta thread, criada na primeira vez que ela pede.

    Guardada por CAMINHO tambem: se o caminho mudar (acontece nos testes, que
    dao um banco novo por caso), a conexao velha apontaria para outro arquivo
    e a thread leria o banco errado - um defeito silencioso, do tipo que este
    projeto ja colecionou.
    """
    atual = getattr(_local, "caminho", None)
    if atual != db_path or getattr(_local, "conn", None) is None:
        anterior = getattr(_local, "conn", None)
        if anterior is not None:
            anterior.close()
        _local.conn = conectar(db_path)
        _local.caminho = db_path
    return _local.conn


def fechar_conexao_do_thread() -> None:
    """Fecha e esquece a conexao desta thread. Usado no encerramento e nos testes."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
    _local.conn = None
    _local.caminho = None


@contextmanager
def bloco_atomico(conn: sqlite3.Connection, nome: str = "bloco") -> Iterator[None]:
    """Tudo ou nada, aninhavel.

    SAVEPOINT em vez de BEGIN porque este bloco pode rodar dentro de outro que
    ja abriu transacao - e um `BEGIN` aninhado falha com "cannot start a
    transaction within a transaction". Em autocommit, o SAVEPOINT abre a
    transacao sozinho.

    Importa no ledger: uma transacao contabil que grave metade dos lancamentos
    e quebre no meio deixaria o livro desequilibrado, que e exatamente o que a
    conferencia de partidas dobradas existe para tornar impossivel.
    """
    conn.execute(f"SAVEPOINT {nome}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO {nome}")
        conn.execute(f"RELEASE {nome}")
        raise
    conn.execute(f"RELEASE {nome}")


def volume_gravavel(caminho: Path) -> bool:
    """Confere que o diretorio existe e aceita escrita.

    Um servico que passa em tudo menos nisso esta gravando no filesystem
    efemero e ninguem percebe ate perder um run.
    """
    try:
        caminho.mkdir(parents=True, exist_ok=True)
        teste = caminho / ".escrita_ok"
        teste.write_text("ok", encoding="utf-8")
        teste.unlink()
        return True
    except OSError:
        return False


def devices_do_caminho(caminho: Path) -> tuple[int | None, int | None]:
    """Device do caminho e device de "/". Evidencia crua, para o log."""
    try:
        return caminho.stat().st_dev, Path("/").stat().st_dev
    except OSError:
        return None, None


def volume_montado(caminho: Path) -> bool | None:
    """O caminho esta num volume montado, ou e diretorio da imagem?

    Escrever com sucesso NAO prova persistencia: o Dockerfile cria /data na
    propria imagem, entao o app grava normalmente mesmo sem volume - e perde
    tudo no redeploy seguinte, sem erro nenhum. Foi exatamente o que
    aconteceu.

    Um volume montado e outro dispositivo de arquivos. Comparar o device do
    caminho com o de "/" distingue os dois casos.

    Retorna None quando a checagem nao se aplica (Windows, por exemplo), para
    nao confundir "nao sei" com "nao esta montado".
    """
    if os.name != "posix":
        return None
    try:
        dev_alvo = caminho.stat().st_dev
        dev_raiz = Path("/").stat().st_dev
    except OSError:
        return None
    return dev_alvo != dev_raiz


def versao_schema(conn: sqlite3.Connection) -> int:
    linha = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migration"
    ).fetchone()
    return int(linha["v"])


def dividir_statements(sql: str) -> list[str]:
    """Separa um script SQL em statements individuais.

    Nao da para usar `executescript`: ele emite um COMMIT implicito antes de
    rodar, o que descarta a transacao explicita e faz a migracao perder a
    atomicidade. Uma falha no meio deixaria o schema meio aplicado, sem
    registro de versao, e o boot seguinte quebraria em "table already exists".

    Tambem nao da para dividir por ';': o corpo de um TRIGGER contem ';'
    dentro de BEGIN...END. `sqlite3.complete_statement` sabe disso.
    """
    statements: list[str] = []
    buffer = ""
    for linha in sql.splitlines(keepends=True):
        buffer += linha
        if sqlite3.complete_statement(buffer):
            texto = buffer.strip()
            if texto:
                statements.append(texto)
            buffer = ""
    # Sobra so com comentario ou espaco e normal: um script pode terminar
    # com um comentario, e isso nao e statement incompleto.
    resto = "\n".join(
        linha
        for linha in buffer.splitlines()
        if linha.strip() and not linha.strip().startswith("--")
    ).strip()
    if resto:
        raise ValueError(f"statement SQL incompleto no fim do script: {resto[:80]!r}")
    return statements


def migrar(conn: sqlite3.Connection) -> int:
    """Aplica as migracoes pendentes, cada uma atomicamente. Idempotente."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration (
            version     INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at  TEXT NOT NULL
        )
        """
    )
    atual = versao_schema(conn)

    for versao, descricao, sql in MIGRACOES:
        if versao <= atual:
            continue
        statements = dividir_statements(sql)
        conn.execute("BEGIN")
        try:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migration (version, description, applied_at)"
                " VALUES (?, ?, datetime('now'))",
                (versao, descricao),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            log.exception("migration.failed", extra={"schema_version": versao})
            raise
        log.info(
            "migration.applied",
            extra={
                "schema_version": versao,
                "description": descricao,
                "statements": len(statements),
            },
        )
        atual = versao

    return atual
