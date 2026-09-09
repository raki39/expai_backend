"""SAVEPOINT no banco canônico contra CÓPIA descartável: a medição.

> *"Minha preferência é pela cópia descartável (…). Isso preserva o caminho
> real e elimina o principal risco do SAVEPOINT: um `commit()` futuro
> contaminaria apenas a cópia descartável."* — o usuário, 2026-09-09

Este arquivo mede as duas, e a medição é o que fecha o ADR 0040.

## O que a cópia exige, e está tudo verificado

| exigência | estado |
|---|---|
| **nenhuma conexão global** no caminho | ✅ **uma** `sqlite3.connect` no repositório inteiro, em `store.conectar`; zero atribuições de módulo nos 15 arquivos do caminho |
| cópia **consistente** | `sqlite3.Connection.backup()`, a API de backup online do SQLite — coerente mesmo com escrita concorrente, e é por isso que não se usa `shutil.copy` sobre um banco em WAL |
| conexão **exclusiva** para ela | `store.conectar` sobre o arquivo temporário, com os mesmos quatro pragmas |
| resultados **canônicos** | pela `chave` do controle e pelo `content_hash`, **nunca** pelo `hypothesis_id` — que é temporário e não existe depois |

## Por que a cópia é melhor que o SAVEPOINT, e não apenas equivalente

O modo de falha do SAVEPOINT é **um `commit()` no caminho**: ele libera o
savepoint, o `ROLLBACK TO` deixa de desfazer, e a contaminação é silenciosa —
com o certificado parecendo correto e a família de 48 já suja. Guardável por
varredura, mas o dano é irreversível quando escapa.

Na cópia, o mesmo `commit()` **commita a cópia**, que é descartada em seguida.
O pior caso deixa de ser contaminação e passa a ser desperdício de disco.
"""

from __future__ import annotations

import pathlib
import sqlite3
import tempfile
import time

import pytest

from tests.test_portao_a import cenario  # noqa: F401
from tests.test_cerebro import settings  # noqa: F401


def _canonico(resultado) -> dict:
    """O resultado por CHAVE, sem nenhum id temporário.

    `hypothesis_id` e `run_id` são de linhas que não existem depois — nem no
    rollback nem na cópia. O que identifica um caso é a `chave` do catálogo, e
    o que identifica o conteúdo julgado é o `content_hash`.
    """
    return {
        c.chave: {
            "familia_de_defeito": c.familia_de_defeito,
            "tipo": c.tipo,
            "barrado": c.barrado,
            "veredito": c.veredito,
            "promovido": c.promovido,
            "creditos_cobrados": c.creditos_cobrados,
            "estado_final": c.estado_final,
            "tentativas": [
                {"o_que": t["o_que"], "barrada": t["barrada"]}
                for t in c.tentativas
            ],
        }
        for c in resultado.controles
    }


def _fotografia(conn: sqlite3.Connection) -> dict:
    tabelas = (
        "hypothesis", "hypothesis_state", "test_credit_entry", "run",
        "execution", "ledger_entry", "agent_event",
    )
    return {
        t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        for t in tabelas
    }


def _copiar(origem: sqlite3.Connection, destino: pathlib.Path) -> int:
    """Cópia CONSISTENTE pela API de backup online do SQLite.

    `shutil.copy` sobre um banco em `journal_mode=WAL` pode capturar o arquivo
    principal sem o `-wal` e produzir uma cópia que perdeu transações — e o
    projeto roda em WAL (`store._PRAGMAS`).
    """
    from app.store import conectar

    alvo = conectar(destino)
    try:
        origem.backup(alvo)
    finally:
        alvo.close()
    return destino.stat().st_size


# ---------------------------------------------------------------------------
# A MEDIÇÃO
# ---------------------------------------------------------------------------


def test_medicao_savepoint_contra_copia(conn: sqlite3.Connection, cenario, tmp_path):
    """Tempo, espaço e equivalência dos resultados nos dois caminhos."""
    from app.a1a import braco
    from app.store import conectar

    dataset_id, cfg = cenario
    antes = _fotografia(conn)

    # ------------------------------------------------------- via SAVEPOINT
    t0 = time.perf_counter()
    conn.execute("SAVEPOINT medicao")
    try:
        r_sp = braco.rodar(
            conn, dataset_id=dataset_id, config=cfg, config_version_id=1
        )
        canonico_sp = _canonico(r_sp)
    finally:
        conn.execute("ROLLBACK TO medicao")
        conn.execute("RELEASE medicao")
    t_savepoint = time.perf_counter() - t0
    depois_sp = _fotografia(conn)

    # ------------------------------------------------------------ via CÓPIA
    destino = tmp_path / "certificacao.sqlite3"
    t0 = time.perf_counter()
    bytes_copia = _copiar(conn, destino)
    t_copia_so = time.perf_counter() - t0

    copia = conectar(destino)
    try:
        r_cp = braco.rodar(
            copia, dataset_id=dataset_id, config=cfg, config_version_id=1
        )
        canonico_cp = _canonico(r_cp)
        dentro_cp = _fotografia(copia)
    finally:
        copia.close()
    t_copia_total = time.perf_counter() - t0

    # A cópia e os arquivos auxiliares do WAL vão todos embora.
    tamanho_total = sum(
        p.stat().st_size for p in tmp_path.glob("certificacao.sqlite3*")
    )
    for p in tmp_path.glob("certificacao.sqlite3*"):
        p.unlink()
    depois_cp = _fotografia(conn)

    print("\n=== TEMPO ===")
    print(f"  SAVEPOINT            {t_savepoint * 1000:8.1f} ms")
    print(f"  copia (so o backup)  {t_copia_so * 1000:8.1f} ms")
    print(f"  copia (total)        {t_copia_total * 1000:8.1f} ms")
    print(f"  sobrecusto da copia  {(t_copia_total - t_savepoint) * 1000:8.1f} ms")
    print("=== ESPACO ===")
    print(f"  banco copiado        {bytes_copia:,} bytes")
    print(f"  copia + WAL/SHM      {tamanho_total:,} bytes")
    print("=== O QUE A COPIA ESCREVEU (e foi descartado) ===")
    for t in dentro_cp:
        print(f"  {t:20s} {antes[t]:>6} -> {dentro_cp[t]:>6}")
    print("=== O CANONICO CONFERE? ===")

    # 1. O banco oficial nao mudou por nenhum dos dois caminhos.
    assert depois_sp == antes, "o SAVEPOINT deixou rastro"
    assert depois_cp == antes, "a copia deixou rastro no banco oficial"

    # 2. A copia EXECUTOU de verdade - se nao escrevesse, nao teria percorrido
    #    o adaptador, e a certificacao mediria outra coisa.
    assert dentro_cp["hypothesis"] > antes["hypothesis"]
    assert dentro_cp["ledger_entry"] > antes["ledger_entry"]

    # 3. E os dois caminhos concordam CASO A CASO.
    assert set(canonico_sp) == set(canonico_cp)
    for chave in canonico_sp:
        assert canonico_sp[chave] == canonico_cp[chave], (
            f"divergencia em '{chave}':\n"
            f"  savepoint {canonico_sp[chave]}\n"
            f"  copia     {canonico_cp[chave]}"
        )
    print(f"  {len(canonico_sp)} casos, todos identicos")


def test_a_copia_e_CONSISTENTE_e_nao_um_shutil_copy(conn, cenario, tmp_path):
    """A API de backup do SQLite, e não uma cópia de arquivo.

    O projeto roda em `journal_mode=WAL` (`store._PRAGMAS`), e nesse modo o
    arquivo principal pode estar atrás do `-wal`: copiar só ele produz um banco
    que perdeu transações — silenciosamente, e a certificação rodaria sobre um
    laboratório que não é o de produção.
    """
    import ast
    import inspect

    # Sem a PROSA: a versao anterior deste assert acusou o proprio docstring
    # que explica por que `shutil` esta proibido. Quarta vez que este projeto
    # cai nisso, e e por isso que `codigo_sem_prosa` existe - aqui basta
    # descartar as docstrings, porque o que se procura e uma chamada.
    arvore = ast.parse(inspect.getsource(_copiar))
    chamadas = {
        ast.unparse(n.func)
        for n in ast.walk(arvore)
        if isinstance(n, ast.Call)
    }
    assert "origem.backup" in chamadas, chamadas
    assert not any("shutil" in c for c in chamadas), chamadas

    destino = tmp_path / "c.sqlite3"
    _copiar(conn, destino)
    copia = __import__("app.store", fromlist=["conectar"]).conectar(destino)
    try:
        # O conteudo que importa atravessou.
        for tabela in ("hypothesis", "run", "ledger_entry", "config_version"):
            n_orig = conn.execute(
                f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
            n_copia = copia.execute(
                f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
            assert n_orig == n_copia, tabela
        # E o schema tambem: gatilhos sao o que barra 4 dos 6 controles.
        for tipo in ("table", "trigger"):
            a = {r[0] for r in conn.execute(
                f"SELECT name FROM sqlite_master WHERE type='{tipo}'")}
            b = {r[0] for r in copia.execute(
                f"SELECT name FROM sqlite_master WHERE type='{tipo}'")}
            assert a == b, f"{tipo}: {a ^ b}"
    finally:
        copia.close()


def test_um_COMMIT_na_copia_nao_alcanca_o_banco_oficial(conn, cenario, tmp_path):
    """O risco do SAVEPOINT, exercitado — e inofensivo na cópia.

    Este é o argumento inteiro a favor da cópia. No SAVEPOINT, um `commit()`
    dentro do caminho libera o savepoint e a contaminação é permanente. Aqui o
    mesmo `commit()` commita a cópia, que é apagada em seguida.
    """
    from app.a1a import braco
    from app.store import conectar

    dataset_id, cfg = cenario
    antes = _fotografia(conn)
    destino = tmp_path / "d.sqlite3"
    _copiar(conn, destino)

    copia = conectar(destino)
    try:
        braco.rodar(
            copia, dataset_id=dataset_id, config=cfg, config_version_id=1
        )
        # O commit que seria fatal no banco canonico.
        copia.commit()
        assert copia.execute(
            "SELECT COUNT(*) FROM hypothesis"
        ).fetchone()[0] > antes["hypothesis"]
    finally:
        copia.close()
    for p in tmp_path.glob("d.sqlite3*"):
        p.unlink()

    assert _fotografia(conn) == antes, (
        "um commit na copia alcancou o banco oficial - a premissa do desenho"
        " esta errada"
    )


def test_o_canonico_NAO_depende_de_id_temporario():
    """Chave do catálogo e `content_hash`, nunca `hypothesis_id`.

    Medido no experimento do SAVEPOINT: dos 6 `hypothesis_id` citados pelo
    resultado, **0 existiam** depois do rollback. Uma chave estrangeira no
    certificado apontaria para linha revertida.
    """
    import ast
    import inspect

    # Pelas STRINGS e ATRIBUTOS do codigo, e nao pelo texto do arquivo: o
    # docstring cita `hypothesis_id` justamente para dizer que ele nao serve.
    arvore = ast.parse(inspect.getsource(_canonico))
    literais = {
        n.value for n in ast.walk(arvore)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    atributos = {
        n.attr for n in ast.walk(arvore) if isinstance(n, ast.Attribute)
    }
    for proibido in ("hypothesis_id", "run_id"):
        assert proibido not in literais, f"{proibido} em literal"
        assert proibido not in atributos, f"{proibido} em atributo"
    assert "chave" in atributos
