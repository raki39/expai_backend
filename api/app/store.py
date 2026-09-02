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
from pathlib import Path

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
