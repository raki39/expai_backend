"""As duas séries que o protocolo precisa, e elas NÃO são a mesma.

**CORREÇÃO BLOQUEANTE 1**, construída em 2026-09-08 depois do rastreamento que o
usuário exigiu. O defeito: todas as estatísticas do projeto liam
`executor.retornos_do_run`, que é a série do **MERCADO** na janela executada.

Medido antes de corrigir, e o número é o argumento inteiro:

| | |
|---|---|
| patrimônio final | **83.741** contra 100.000 de semente — perdeu 16% |
| "Sharpe realizado" que o protocolo publicava | **+25,78** |
| p-valor que ia ao BY | **846 ppm** (o limiar da 1ª rejeição é 467) |
| Sharpe da equity da própria estratégia | **−17,06** |

*"Se isso passasse despercebido, um agente poderia parecer genial simplesmente
porque o Bitcoin subiu."* — e é literalmente isso que a medição mostra.

## São DOIS objetos, e forçá-los a ser um seria repetir o erro

| série | o que é | quem consome |
|---|---|---|
| **`retorno_da_estrategia`** | retorno por barra da **equity da própria estratégia** | p-valor → BY, e o DSR pela definição oficial dele |
| **`excesso_incremental`** | `Δequity_candidata − Δequity_B3`, na mesma grade | variância, autocorrelação e `n_efetivo` da D48 quando o efeito é "sobre o B3"; e o CUSUM quando houver candidata |

O usuário foi explícito: *"Não force todas as estatísticas a usar a mesma série
se elas estimam objetos diferentes."* O DSR mede o Sharpe **da estratégia**
deflacionado por tentativas; a D48 dimensiona um efeito **de diferença**. Uma
série só para os dois seria trocar um erro por outro.

## A marcação é causal, e é a mesma para os dois lados

```
equity_t = caixa_t + posição_t × preço_de_marcação_t
```

`preço_de_marcação_t` é o **fechamento da barra `t` do dataset** — o mesmo
objeto para candidata e B3, porque o excesso só significa algo se os dois forem
marcados pelo mesmo preço no mesmo instante. Operações **não precisam ocorrer
juntas**: o que casa é a grade de avaliação, e não o giro.

### E o caixa inclui o pensamento

`curva.curva_do_run` soma apenas `delta_caixa` das **execuções** — foi assim que
nasceu a diferença de US$ 0,09 entre o número-herói e a tabela, na mesma tela.
Aqui todo lançamento em `CAIXA_SIM` entra.

**Os que não são de execução caem na PRIMEIRA barra**, e isso não é
aproximação de conveniência: é a sequência real. O grafo do cérebro termina
**antes** da execução (o `registrar_intencao` é o último nó), então a reflexão é
paga antes de a primeira ordem existir. Colocá-la no fim faria a curva subir
durante um período em que o dinheiro já tinha saído.

## As garantias, e cada uma é testada

1. mesma grade e mesmo preço de marcação para candidata e B3;
2. operações não precisam ocorrer juntas;
3. custos de execução **e de pensamento** incluídos;
4. o comparador B3 é identificado por `run_id` **e** `run_digest`;
5. mesma janela, timeframe, dataset e identidade executável compatível;
6. a equity final **reconcilia com o ledger**;
7. a soma do excesso incremental **reconstrói exatamente** a diferença final;
8. barra ausente segue a política da D40 — **nunca vira retorno zero**;
9. retorno do mercado **nunca** substitui retorno da estratégia.

A nona é guarda de código, e não boa vontade: há teste varrendo os módulos de
estatística por `retornos_do_run`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Sequence

from ..dataset.loader import BarraCarregada
from ..ledger import contas
from .curva import valor_da_posicao_cents

#: A política da D40 para barra ausente, e ela é a mesma do CUSUM.
#:
#: Zero é uma **afirmação** sobre o retorno: dizer que a equity não se moveu num
#: instante em que ninguém observou o preço inventa um dado. A série carrega o
#: último valor e **conta** as ausências, exatamente como `S_t = S_{t−1}` faz no
#: monitor.
POLITICA_BARRA_AUSENTE = (
    "barra ausente NAO vira retorno zero (D40): a equity carrega o ultimo valor"
    " conhecido e a ausencia e CONTADA. Zero seria afirmar que nada se moveu"
    " num instante em que ninguem observou o preco"
)


class SeriesIncompativeis(Exception):
    """Os dois runs não podem ser diferenciados barra a barra.

    Levanta em vez de devolver número: um excesso calculado entre janelas,
    datasets ou identidades executáveis diferentes tem a aparência de um
    resultado e não é comparação nenhuma.
    """


@dataclass(frozen=True)
class Comparador:
    """Quem é o outro lado da diferença, identificado para sempre.

    `run_id` sozinho aponta para uma linha; `run_digest` diz **qual resultado
    econômico** aquela linha produziu. É o mesmo par que a quarentena congela,
    e pelo mesmo motivo: um id pode ser reapontado, um digest não.
    """

    run_id: int
    run_digest: str
    identidade_executavel: str
    dataset_id: int
    timeframe: str
    de_ms: int
    ate_ms: int
    barras: int

    def como_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "run_digest": self.run_digest,
            "identidade_executavel": self.identidade_executavel,
            "dataset_id": self.dataset_id,
            "timeframe": self.timeframe,
            "de_ms": self.de_ms,
            "ate_ms": self.ate_ms,
            "barras": self.barras,
        }


@dataclass(frozen=True)
class Equity:
    """A equity reconstruída barra a barra, com a reconciliação ao lado."""

    run_id: int
    valores_cents: tuple[int, ...]
    posicao_final_sats: int
    caixa_final_cents: int
    ledger_caixa_cents: int
    custo_de_pensar_cents: int
    barras_ausentes: int

    @property
    def reconcilia_com_o_ledger(self) -> bool:
        """O caixa reconstruído bate com o saldo da conta.

        **A posição final entra por fora**: `caixa_cents` é caixa, e a equity é
        caixa mais posição marcada. Quando o run termina sem posição — o caso
        de toda regra deste projeto, que fecha no fim — os dois coincidem.
        """
        return self.caixa_final_cents == self.ledger_caixa_cents

    def como_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "barras": len(self.valores_cents),
            "equity_final_cents": (
                self.valores_cents[-1] if self.valores_cents else None
            ),
            "posicao_final_sats": self.posicao_final_sats,
            "caixa_final_cents": self.caixa_final_cents,
            "ledger_caixa_cents": self.ledger_caixa_cents,
            "custo_de_pensar_cents": self.custo_de_pensar_cents,
            "reconcilia_com_o_ledger": self.reconcilia_com_o_ledger,
            "barras_ausentes": self.barras_ausentes,
        }


@dataclass(frozen=True)
class SerieDeExcesso:
    """`Δequity_candidata − Δequity_B3`, e a prova de que ela fecha."""

    candidata: Comparador
    b3: Comparador
    incremental_cents: tuple[int, ...]
    soma_cents: int
    diferenca_final_cents: int
    equity_candidata: Equity
    equity_b3: Equity

    @property
    def reconstroi_a_diferenca(self) -> bool:
        """A garantia 7, e é ela que torna a série utilizável.

        Se a soma não reconstruísse a diferença final, a série descreveria
        outra coisa — e a variância que sai dela dimensionaria outro efeito.
        """
        return self.soma_cents == self.diferenca_final_cents

    def como_dict(self) -> dict:
        return {
            "candidata": self.candidata.como_dict(),
            "b3": self.b3.como_dict(),
            "barras": len(self.incremental_cents),
            "soma_cents": self.soma_cents,
            "diferenca_final_cents": self.diferenca_final_cents,
            "reconstroi_a_diferenca": self.reconstroi_a_diferenca,
            "equity_candidata": self.equity_candidata.como_dict(),
            "equity_b3": self.equity_b3.como_dict(),
            "politica_barra_ausente": POLITICA_BARRA_AUSENTE,
        }


# ---------------------------------------------------------------------------
# A equity, e ela inclui TODO lançamento no caixa simulado
# ---------------------------------------------------------------------------


def equity_por_barra(
    conn: sqlite3.Connection, run_id: int, *, barras: Sequence[BarraCarregada]
) -> Equity:
    """A equity da estratégia, barra a barra, na grade dada.

    **Não recalcula preço nem custo:** cada movimento de caixa vem do ledger,
    que é a autoridade sobre dinheiro (regra 16). Recalcular aqui criaria uma
    segunda aritmética do dinheiro — que é como duas fontes de verdade começam.

    O que esta função acrescenta a `curva.curva_do_run` é **o custo do
    pensamento**. A curva soma só as execuções, e é por isso que ela e o
    `caixa_cents` divergiam em nove centavos num run do agente.
    """
    if not barras:
        return Equity(
            run_id=run_id, valores_cents=(), posicao_final_sats=0,
            caixa_final_cents=0, ledger_caixa_cents=0,
            custo_de_pensar_cents=0, barras_ausentes=0,
        )

    caixa = int(
        conn.execute(
            "SELECT COALESCE(SUM(e.amount_minor), 0) AS c"
            "  FROM ledger_entry e"
            "  JOIN ledger_transaction t ON t.id = e.transaction_id"
            "  JOIN account a ON a.id = e.account_id"
            " WHERE t.run_id = ? AND t.kind = 'abertura' AND a.code = ?",
            (run_id, contas.CAIXA_SIM),
        ).fetchone()["c"]
    )

    # Tudo que tocou o caixa simulado e NAO veio de uma execucao: reflexao,
    # estorno, o que for. Cai na PRIMEIRA barra, porque o grafo do cerebro
    # termina antes da execucao - a reflexao e paga antes de existir ordem.
    fora_da_execucao = int(
        conn.execute(
            "SELECT COALESCE(SUM(e.amount_minor), 0) AS c"
            "  FROM ledger_entry e"
            "  JOIN ledger_transaction t ON t.id = e.transaction_id"
            "  JOIN account a ON a.id = e.account_id"
            " WHERE t.run_id = ? AND a.code = ? AND t.kind <> 'abertura'"
            "   AND t.id NOT IN ("
            "       SELECT ledger_transaction_id FROM execution"
            "        WHERE run_id = ? AND ledger_transaction_id IS NOT NULL)",
            (run_id, contas.CAIXA_SIM, run_id),
        ).fetchone()["c"]
    )
    caixa += fora_da_execucao

    execucoes = list(
        conn.execute(
            "SELECT e.execution_bar_ms AS t, e.side, e.quantity_sats,"
            "       (SELECT COALESCE(SUM(le.amount_minor), 0)"
            "          FROM ledger_entry le"
            "          JOIN account a ON a.id = le.account_id"
            "         WHERE le.transaction_id = e.ledger_transaction_id"
            "           AND a.code = ?) AS delta_caixa"
            "  FROM execution e"
            " WHERE e.run_id = ?"
            " ORDER BY e.execution_bar_ms, e.id",
            (contas.CAIXA_SIM, run_id),
        )
    )

    # Barra ausente: a grade ESPERADA sai do intervalo entre as barras; o que
    # falta e contado, e nunca preenchido com zero (D40).
    ausentes = _barras_ausentes(barras)

    posicao = 0
    proxima = 0
    valores: list[int] = []
    for barra in barras:
        while (
            proxima < len(execucoes)
            and execucoes[proxima]["t"] <= barra.open_time_ms
        ):
            ex = execucoes[proxima]
            caixa += int(ex["delta_caixa"])
            posicao += (
                int(ex["quantity_sats"])
                if ex["side"] == "compra"
                else -int(ex["quantity_sats"])
            )
            proxima += 1
        valores.append(caixa + valor_da_posicao_cents(posicao, barra.close))

    from ..simulador import execucao as simulador

    return Equity(
        run_id=run_id,
        valores_cents=tuple(valores),
        posicao_final_sats=posicao,
        caixa_final_cents=caixa,
        ledger_caixa_cents=simulador.caixa_cents(conn, run_id),
        custo_de_pensar_cents=fora_da_execucao,
        barras_ausentes=ausentes,
    )


def _barras_ausentes(barras: Sequence[BarraCarregada]) -> int:
    """Quantas barras a grade regular teria e não estão presentes.

    O passo sai da MEDIANA dos intervalos observados, e não de uma constante:
    fixar 15 minutos aqui faria o número parar de descrever no dia em que o
    timeframe mudasse — o padrão que este projeto conta vinte e oito vezes.
    """
    if len(barras) < 3:
        return 0
    passos = sorted(
        b.open_time_ms - a.open_time_ms for a, b in zip(barras, barras[1:])
    )
    passo = passos[len(passos) // 2]
    if passo <= 0:
        return 0
    esperadas = (barras[-1].open_time_ms - barras[0].open_time_ms) // passo + 1
    return max(0, esperadas - len(barras))


# ---------------------------------------------------------------------------
# 1. A série da ESTRATÉGIA — p-valor e DSR
# ---------------------------------------------------------------------------


def retorno_da_estrategia(
    conn: sqlite3.Connection, run_id: int, *, barras: Sequence[BarraCarregada]
) -> list[int]:
    """Retorno por barra da EQUITY da estratégia, em bps inteiros.

    **Esta é a série que o p-valor e o DSR precisam.** A que eles usavam era
    `executor.retornos_do_run`, do mercado — e por isso uma regra que perdeu
    16% do capital recebia Sharpe +25,78 num mercado que subiu 198,8%.

    Barra com equity anterior nula ou negativa devolve zero: dividir por ela
    produziria um número sem significado, e a alternativa (pular a barra)
    romperia a correspondência com a grade.
    """
    eq = equity_por_barra(conn, run_id, barras=barras)
    v = eq.valores_cents
    return [
        0 if v[i - 1] <= 0 else (v[i] - v[i - 1]) * 10_000 // v[i - 1]
        for i in range(1, len(v))
    ]


def serie_do_run(conn: sqlite3.Connection, run_id: int) -> list[int]:
    """A serie da estrategia de um run, carregando a grade do dataset dele.

    **A DEFINICAO, e as duas partes leem daqui.** O run 30 mostrou o que
    acontece quando ha duas: o agente e o validador publicaram `n_efetivo`
    diferentes para a mesma hipotese - 11.023 contra 10.976 -, e `n_efetivo`
    decide entre `refutada` e `inconclusiva` (secao 14.4).

    A correcao daquela vez criou `executor.retornos_do_run` como definicao
    unica. Ela continua unica; o que mudou em 2026-09-08 foi o OBJETO que ela
    devolvia - mercado, e nao estrategia. Trocar so um dos lados teria
    devolvido a divergencia, e a guarda do run 30 acusou exatamente isso na
    primeira tentativa.

    Devolve lista vazia quando o run nao executou nada, que e o caminho que
    quem chama ja trata como serie curta.
    """
    from . import executor

    linha = conn.execute(
        "SELECT MIN(dataset_id) AS ds FROM execution WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if linha is None or linha["ds"] is None:
        return []
    barras = executor.carregar_janela(conn, int(linha["ds"]))
    return retorno_da_estrategia(conn, run_id, barras=barras)


# ---------------------------------------------------------------------------
# 2. A série de EXCESSO — a D48 e o CUSUM
# ---------------------------------------------------------------------------


def comparador(conn: sqlite3.Connection, run_id: int) -> Comparador:
    """Identifica um run pelo par `(run_id, run_digest)` e pelo contexto dele."""
    from ..calibracao import identidade
    from . import executor

    linha = conn.execute(
        "SELECT MIN(execution_bar_ms) AS de, MAX(execution_bar_ms) AS ate,"
        "       MIN(dataset_id) AS ds, COUNT(*) AS n"
        "  FROM execution WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if linha is None or linha["ds"] is None:
        raise SeriesIncompativeis(
            f"run {run_id} nao tem execucao nenhuma: nao ha janela a comparar"
        )
    ds = conn.execute(
        "SELECT timeframe FROM dataset WHERE id = ?", (int(linha["ds"]),)
    ).fetchone()
    return Comparador(
        run_id=run_id,
        run_digest=executor.digest_do_run(conn, run_id),
        identidade_executavel=identidade.do_run(conn, run_id),
        dataset_id=int(linha["ds"]),
        timeframe=str(ds["timeframe"]) if ds else "?",
        de_ms=int(linha["de"]),
        ate_ms=int(linha["ate"]),
        barras=int(linha["n"]),
    )


def _exigir_compativel(a: Comparador, b: Comparador) -> None:
    """A garantia 5, e ela levanta em vez de devolver número.

    **A janela NÃO precisa ser idêntica**, e é aqui que a garantia 2 vive: as
    operações não ocorrem juntas, então a primeira e a última execução de cada
    run caem em instantes diferentes. O que precisa ser igual é o **dataset**,
    o **timeframe** e a **identidade executável** — porque é isso que faz o
    preço de marcação ser o mesmo objeto nos dois lados.
    """
    if a.dataset_id != b.dataset_id:
        raise SeriesIncompativeis(
            f"datasets diferentes ({a.dataset_id} e {b.dataset_id}): o preco de"
            " marcacao nao seria o mesmo objeto"
        )
    if a.timeframe != b.timeframe:
        raise SeriesIncompativeis(
            f"timeframes diferentes ({a.timeframe} e {b.timeframe}): as grades"
            " nao se alinham barra a barra"
        )
    if a.identidade_executavel != b.identidade_executavel:
        raise SeriesIncompativeis(
            "identidades executaveis diferentes"
            f" ({a.identidade_executavel} e {b.identidade_executavel}): os dois"
            " runs executaram sob precos ou parametros diferentes, e a"
            " diferenca entre eles mediria a config, e nao a estrategia"
        )


def excesso_incremental(
    conn: sqlite3.Connection,
    *,
    candidata_run_id: int,
    b3_run_id: int,
    barras: Sequence[BarraCarregada],
) -> SerieDeExcesso:
    """`Δequity_candidata_t − Δequity_B3_t`, na mesma grade e ao mesmo preço.

    **Esta é a série que a D48 precisa** quando o efeito declarado é "sobre o
    B3" — que é o caso de `excesso_sobre_b3_cents`. A que ela usava era a
    volatilidade do mercado, e medido em cenário sintético isso superestimou a
    amostra exigida em cerca de 3,3×.

    A garantia que torna a série utilizável é a soma: ela reconstrói
    **exatamente** `patrimônio_final_candidata − patrimônio_final_B3`. Se não
    reconstruísse, a variância que sai dela dimensionaria outro efeito.
    """
    a = comparador(conn, candidata_run_id)
    b = comparador(conn, b3_run_id)
    _exigir_compativel(a, b)

    ea = equity_por_barra(conn, candidata_run_id, barras=barras)
    eb = equity_por_barra(conn, b3_run_id, barras=barras)
    va, vb = ea.valores_cents, eb.valores_cents
    if len(va) != len(vb):
        raise SeriesIncompativeis(
            f"as equities sairam com tamanhos diferentes ({len(va)} e"
            f" {len(vb)}): a grade nao e a mesma"
        )

    incremental = tuple(
        (va[i] - va[i - 1]) - (vb[i] - vb[i - 1]) for i in range(1, len(va))
    )
    return SerieDeExcesso(
        candidata=a,
        b3=b,
        incremental_cents=incremental,
        soma_cents=sum(incremental),
        # A diferenca final e entre as equities do PRIMEIRO ponto ao ULTIMO -
        # e nao entre os caixas -, porque a soma dos incrementos e telescopica
        # e reconstroi exatamente isso. Comparar contra os caixas so daria o
        # mesmo numero quando os dois terminam sem posicao, e a serie tem de
        # fechar sempre.
        diferenca_final_cents=(va[-1] - vb[-1]) - (va[0] - vb[0])
        if va and vb
        else 0,
        equity_candidata=ea,
        equity_b3=eb,
    )
