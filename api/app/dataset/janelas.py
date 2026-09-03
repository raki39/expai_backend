"""Janelas de walk-forward, com purga e embargo (R29, R30, secoes 8.4 e 8.5.1).

> "O walk-forward historico simula a passagem do tempo, treinando ate
> determinado ponto e testando somente no periodo seguinte." — secao 8.5.1

Encadeamento para a frente, com treino expansivo: a janela `i` treina em tudo
que veio antes dela e testa so no pedaco seguinte. Nenhuma barra de teste de
uma janela aparece no treino da MESMA janela - e entre os dois ha um intervalo
removido, que e a purga mais o embargo.

## Por indice de barra, nunca por aritmetica de timestamp

Somar milissegundos para achar a fronteira cairia num buraco se houvesse
lacuna, e as janelas sairiam de tamanhos diferentes dos declarados. A grade de
barras existentes e a unica referencia confiavel - o mesmo motivo pelo qual
`proxima_barra` anda pela grade e nao pelo relogio.

## O intervalo removido nao e enfeite

A purga sai do maior lookback do CATALOGO (400 barras, `CruzamentoMedias.lenta`
- ver `purga.py`, onde a derivacao ja pegou um erro da D28). Sem ela, a
primeira barra de teste de uma media de 400 periodos e calculada com 400
barras de treino: o teste ja viu o treino, e nao de um jeito sutil.

O embargo, 1% da janela de teste, cobre a dependencia serial que sobra depois
que a sobreposicao mecanica foi removida.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import purga as purga_mod
from .split import conjunto

log = logging.getLogger(__name__)

# Secao 14.4, criterio B5: "o resultado se mantem em teste walk-forward fora da
# amostra, em pelo menos 3 janelas independentes". Tres e o PISO do documento,
# nao a escolha - por isso o valor tem nome e o nome cita a secao.
JANELAS_MINIMAS = 3


class JanelasImpossiveis(Exception):
    """O conjunto de walk-forward nao comporta as janelas pedidas."""


@dataclass(frozen=True)
class Janela:
    ordem: int
    treino_de_ms: int
    treino_ate_ms: int
    teste_de_ms: int
    teste_ate_ms: int
    purga_barras: int
    embargo_barras: int
    purga_origem: str

    @property
    def barras_removidas(self) -> int:
        return self.purga_barras + self.embargo_barras

    def como_dict(self) -> dict:
        return {
            "ordem": self.ordem,
            "treino_de_ms": self.treino_de_ms,
            "treino_ate_ms": self.treino_ate_ms,
            "teste_de_ms": self.teste_de_ms,
            "teste_ate_ms": self.teste_ate_ms,
            "purga_barras": self.purga_barras,
            "embargo_barras": self.embargo_barras,
            "purga_origem": self.purga_origem,
        }


def marcos(
    conn: sqlite3.Connection, dataset_id: int, quantos: int
) -> list[int]:
    """Os `quantos` primeiros instantes de abertura do dataset.

    Não devolve barra: devolve **onde** as barras começam. Existe porque o
    controle A1a precisa montar uma janela de walk-forward inválida, e montar
    uma exige conhecer a grade — mas escrever o `SELECT ... FROM bar` no
    controle furaria a fronteira do incremento 9, que é justamente a fronteira
    que o controle vizinho testa.

    Mesmo desenho de `selado.barras_do_holdout`: quem quer metadado pergunta
    ao módulo dono do dado, e a guarda não precisa distinguir intenção — ela
    não deveria mesmo.
    """
    if quantos < 1:
        raise ValueError("`quantos` precisa ser positivo")
    return _grade(conn, dataset_id)[:quantos]


def _grade(conn: sqlite3.Connection, dataset_id: int) -> list[int]:
    return [
        int(l["open_time_ms"])
        for l in conn.execute(
            "SELECT open_time_ms FROM bar WHERE dataset_id = ?"
            " ORDER BY open_time_ms",
            (dataset_id,),
        )
    ]


def planejar(
    conn: sqlite3.Connection, dataset_id: int, *, quantas: int = JANELAS_MINIMAS
) -> list[Janela]:
    """Calcula as janelas. Nao grava nada - `gerar` faz isso.

    Separado de proposito: da para conferir o plano sem consumir a decisao de
    fixa-lo, e a fixacao e imutavel por gatilho.
    """
    if quantas < JANELAS_MINIMAS:
        raise JanelasImpossiveis(
            f"a secao 14.4 (criterio B5) exige ao menos {JANELAS_MINIMAS}"
            f" janelas independentes; foram pedidas {quantas}"
        )

    wf = conjunto(conn, dataset_id, "walk_forward")
    if wf is None:
        raise JanelasImpossiveis(
            "o dataset nao tem conjunto de walk-forward; a divisao por"
            " finalidade nao foi criada"
        )

    grade = _grade(conn, dataset_id)
    if not grade:
        raise JanelasImpossiveis("o dataset nao tem barras")

    inicio_wf = next(
        (i for i, ms in enumerate(grade) if ms >= wf.from_ms), None
    )
    fim_wf = next(
        (i for i, ms in enumerate(grade) if ms >= wf.to_ms_exclusive),
        len(grade),
    )
    if inicio_wf is None or fim_wf - inicio_wf < quantas:
        raise JanelasImpossiveis(
            "o conjunto de walk-forward nao tem barras suficientes para"
            f" {quantas} janelas"
        )

    tamanho = (fim_wf - inicio_wf) // quantas
    sep = purga_mod.separacao(tamanho)
    removidas = sep.total_barras

    if inicio_wf - removidas <= 0:
        raise JanelasImpossiveis(
            f"purga mais embargo somam {removidas} barras e nao cabem antes da"
            f" primeira janela de teste: o treino ficaria vazio. Reduza o"
            " numero de janelas ou aumente o conjunto anterior"
        )

    janelas: list[Janela] = []
    for i in range(quantas):
        teste_ini = inicio_wf + i * tamanho
        teste_fim = fim_wf if i == quantas - 1 else teste_ini + tamanho
        treino_fim = teste_ini - removidas
        if treino_fim <= 0:
            raise JanelasImpossiveis(
                f"a janela {i + 1} nao tem treino depois de remover"
                f" {removidas} barras de purga e embargo"
            )
        janelas.append(
            Janela(
                ordem=i + 1,
                treino_de_ms=grade[0],
                # Exclusivo: a ultima barra de treino e a ANTERIOR a esta.
                treino_ate_ms=grade[treino_fim],
                teste_de_ms=grade[teste_ini],
                teste_ate_ms=(
                    grade[teste_fim] if teste_fim < len(grade) else grade[-1] + 1
                ),
                purga_barras=sep.purga_barras,
                embargo_barras=sep.embargo_barras,
                purga_origem=sep.purga_origem,
            )
        )
    return janelas


def gerar(
    conn: sqlite3.Connection, dataset_id: int, *, quantas: int = JANELAS_MINIMAS
) -> list[Janela]:
    """Fixa as janelas no banco. Idempotente: se ja existem, devolve-as.

    Imutaveis a partir daqui. Mover a fronteira depois de ver o resultado e
    exatamente o vazamento que a purga existe para impedir, so que feito a
    mao - e por isso o gatilho recusa `UPDATE`.
    """
    ja = ler(conn, dataset_id)
    if ja:
        return ja

    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    janelas = planejar(conn, dataset_id, quantas=quantas)
    for j in janelas:
        conn.execute(
            "INSERT INTO walk_forward_window (dataset_id, ordem, treino_de_ms,"
            " treino_ate_ms, teste_de_ms, teste_ate_ms, purga_barras,"
            " embargo_barras, purga_origem, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                dataset_id, j.ordem, j.treino_de_ms, j.treino_ate_ms,
                j.teste_de_ms, j.teste_ate_ms, j.purga_barras,
                j.embargo_barras, j.purga_origem, agora,
            ),
        )
    log.info(
        "walk_forward.janelas_fixadas",
        extra={
            "dataset_id": dataset_id,
            "quantas": len(janelas),
            "purga": janelas[0].purga_barras,
            "embargo": janelas[0].embargo_barras,
        },
    )
    return ler(conn, dataset_id)


def ler(conn: sqlite3.Connection, dataset_id: int) -> list[Janela]:
    return [
        Janela(
            ordem=int(l["ordem"]),
            treino_de_ms=int(l["treino_de_ms"]),
            treino_ate_ms=int(l["treino_ate_ms"]),
            teste_de_ms=int(l["teste_de_ms"]),
            teste_ate_ms=int(l["teste_ate_ms"]),
            purga_barras=int(l["purga_barras"]),
            embargo_barras=int(l["embargo_barras"]),
            purga_origem=l["purga_origem"],
        )
        for l in conn.execute(
            "SELECT ordem, treino_de_ms, treino_ate_ms, teste_de_ms,"
            " teste_ate_ms, purga_barras, embargo_barras, purga_origem"
            " FROM walk_forward_window WHERE dataset_id = ? ORDER BY ordem",
            (dataset_id,),
        )
    ]


def conferir_sem_vazamento(
    conn: sqlite3.Connection, dataset_id: int
) -> dict:
    """Prova, por consulta, que nenhuma janela mistura treino com teste.

    Derivado do que ficou GRAVADO, e nao do que o gerador pretendia fazer: um
    defeito no gerador que a conferencia lesse do mesmo lugar nao apareceria.
    """
    janelas = ler(conn, dataset_id)
    if not janelas:
        return {"conferido": None, "motivo": "nao ha janelas fixadas"}

    problemas: list[str] = []
    grade = _grade(conn, dataset_id)
    posicao = {ms: i for i, ms in enumerate(grade)}

    for j in janelas:
        # 1. O treino termina antes do teste comecar.
        if j.treino_ate_ms > j.teste_de_ms:
            problemas.append(
                f"janela {j.ordem}: treino invade o teste"
            )
        # 2. O intervalo removido e ao menos purga + embargo BARRAS.
        removidas = posicao.get(j.teste_de_ms, 0) - posicao.get(
            j.treino_ate_ms, 0
        )
        if removidas < j.barras_removidas:
            problemas.append(
                f"janela {j.ordem}: so {removidas} barras entre treino e"
                f" teste, e a purga mais o embargo pedem {j.barras_removidas}"
            )
        # 3. Nenhuma barra de teste dentro do treino.
        dentro = conn.execute(
            "SELECT COUNT(*) AS n FROM bar WHERE dataset_id = ?"
            " AND open_time_ms >= ? AND open_time_ms < ?"
            " AND open_time_ms >= ? AND open_time_ms < ?",
            (
                dataset_id, j.treino_de_ms, j.treino_ate_ms,
                j.teste_de_ms, j.teste_ate_ms,
            ),
        ).fetchone()["n"]
        if dentro:
            problemas.append(
                f"janela {j.ordem}: {dentro} barras em treino E teste"
            )

    return {
        "conferido": not problemas,
        "janelas": len(janelas),
        "purga_barras": janelas[0].purga_barras,
        "embargo_barras": janelas[0].embargo_barras,
        "purga_origem": janelas[0].purga_origem,
        "problemas": problemas,
    }
