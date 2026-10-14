"""Acesso ao SQLite no volume persistente.

Regras que valem para todo o projeto:

- O `runner` e o unico dono de escrita durante um run. A API le.
- WAL ligado, `busy_timeout` definido. Um PROCESSO escritor, e replicas
  proibidas pela plataforma. **Escrita concorrente entre THREADS existe na
  0C** e e coberta por WAL + `busy_timeout` + conexao por thread (ADR 0031).
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
# cobre a espera de escrita.
#
# A ULTIMA FRASE DESTE COMENTARIO DESCREVIA A 0A, e foi corrigida na 0C. Ela
# dizia "nada aqui vira concorrencia de ESCRITA: na 0A o run e atomico (ADR
# 0018) e ha um agente so". Na 0C ha escrita concorrente, e ela e ROTINA:
#
#   - as rotas POST do painel sao sincronas, e correm no threadpool;
#   - `POST /api/aovivo/barras` e a unica rota `async`, e grava na thread do
#     event loop -- a cada 5 minutos, para sempre, vinda do rele;
#   - o executor do forward (incremento 18) sera mais uma.
#
# O que torna isso seguro NAO e haver um escritor so: e WAL, `busy_timeout` e
# uma conexao por thread, os tres. "Escritor unico" e verdade de PROCESSO e
# falso de THREAD, e o ADR 0031 corrige o criterio da D44 por isso.
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


class PreflightFalhou(Exception):
    """Uma migracao destrutiva encontrou dado onde esperava tabela vazia.

    Ela PARA em vez de descartar. O contrario e o modo de falha que nao tem
    volta: `DROP TABLE` sobre linha que alguem gravou apaga evidencia, e
    nenhuma conferencia posterior sabe o que havia la.
    """


def _preflight_33(conn: sqlite3.Connection) -> None:
    """A migracao 33 RECRIA cinco tabelas, e isso so e seguro se elas estiverem
    vazias.

    Elas estao vazias em producao - conferido por leitura em 2026-09-22 e de
    novo em 2026-10-07 -, e o ADR 0041 usa exatamente esse fato para justificar
    recriar em vez de migrar. **Mas "estava vazio quando eu olhei" nao e uma
    garantia**: entre a leitura e o deploy cabe uma chamada. Esta conferencia
    roda IMEDIATAMENTE antes do `DROP`, dentro da mesma transacao.

    E `janela_piloto` NAO e tocada pela migracao: a linha de 2026-09-21 tem de
    atravessar inteira. Ela e conferida antes e depois, pelo conteudo.
    """
    recriadas = (
        "calibracao_observacao", "janela_revalidacao", "revalidacao_uso",
        "calibracao_versao", "calibracao_regime",
    )
    com_linha = []
    for tabela in recriadas:
        n = int(conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0])
        if n:
            com_linha.append({"tabela": tabela, "linhas": n})
    if com_linha:
        raise PreflightFalhou(
            f"a migracao 33 recria {len(recriadas)} tabelas e encontrou dado em"
            f" {com_linha}. Ela PARA: o ADR 0041 autoriza recriar porque elas"
            f" estao vazias, e descartar linha que alguem gravou apagaria"
            f" evidencia sem volta. Decida o que fazer com esse dado antes"
        )


def _preflight_35(conn: sqlite3.Connection) -> None:
    """A 35 rotula o passado por DEFAULT, e isso so vale se o rotulo descrever.

    `tem_override` entra com `DEFAULT 1`, e para as linhas que ja existem isso
    e correto: sob a semantica antiga do ADR 0033 uma linha so era gravada
    para regime com override.

    **O caso que mentiria** e uma linha antiga cujo `spread_bps_x1000` ja fosse
    igual a base do perfil - ela seria rotulada como override sem o ser. Em
    producao nao ha linha nenhuma (a identidade vigente termina em
    `sem-perfil`), mas "nao havia quando eu olhei" nao e garantia: esta
    conferencia roda na mesma transacao, imediatamente antes do `ALTER`.

    Ela PARA em vez de rotular errado, porque um rotulo errado aqui e
    invisivel: o valor continua certo, e so o estado publicado fica falso.
    """
    mentiriam = [
        dict(linha) for linha in conn.execute(
            "SELECT r.perfil_hash, r.regime, r.spread_bps_x1000"
            "  FROM calibracao_perfil_regime r"
            "  JOIN calibracao_perfil p ON p.hash = r.perfil_hash"
            " WHERE r.spread_bps_x1000 = p.spread_bps_base_x1000"
        )
    ]
    if mentiriam:
        raise PreflightFalhou(
            f"a migracao 35 rotularia como `tem_override = 1` linha(s) cujo"
            f" valor ja e igual a base: {mentiriam}. Sob a semantica nova elas"
            f" sao `calibrado_sem_override`, e o DEFAULT as descreveria"
            f" errado. Decida o rotulo de cada uma antes"
        )


#: Conferencias que rodam IMEDIATAMENTE antes do SQL de uma migracao, na mesma
#: transacao. Uma migracao destrutiva que nao confere o que vai destruir e uma
#: aposta em que ninguem escreveu entre a leitura e o deploy.
#:
#: A 35 nao destroi nada, e tem preflight pelo motivo vizinho: ela ROTULA o
#: passado por `DEFAULT`, e um rotulo errado nao aparece em numero nenhum.
PREFLIGHT = {33: _preflight_33, 35: _preflight_35}


def _impressao_da_janela_piloto(conn: sqlite3.Connection) -> list[tuple]:
    """O CONTEUDO da linha do piloto, para comparar antes e depois.

    Contar linhas nao bastaria: a migracao poderia recriar a tabela com uma
    linha diferente e a contagem fecharia.
    """
    try:
        return [
            tuple(r) for r in conn.execute(
                "SELECT contrato, venue, symbol, de_ms, ate_ms_exclusive,"
                "       observacoes_validas, observacoes_totais,"
                "       dias_corridos_x1000, fechada_por, criado_em"
                "  FROM janela_piloto ORDER BY contrato, venue, symbol"
            )
        ]
    except sqlite3.OperationalError:
        return []


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
            preflight = PREFLIGHT.get(versao)
            piloto_antes = _impressao_da_janela_piloto(conn)
            if preflight is not None:
                preflight(conn)
            for statement in statements:
                conn.execute(statement)
            piloto_depois = _impressao_da_janela_piloto(conn)
            if piloto_antes != piloto_depois:
                raise PreflightFalhou(
                    f"a migracao {versao} alterou `janela_piloto`:"
                    f" {piloto_antes} -> {piloto_depois}. Ela nao deveria"
                    f" tocar o fechamento do piloto"
                )
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
