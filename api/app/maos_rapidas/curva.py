"""Curva de patrimonio ao longo da janela (bloco 2 do painel, secao 6.2).

**Derivada das execucoes gravadas, nunca guardada.** Nao ha tabela de curva:
uma serie persistida ao lado do ledger seria uma segunda fonte de verdade
sobre dinheiro (regra 16), e divergiria dele no dia em que alguem esquecesse
de atualiza-la.

## Marcacao a mercado, e o que ela permite afirmar

Entre uma compra e a venda seguinte o agente esta comprado, e o patrimonio
dele nao e o caixa: e o caixa mais a posicao. Avaliar a posicao exige um
preco, e em fidelidade 1 o unico preco disponivel e o **fechamento da barra**.

Isso e legitimo e e declarado: a curva e "caixa + posicao avaliada ao
fechamento", nunca "o que teria sido recebido se vendesse". A diferenca
importa - vender pagaria taxa, spread, slippage e penalidade, e o preco
executado seria pior que o fechamento. **A curva e otimista no meio e exata
nas pontas**, onde a posicao esta zerada. Os numeros que a comparacao usa sao
sempre das pontas.

Para nao esconder isso, a avaliacao da posicao arredonda **para baixo**, como
toda receita neste projeto.

## Por que B1 nao tem curva

B1 sao mil repeticoes, e o incremento 4 decidiu guardar delas apenas o
resultado final - mil historias completas no ledger seriam milhoes de
lancamentos imutaveis para produzir tres numeros. Entao B1 entra na tela como
**faixa do resultado final** (p5 a p95), e nao como caminho.

Desenhar uma faixa de B1 atravessando o tempo afirmaria que mil caminhos
foram simulados e acompanhados, o que nao aconteceu. E o tipo de grafico que
mente sem que nenhum numero esteja errado.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Sequence

from ..config.schema import ExperimentConfig
from ..dataset.loader import BarraCarregada
from ..ledger import contas
from ..simulador.execucao import DIVISOR_NOCIONAL
from . import executor

# Pontos plotados. 56.064 barras nao cabem num grafico nem em JSON util, e
# amostrar e melhor que agregar: agregar inventaria valores que nao existem na
# serie, e o ponto amostrado e um valor que de fato ocorreu.
PONTOS_PADRAO = 400


@dataclass(frozen=True)
class Ponto:
    open_time_ms: int
    patrimonio_cents: int
    posicao_sats: int

    def como_dict(self) -> dict:
        return {
            "t": self.open_time_ms,
            "patrimonio_cents": self.patrimonio_cents,
            # Quem le a curva precisa saber onde ela e exata: com posicao
            # zerada o numero e o caixa; com posicao aberta e avaliacao.
            "comprado": self.posicao_sats > 0,
        }


def _valor_da_posicao_cents(quantity_sats: int, preco: int) -> int:
    """Posicao avaliada ao preco dado. Para BAIXO, como toda receita."""
    return quantity_sats * preco // DIVISOR_NOCIONAL


def curva_do_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    barras: Sequence[BarraCarregada],
    pontos: int = PONTOS_PADRAO,
) -> list[Ponto]:
    """Patrimonio barra a barra, amostrado.

    Reconstroi caixa e posicao caminhando pelas execucoes gravadas, na ordem
    em que aconteceram. Nao recalcula preco nem custo: usa o que o ledger e a
    tabela de execucoes registraram, que e o que de fato aconteceu.
    """
    if not barras:
        return []

    abertura = conn.execute(
        "SELECT COALESCE(SUM(e.amount_minor), 0) AS caixa"
        " FROM ledger_entry e"
        " JOIN ledger_transaction t ON t.id = e.transaction_id"
        " JOIN account a ON a.id = e.account_id"
        " WHERE t.run_id = ? AND t.kind = 'abertura' AND a.code = ?",
        (run_id, contas.CAIXA_SIM),
    ).fetchone()
    caixa = int(abertura["caixa"])

    # As execucoes, na ordem da barra em que aconteceram. O efeito de cada uma
    # no caixa vem do LEDGER, e nao de um recalculo: recalcular aqui criaria
    # uma segunda aritmetica do dinheiro, que e como duas fontes de verdade
    # comecam.
    execucoes = list(
        conn.execute(
            "SELECT e.execution_bar_ms AS t, e.side, e.quantity_sats,"
            "       (SELECT COALESCE(SUM(le.amount_minor), 0)"
            "          FROM ledger_entry le"
            "          JOIN account a ON a.id = le.account_id"
            "         WHERE le.transaction_id = e.ledger_transaction_id"
            "           AND a.code = ?) AS delta_caixa"
            " FROM execution e"
            " WHERE e.run_id = ?"
            " ORDER BY e.execution_bar_ms, e.id",
            (contas.CAIXA_SIM, run_id),
        )
    )

    posicao = 0
    proxima = 0
    passo = max(1, len(barras) // max(1, pontos))
    saida: list[Ponto] = []

    for indice, barra in enumerate(barras):
        while proxima < len(execucoes) and execucoes[proxima]["t"] <= barra.open_time_ms:
            ex = execucoes[proxima]
            caixa += int(ex["delta_caixa"])
            posicao += (
                int(ex["quantity_sats"])
                if ex["side"] == "compra"
                else -int(ex["quantity_sats"])
            )
            proxima += 1

        if indice % passo == 0 or indice == len(barras) - 1:
            saida.append(
                Ponto(
                    open_time_ms=barra.open_time_ms,
                    patrimonio_cents=caixa
                    + _valor_da_posicao_cents(posicao, barra.close),
                    posicao_sats=posicao,
                )
            )

    return saida


def curvas_da_comparacao(
    conn: sqlite3.Connection,
    *,
    dataset_id: int,
    config: ExperimentConfig,
    runs: dict[str, int],
    pontos: int = PONTOS_PADRAO,
) -> dict:
    """Uma curva por run nomeado, todas sobre a MESMA janela e escala.

    "Mesma escala" nao e detalhe de desenho: comparar curvas em escalas
    diferentes e a forma mais barata de fazer duas coisas parecerem o que nao
    sao (secao 6.2).
    """
    barras = executor.carregar_janela(conn, dataset_id)
    return {
        nome: [p.como_dict() for p in curva_do_run(
            conn, run_id, barras=barras, pontos=pontos
        )]
        for nome, run_id in runs.items()
    }


def excesso_sobre_baseline_cents(
    patrimonio_cents: int, baseline_cents: int
) -> int:
    """Desempenho SEMPRE como excesso sobre baseline, nunca absoluto (regra 14).

    Um numero absoluto responde "quanto sobrou", que nao e a pergunta do
    experimento. A pergunta e "sobrou mais do que teria sobrado sem o agente".
    """
    return patrimonio_cents - baseline_cents
