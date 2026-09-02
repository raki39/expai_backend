"""Plano de contas dos dois livros (secao 5.1).

Dois livros porque duas realidades economicas distintas convivem:

- **simulado, em USD** — o patrimonio ficticio do agente. E onde o desempenho
  e medido.
- **real, em BRL** — o dinheiro que de fato sai da conta para pagar inferencia.

Eles nunca se somam. Uma transacao que toque os dois se equilibra **dentro de
cada um**, e a ponte entre eles e a taxa de cambio gravada no proprio evento
(regra 7), nunca uma conversao feita na hora da leitura. Sem isso, variacao
cambial entraria no resultado do agente disfarcada de desempenho (secao 4.2).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import NamedTuple

log = logging.getLogger(__name__)


class Conta(NamedTuple):
    code: str
    book: str
    currency: str
    kind: str
    name: str


# ---------------------------------------------------------------------------
# Livro SIMULADO (USD)
# ---------------------------------------------------------------------------
CAIXA_SIM = "sim.carteira.caixa"
POSICAO_BTC = "sim.carteira.posicao_btc"
SEMENTE = "sim.patrimonio.semente"
TESOURARIA_SIM = "sim.tesouraria"
DESPESA_TAXA = "sim.despesa.taxa"
DESPESA_SPREAD = "sim.despesa.spread"
DESPESA_SLIPPAGE = "sim.despesa.slippage"
DESPESA_PENALIDADE = "sim.despesa.penalidade"
RESULTADO = "sim.resultado.realizado"

# ---------------------------------------------------------------------------
# Livro REAL (BRL)
# ---------------------------------------------------------------------------
CAIXA_REAL = "real.tesouraria.caixa"
APORTE_REAL = "real.patrimonio.aporte"
DESPESA_INFERENCIA = "real.despesa.inferencia"


PLANO: tuple[Conta, ...] = (
    # --- simulado -----------------------------------------------------------
    Conta(CAIXA_SIM, "simulado", "USD", "ativo", "Caixa da carteira (USDT)"),
    Conta(POSICAO_BTC, "simulado", "USD", "ativo", "Posicao em BTC"),
    Conta(SEMENTE, "simulado", "USD", "patrimonio", "Capital semente"),
    # Recebe o que a carteira paga pelo proprio pensamento. E a contrapartida
    # que torna o custo cognitivo visivel DENTRO do resultado do agente, e nao
    # uma despesa externa que ele nao enxerga (secao 3.6).
    Conta(TESOURARIA_SIM, "simulado", "USD", "tesouraria", "Tesouraria (simulado)"),
    # Quatro contas de custo, e nao uma so: o criterio 3 do incremento 3
    # recusa um campo "custo" agregado. Sem separar, e impossivel saber
    # depois qual componente comeu o resultado.
    Conta(DESPESA_TAXA, "simulado", "USD", "despesa", "Taxas de execucao"),
    Conta(DESPESA_SPREAD, "simulado", "USD", "despesa", "Spread"),
    Conta(DESPESA_SLIPPAGE, "simulado", "USD", "despesa", "Slippage"),
    Conta(
        DESPESA_PENALIDADE,
        "simulado",
        "USD",
        "despesa",
        "Penalidade pessimista do simulador",
    ),
    Conta(RESULTADO, "simulado", "USD", "resultado", "Resultado realizado"),
    # --- real ---------------------------------------------------------------
    Conta(CAIXA_REAL, "real", "BRL", "tesouraria", "Caixa da tesouraria"),
    Conta(APORTE_REAL, "real", "BRL", "patrimonio", "Aporte"),
    Conta(
        DESPESA_INFERENCIA, "real", "BRL", "despesa", "Inferencia de LLM"
    ),
)


def garantir_plano(conn: sqlite3.Connection) -> int:
    """Cria as contas que faltarem. Idempotente.

    Nao remove nem altera conta existente: `account` participa de lancamentos
    imutaveis, e mudar a moeda ou o livro de uma conta ja usada reescreveria o
    significado do historico sem tocar em nenhuma linha dele.
    """
    existentes = {
        linha["code"] for linha in conn.execute("SELECT code FROM account")
    }
    novas = [c for c in PLANO if c.code not in existentes]
    if novas:
        conn.executemany(
            "INSERT INTO account (code, book, currency, kind, name)"
            " VALUES (?,?,?,?,?)",
            novas,
        )
        log.info(
            "ledger.plano_de_contas",
            extra={"criadas": len(novas), "total": len(PLANO)},
        )
    return len(novas)


def id_por_codigo(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        linha["code"]: int(linha["id"])
        for linha in conn.execute("SELECT id, code FROM account")
    }
