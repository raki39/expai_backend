"""Os quatro conjuntos por finalidade (R26, D27, secao 8.5.1).

| Conjunto | Fatia | Acesso | Finalidade |
|---|---|---|---|
| Exploracao | 20% | agente | conhecer o mercado e formular hipoteses |
| In-sample | 30% | agente | desenvolver e ajustar estrategias |
| Walk-forward | 30% | **validador** | decisoes sequenciais em periodo nao usado no ajuste |
| Holdout selado | 20% | **validador** | teste final, uso unico |

Contiguos e em ordem cronologica. **Nunca embaralhados** - secao 8.4: "nada de
embaralhamento aleatorio de dados temporais".

## O holdout nao PASSA A SER a reserva: ele sempre foi

A reserva da D11 foi carvada na ingestao do incremento 1 e nunca foi lida por
nada. O que este modulo faz e dar a ela o nome e a permissao que a secao 8.5.1
usa. O corte e o mesmo `reserved_from_ms`, e ha teste provando que o intervalo
e identico ao gravado la.

Isso importa porque a alternativa - criar um holdout novo agora - significaria
selar um periodo que ja foi visto. Um holdout escolhido depois de olhar os
dados nao e holdout, e uma amostra com nome bonito.

## A fronteira e da ESTRUTURA, nao da disciplina

`bar_por_finalidade` carrega `acesso` em cada linha, e o SQL do caminho do
agente traz `acesso = 'agente'` como LITERAL - nao como parametro. Nao existe
argumento capaz de fazer aquela consulta devolver walk-forward ou holdout,
pelo mesmo motivo que nao existe argumento capaz de fazer `bar_experimento`
devolver a reserva.

Secao 8.5.1: "A separacao e garantida pela estrutura de dados e pelas
permissoes da ferramenta, nao pela disciplina do agente (...) Um holdout que
depende de boa vontade ja foi consumido."
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

Finalidade = Literal["exploracao", "in_sample", "walk_forward", "holdout"]

# D27. Em partes por dez mil, para nao haver ponto flutuante decidindo
# fronteira de dado. O holdout nao aparece aqui: ele e o que sobra a partir de
# `reserved_from_ms`, e sobrar e o ponto - a fatia dele foi decidida na
# ingestao (D11) e nao e recalculada agora.
FATIAS_BPS: dict[str, int] = {
    "exploracao": 2_000,
    "in_sample": 3_000,
    "walk_forward": 3_000,
}

# A fatia do holdout, so para derivar quanto sobra para as outras tres. O
# corte de verdade e o `reserved_from_ms` gravado na ingestao (D11), e este
# numero nunca o recalcula - se os dois divergirem, quem manda e o gravado.
FATIAS_BPS_RESERVA = 2_000

ACESSO: dict[str, str] = {
    "exploracao": "agente",
    "in_sample": "agente",
    "walk_forward": "validador",
    "holdout": "validador",
}

# O que o AGENTE pode pedir. O holdout nao esta aqui, e o walk-forward
# tampouco: os dois tem caminho proprio, no modulo do validador.
FINALIDADES_DO_AGENTE: tuple[str, ...] = ("exploracao", "in_sample")


class DivisaoInvalida(Exception):
    pass


class FinalidadeProibida(Exception):
    """Pedido de leitura por um caminho que nao tem essa permissao."""


@dataclass(frozen=True)
class Conjunto:
    finalidade: str
    from_ms: int
    to_ms_exclusive: int
    bars: int
    acesso: str

    def como_dict(self) -> dict:
        return {
            "finalidade": self.finalidade,
            "from_ms": self.from_ms,
            "to_ms_exclusive": self.to_ms_exclusive,
            "barras": self.bars,
            "acesso": self.acesso,
        }


def _fronteiras(
    conn: sqlite3.Connection, dataset_id: int
) -> list[tuple[str, int, int]]:
    """As fronteiras em ms, calculadas sobre a GRADE de barras existentes.

    Por indice de barra, e nao por aritmetica de timestamp: se houvesse
    lacuna, dividir o intervalo de tempo em pedacos daria conjuntos de
    tamanhos diferentes dos declarados, e a divisao nao seria a que a D27 diz.
    """
    aberturas = [
        int(l["open_time_ms"])
        for l in conn.execute(
            "SELECT open_time_ms FROM bar WHERE dataset_id = ?"
            " ORDER BY open_time_ms",
            (dataset_id,),
        )
    ]
    if not aberturas:
        raise DivisaoInvalida(f"dataset {dataset_id} nao tem barras")

    reserva = conn.execute(
        "SELECT reserved_from_ms FROM dataset WHERE id = ?", (dataset_id,)
    ).fetchone()
    if reserva is None:
        raise DivisaoInvalida(f"dataset {dataset_id} nao existe")
    reservada_de = int(reserva["reserved_from_ms"])

    fim = aberturas[-1] + 1  # exclusivo, cobre a ultima barra

    # **A reserva da D11 e a AUTORIDADE, e as tres fatias dividem o que sobra
    # dela.** Nao o contrario.
    #
    # A primeira versao calculou as tres fatias sobre o total e conferiu se
    # elas terminavam na reserva. Errou por uma barra: tres truncamentos
    # independentes de 20%, 30% e 30% nao somam o mesmo que um truncamento de
    # 80%. O teste pegou, e a licao e a de sempre neste projeto - a fronteira
    # que ja existe manda, e a que esta sendo criada se ajusta a ela. Mover o
    # `reserved_from_ms` para fechar a conta seria mexer num corte carvado na
    # ingestao, com dado ja visto do outro lado.
    corte = next(
        (i for i, ms in enumerate(aberturas) if ms >= reservada_de), None
    )
    if corte is None or corte == 0:
        raise DivisaoInvalida(
            f"a reserva da D11 comeca em {reservada_de}, fora da grade de"
            " barras deste dataset"
        )

    # Proporcoes DENTRO do que nao e reservado. 20/30/30 do total sao
    # 25/37,5/37,5 dos 80% - e a ultima fatia leva o resto, o que garante
    # cobertura exata sem depender de arredondamento.
    disponivel = 10_000 - FATIAS_BPS_RESERVA
    cortes: list[tuple[str, int, int]] = []
    inicio_idx = 0
    ordem = ("exploracao", "in_sample", "walk_forward")
    for nome in ordem[:-1]:
        quantas = corte * FATIAS_BPS[nome] // disponivel
        fim_idx = inicio_idx + quantas
        if fim_idx >= corte:
            raise DivisaoInvalida(
                f"a fatia de '{nome}' nao cabe: ha {corte} barras antes da"
                " reserva"
            )
        cortes.append((nome, aberturas[inicio_idx], aberturas[fim_idx]))
        inicio_idx = fim_idx

    if inicio_idx >= corte:
        raise DivisaoInvalida(
            "nao sobrou barra para o walk-forward depois das fatias anteriores"
        )
    cortes.append((ordem[-1], aberturas[inicio_idx], reservada_de))
    cortes.append(("holdout", reservada_de, fim))
    return cortes


def criar(conn: sqlite3.Connection, dataset_id: int) -> list[Conjunto]:
    """Grava os quatro conjuntos. Idempotente: se ja existem, devolve-os.

    Fixados uma vez e imutaveis por gatilho. Mover a fronteira depois seria
    contaminar o conjunto do outro lado - e a tentacao de faze-lo aparece
    exatamente quando o resultado nao agrada, que e quando ela e mais cara.
    """
    ja = ler(conn, dataset_id)
    if ja:
        return ja

    for nome, de, ate in _fronteiras(conn, dataset_id):
        quantas = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM bar WHERE dataset_id = ?"
                " AND open_time_ms >= ? AND open_time_ms < ?",
                (dataset_id, de, ate),
            ).fetchone()["n"]
        )
        conn.execute(
            "INSERT INTO dataset_split (dataset_id, finalidade, from_ms,"
            " to_ms_exclusive, bars, acesso) VALUES (?,?,?,?,?,?)",
            (dataset_id, nome, de, ate, quantas, ACESSO[nome]),
        )
    conjuntos = ler(conn, dataset_id)
    log.info(
        "dataset.dividido",
        extra={
            "dataset_id": dataset_id,
            "conjuntos": {c.finalidade: c.bars for c in conjuntos},
        },
    )
    return conjuntos


def ler(conn: sqlite3.Connection, dataset_id: int) -> list[Conjunto]:
    return [
        Conjunto(
            finalidade=l["finalidade"],
            from_ms=int(l["from_ms"]),
            to_ms_exclusive=int(l["to_ms_exclusive"]),
            bars=int(l["bars"]),
            acesso=l["acesso"],
        )
        for l in conn.execute(
            "SELECT finalidade, from_ms, to_ms_exclusive, bars, acesso"
            " FROM dataset_split WHERE dataset_id = ? ORDER BY from_ms",
            (dataset_id,),
        )
    ]


def conjunto(
    conn: sqlite3.Connection, dataset_id: int, finalidade: str
) -> Conjunto | None:
    for c in ler(conn, dataset_id):
        if c.finalidade == finalidade:
            return c
    return None


def exigir_do_agente(finalidade: str) -> str:
    """Portao do caminho do agente. Levanta se a finalidade nao for dele.

    Existe alem do literal no SQL, e nao no lugar dele: o SQL impede que o
    dado saia, e isto faz o pedido errado FALHAR ALTO em vez de devolver zero
    barras em silencio. Uma lista vazia seria lida como "nao ha dado", que e
    coisa diferente de "voce nao pode ver isto".
    """
    if finalidade not in FINALIDADES_DO_AGENTE:
        raise FinalidadeProibida(
            f"'{finalidade}' nao e um conjunto do agente. O agente le"
            f" {list(FINALIDADES_DO_AGENTE)}; walk-forward e holdout sao do"
            " validador (secao 8.5.1), e o holdout tem uso unico por hipotese"
        )
    return finalidade


def resumo(conn: sqlite3.Connection, dataset_id: int) -> dict:
    """Para o painel. Nao devolve barra nenhuma, so onde cada conjunto comeca."""
    conjuntos = ler(conn, dataset_id)
    return {
        "dataset_id": dataset_id,
        "dividido": bool(conjuntos),
        "conjuntos": [c.como_dict() for c in conjuntos],
        "acessivel_ao_agente": list(FINALIDADES_DO_AGENTE),
    }
