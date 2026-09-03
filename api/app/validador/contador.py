"""O contador global de tentativas (R37, §8.6).

> "O sistema mantém um contador global de hipóteses testadas por especialidade.
> Esse contador **nunca é zerado**." — §8.6

## Derivado, nunca armazenado

Um número guardado numa coluna pode ser zerado por `UPDATE`. §8.6 diz que
*"descartar tentativas fracassadas do registro é o mecanismo exato que produz
falsas descobertas"* — então zerar não pode ser algo que se proíbe, tem que ser
algo que não existe.

`tentativas_por_especialidade` é uma view sobre `hypothesis`, que é
append-only por gatilho desde a migração 9. Não há caminho para diminuir o
número: seria preciso apagar uma hipótese, e o banco recusa.

## Tentativas, e não sucessos

O contador conta hipóteses **registradas**, não promovidas. É o número que
alimenta o DSR, e o DSR desconta pelo que foi tentado:

> "Um Sharpe de 1,5 após 10 tentativas e um Sharpe de 1,5 após 5.000 tentativas
> não são a mesma evidência." — §8.6

## Duas contagens diferentes, e a diferença custa 2 créditos

`tentativas` conta linhas. `hipoteses_distintas` conta hashes de conteúdo
diferentes. A diferença entre elas é quantos **retestes da mesma hipótese**
houve — e §8.6.1 cobra 1 crédito pelo teste in-sample e 3 pelo reteste com
parâmetro alterado, "porque varredura de parâmetro é a principal fonte de
sobreajuste".

Nenhum crédito é cobrado aqui: isso é o incremento 11. O que este módulo faz é
tornar a distinção **calculável** antes de ser cobrada.
"""

from __future__ import annotations

import sqlite3


def por_especialidade(conn: sqlite3.Connection) -> list[dict]:
    return [
        {
            "especialidade": l["especialidade"],
            "tentativas": int(l["tentativas"]),
            "hipoteses_distintas": int(l["hipoteses_distintas"]),
            "retestes": int(l["tentativas"]) - int(l["hipoteses_distintas"]),
            "nao_testaveis": int(l["nao_testaveis"]),
        }
        for l in conn.execute(
            "SELECT especialidade, tentativas, hipoteses_distintas,"
            " nao_testaveis FROM tentativas_por_especialidade"
            " ORDER BY especialidade"
        )
    ]


def total(conn: sqlite3.Connection) -> int:
    """O número que alimenta o DSR. Uma consulta, sem estado intermediário."""
    return int(
        conn.execute("SELECT COUNT(*) AS n FROM hypothesis").fetchone()["n"]
    )


def resumo(conn: sqlite3.Connection) -> dict:
    """Para o painel e para a auditoria de §14.4 (critério A4).

    A terceira condição do A4 é "nenhuma tentativa testada some do registro".
    Ela é conferida comparando este contador com o que o relatório afirma —
    duas leituras da mesma tabela, o que só pega divergência de código, e uma
    leitura contra o número gravado, que é o que pega perda de linha.
    """
    por = por_especialidade(conn)
    return {
        "total": total(conn),
        "por_especialidade": por,
        # De ONDE vem cada tentativa do total. O `total` alimenta o DSR e
        # soma TODAS as famílias — o que é deliberado (§8.6: "o contador
        # global é registro histórico") e declarado como brecha no incremento
        # 11, porque trocar de `config_version` abre família nova e o contador
        # não reseta.
        #
        # Sem esta quebra, o número fica inauditável: em produção o lote da
        # `config_version` 5 apareceu com `membros: []` ao lado de
        # `tentativas_globais: 1`, e não havia como descobrir de qual família
        # era aquela tentativa sem abrir o banco. O critério A4 de §14.4 exige
        # que "nenhuma tentativa testada some do registro" — e um total que
        # ninguém consegue decompor não é registro conferível.
        "por_config_version": [
            dict(l)
            for l in conn.execute(
                "SELECT r.config_version_id AS config_version_id,"
                "       COUNT(*) AS tentativas,"
                "       SUM(CASE WHEN h.testavel = 0 THEN 1 ELSE 0 END)"
                "         AS nao_testaveis"
                "  FROM hypothesis h JOIN run r ON r.id = h.run_id"
                " GROUP BY r.config_version_id"
                " ORDER BY r.config_version_id"
            )
        ],
        "retestes": sum(e["retestes"] for e in por),
        "nunca_zerado": (
            "derivado de `hypothesis`, que é append-only por gatilho:"
            " zerar exigiria apagar linha, e o banco recusa"
        ),
    }
