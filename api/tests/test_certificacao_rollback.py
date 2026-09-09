"""O caminho REAL roda dentro de um SAVEPOINT e reverte por inteiro?

> *"Avalie se é possível executar `promocao.avaliar_in_sample` dentro de uma
> transação/SAVEPOINT (…), capturar o resultado e reverter hipótese, crédito e
> transição. O certificado seria persistido depois, em transação separada."*
> — o usuário, 2026-09-09

**A resposta medida é SIM**, e este arquivo é a evidência em que o ADR 0040 se
apoia. Ele fica permanente porque a viabilidade depende de propriedades que
alguém pode quebrar sem perceber:

| o que a viabilidade exige | onde ele quebraria |
|---|---|
| nenhum `commit()` no caminho | existe **um** no repositório, em `api/rotas/dataset.py` — se alguém o copiasse para este caminho, o SAVEPOINT seria liberado e o rollback não desfaria nada |
| nenhum efeito externo | rede, arquivo, LLM ou `subprocess` não voltam com o rollback |
| `bloco_atomico` aninhável | SAVEPOINT, e não `BEGIN` — um `BEGIN` aninhado falha |

## O que foi medido, com o cenário completo (baselines + B4 + A1a)

| tabela | antes | dentro | depois |
|---|---|---|---|
| `hypothesis` | 16 | 22 | **16** |
| `hypothesis_state` (as transições) | 32 | 44 | **32** |
| `test_credit_entry` | 0 | 1 | **0** |
| `run` | 19 | 25 | **19** |
| `execution` | 162 | 498 | **162** |
| `ledger_entry` | 1091 | 3286 | **1091** |
| `agent_event` | 16 | 22 | **16** |
| `contador.total` — **o `N` do DSR** | 16 | 22 | **16** |

## E o achado que muda o desenho do certificado

O objeto de resultado **sobrevive** ao rollback — é Python, não banco. Mas os
`hypothesis_id` que ele cita **deixam de existir** (0 de 6 medidos). Então o
certificado tem de gravar **conteúdo**, e nunca ponteiro para `hypothesis`: uma
chave estrangeira ali apontaria para linha revertida.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.config.schema import ExperimentConfig
from tests.test_portao_a import cenario  # noqa: F401
from tests.test_cerebro import settings  # noqa: F401


def _fotografia(conn: sqlite3.Connection) -> dict:
    def um(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "hypothesis": um("SELECT COUNT(*) FROM hypothesis"),
        "test_credit_entry": um("SELECT COUNT(*) FROM test_credit_entry"),
        "hypothesis_state": um("SELECT COUNT(*) FROM hypothesis_state"),
        "run": um("SELECT COUNT(*) FROM run"),
        "execution": um("SELECT COUNT(*) FROM execution"),
        "ledger_entry": um("SELECT COUNT(*) FROM ledger_entry"),
        "agent_event": um("SELECT COUNT(*) FROM agent_event"),
    }


def test_experimento_rollback_do_caminho_real(conn: sqlite3.Connection, cenario):
    """Roda A1a inteiro dentro de um SAVEPOINT e desfaz. Sobra alguma coisa?"""
    from app.a1a import braco
    from app.validador import contador

    dataset_id, cfg = cenario
    antes = _fotografia(conn)
    tentativas_antes = contador.total(conn)
    print("\nANTES:", antes, "| contador:", tentativas_antes)

    # in_transaction diz se o driver ve transacao aberta.
    print("in_transaction antes do SAVEPOINT:", conn.in_transaction)

    conn.execute("SAVEPOINT experimento")
    try:
        resultado = braco.rodar(
            conn, dataset_id=dataset_id, config=cfg, config_version_id=1
        )
        dentro = _fotografia(conn)
        print("in_transaction dentro:", conn.in_transaction)
        print("DENTRO:", dentro, "| contador:", contador.total(conn))
        print("controles:", [(c.chave, c.hypothesis_id, c.barrado)
                             for c in resultado.controles])
        # O resultado FOI produzido pelo caminho real?
        assert len(resultado.controles) == 6
        assert dentro["hypothesis"] > antes["hypothesis"]
    finally:
        conn.execute("ROLLBACK TO experimento")
        conn.execute("RELEASE experimento")

    depois = _fotografia(conn)
    print("DEPOIS:", depois, "| contador:", contador.total(conn))
    print("in_transaction depois:", conn.in_transaction)

    for tabela, valor in antes.items():
        assert depois[tabela] == valor, (
            f"{tabela}: {valor} antes, {depois[tabela]} depois - o rollback"
            " NAO desfez tudo"
        )
    assert contador.total(conn) == tentativas_antes


def test_experimento_o_resultado_SOBREVIVE_ao_rollback(
    conn: sqlite3.Connection, cenario
):
    """O veredito e um objeto Python: ele sobrevive ao rollback do banco?"""
    from app.a1a import braco

    dataset_id, cfg = cenario
    conn.execute("SAVEPOINT exp2")
    try:
        r = braco.rodar(
            conn, dataset_id=dataset_id, config=cfg, config_version_id=1
        )
        capturado = r.como_dict()
    finally:
        conn.execute("ROLLBACK TO exp2")
        conn.execute("RELEASE exp2")

    # O dicionario capturado nao depende mais do banco.
    print("\ncapturado apos rollback:", capturado["mensagem"])
    print("promovidos:", capturado["promovidos"])
    print("quantos:", capturado["quantos"])
    assert capturado["quantos"] == 6
    assert capturado["promovidos"] == []
    # E as linhas que ele CITA nao existem mais - o certificado teria de
    # gravar o CONTEUDO, e nao ponteiros.
    ids = [c["hypothesis_id"] for c in capturado["controles"]]
    existem = conn.execute(
        f"SELECT COUNT(*) FROM hypothesis WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    ).fetchone()[0]
    print("hypothesis_id citados que ainda existem:", existem, "de", len(ids))
    assert existem == 0


def test_experimento_gravar_DEPOIS_em_transacao_separada(
    conn: sqlite3.Connection, cenario
):
    """Depois de reverter, dá para gravar o certificado numa transação nova?"""
    from app.a1a import braco

    dataset_id, cfg = cenario
    conn.execute("CREATE TABLE certificado_experimento (payload TEXT NOT NULL)")

    conn.execute("SAVEPOINT exp3")
    try:
        r = braco.rodar(
            conn, dataset_id=dataset_id, config=cfg, config_version_id=1
        )
        import json

        payload = json.dumps(r.como_dict(), ensure_ascii=False)
    finally:
        conn.execute("ROLLBACK TO exp3")
        conn.execute("RELEASE exp3")

    conn.execute(
        "INSERT INTO certificado_experimento (payload) VALUES (?)", (payload,)
    )
    n = conn.execute(
        "SELECT COUNT(*) FROM certificado_experimento"
    ).fetchone()[0]
    print("\ncertificado gravado depois do rollback:", n)
    assert n == 1
    restou = conn.execute("SELECT COUNT(*) FROM hypothesis").fetchone()[0]
    print("hipoteses que restaram (as 16 de B4 do cenario):", restou)
    assert restou == 16


def test_experimento_o_CREDITO_reverte(conn: sqlite3.Connection, cenario):
    """A cobranca de credito e um INSERT no mesmo SAVEPOINT. Reverte?

    O caminho de `_avaliar` cobra por `creditos.cobrar` antes de julgar. Aqui
    exercito a cobranca direta, porque no cenario de teste os controles saem
    todos barrados ou ja avaliados e a cobranca nao e alcancada.
    """
    from app import creditos as creditos_mod

    antes_n = _fotografia(conn)["test_credit_entry"]
    antes_saldo = creditos_mod.saldo(conn, braco="b4", config_version_id=1)
    hid = int(conn.execute(
        "SELECT id FROM hypothesis ORDER BY id LIMIT 1").fetchone()["id"])

    conn.execute("SAVEPOINT exp5")
    try:
        cobrado = creditos_mod.cobrar(
            conn, hypothesis_id=hid, tipo="in_sample", braco="b4",
            config_version_id=1, cpu_micros=1, barras_reservadas=0,
            familia_max=48,
        )
        dentro_n = _fotografia(conn)["test_credit_entry"]
        dentro_saldo = creditos_mod.saldo(
            conn, braco="b4", config_version_id=1)
        print("DENTRO: linhas", dentro_n, "| consumido",
              dentro_saldo.consumido, "| cobrado", cobrado)
        assert dentro_n > antes_n
    finally:
        conn.execute("ROLLBACK TO exp5")
        conn.execute("RELEASE exp5")

    depois_n = _fotografia(conn)["test_credit_entry"]
    depois_saldo = creditos_mod.saldo(conn, braco="b4", config_version_id=1)
    print("ANTES:", antes_n, antes_saldo.consumido,
          "| DEPOIS:", depois_n, depois_saldo.consumido)
    assert depois_n == antes_n
    assert depois_saldo.consumido == antes_saldo.consumido


def test_NENHUM_commit_no_caminho_da_certificacao():
    """A guarda que protege a viabilidade medida acima.

    Existe **um** `conn.commit()` no repositório, em `api/rotas/dataset.py`.
    Ele é legítimo ali e fatal aqui: um commit dentro do caminho liberaria o
    SAVEPOINT, e o `ROLLBACK TO` deixaria de desfazer — silenciosamente, com o
    certificado parecendo correto e a família de 48 já contaminada.

    Varre a FORMA, e não um arquivo: um módulo novo no caminho é pego sozinho.
    """
    import pathlib

    from tests._prosa import codigo_sem_prosa

    caminho = [
        "app/a1a/braco.py", "app/a1a/injecoes.py", "app/a1a/catalogo.py",
        "app/validador/promocao.py", "app/validador/estados.py",
        "app/creditos.py", "app/hipotese/registro.py",
        "app/hipotese/veredito.py", "app/hipotese/escala.py",
        "app/simulador/execucao.py", "app/ledger/livro.py",
        "app/maos_rapidas/executor.py", "app/b4/braco.py", "app/b4/busca.py",
    ]
    for nome in caminho:
        arquivo = pathlib.Path(nome)
        assert arquivo.exists(), f"{nome} saiu do lugar: revise esta lista"
        codigo = codigo_sem_prosa(arquivo)
        assert "commit" not in codigo, (
            f"{nome} chama commit(): isso LIBERA o SAVEPOINT e o rollback da"
            " certificacao deixa de desfazer. Ver o docstring deste modulo"
        )
        for externo in ("requests", "httpx", "urllib", "subprocess"):
            assert externo not in codigo, (
                f"{nome} toca {externo}: efeito externo nao volta com o"
                " rollback, e a certificacao deixaria rastro fora do banco"
            )


def test_o_commit_legitimo_EXISTE_e_esta_fora_do_caminho():
    """Não-vacuidade: a guarda acima passaria num repositório sem commit nenhum.

    Verificado no modo de falha — se este `commit()` desaparecesse, a guarda de
    cima ficaria afirmando uma proteção contra algo que não existe mais, que é
    a forma do `volume_gravavel` e do `BLOCOS`.
    """
    import pathlib

    fonte = pathlib.Path("app/api/rotas/dataset.py").read_text(encoding="utf-8")
    assert "conn.commit()" in fonte, (
        "o commit de referencia saiu de `api/rotas/dataset.py`: a guarda de"
        " cima perdeu o contra-exemplo, e precisa de um novo ou de revisao"
    )
