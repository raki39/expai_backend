"""Simulador de execucao. **Sempre pessimista, nunca generoso.**

Fidelidade declarada 1: barras OHLCV, e nada alem disso. O que este modulo
pode afirmar e limitado por esse nivel, e o nivel viaja junto de cada
execucao e de cada resultado (secao 8.4.1.1).

Como o pessimismo e construido, em ordem:

1. **Referencia = o limite ADVERSO da barra.** Compra ao topo da barra, venda
   ao fundo. Nao a abertura, nao o fechamento, nao a media: o pior preco que
   de fato existiu naquele instante (secao 8.4.1.2).

2. **Spread, slippage e penalidade pioram a referencia**, cada um na direcao
   contraria a operacao. O executado pode passar do topo da barra numa
   compra, e isso e realista, nao ficcao: o preco impresso e o ultimo
   negociado, e quem entra a mercado paga a oferta, que esta acima.

3. **Taxa sempre taker.** Nunca maker - a Fase 0A nao tem como afirmar nada
   sobre fila ou preenchimento passivo, entao supor que teria sido preenchida
   como maker seria inventar fidelidade que o dado nao tem.

4. **Arredondamento assimetrico.** Custo arredonda para cima, receita para
   baixo. O centavo perdido vai sempre contra o experimento.

5. **Latencia estrutural.** A execucao acontece numa barra posterior a da
   decisao, e o CHECK do banco recusa o contrario.

**O que este modulo nao calcula e nao vai calcular:** spread real de book,
posicao em fila, probabilidade de preenchimento maker. Secao 8.4.1 proibe
afirmar fidelidade de book, e o caminho para violar isso e sempre comecar a
estimar essas coisas "so para ter uma ideia".

Fronteira de importacao (regra 3): aqui nao entra LangGraph, provedor de LLM
nem o cerebro lento. Isto sao maos rapidas.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal

from ..config.schema import ExperimentConfig
from ..dataset import loader
from ..ledger import contas
from ..ledger import livro
from ..ledger.livro import Lancamento, registrar
from ..store import bloco_atomico

log = logging.getLogger(__name__)

BPS = Decimal(10_000)

# Preco e quantidade vem com 8 casas; dinheiro e centavo. Converter
# quantidade x preco para centavos divide por 1e8 (preco) x 1e8 (qty) e
# multiplica por 100 (centavos) = 1e14.
DIVISOR_NOCIONAL = 10**14

Lado = Literal["compra", "venda"]


class SemBarraParaExecutar(Exception):
    """A latencia cai fora da janela disponivel."""


class PosicaoInvalida(Exception):
    """D1 fixou long/flat: nao ha venda a descoberto nem posicao dupla."""


class CaixaInsuficiente(Exception):
    pass


@dataclass(frozen=True)
class Execucao:
    id: int
    side: Lado
    decision_bar_ms: int
    execution_bar_ms: int
    quantity_sats: int
    price_ref: int
    price_exec: int
    notional_ref_cents: int
    fee_cents: int
    spread_cents: int
    slippage_cents: int
    penalty_cents: int
    fidelity_level: int
    ledger_transaction_id: int
    rule_id: int | None = None

    @property
    def custo_total_cents(self) -> int:
        return (
            self.fee_cents
            + self.spread_cents
            + self.slippage_cents
            + self.penalty_cents
        )

    def como_dict(self) -> dict:
        return {
            "id": self.id,
            "side": self.side,
            "decision_bar_ms": self.decision_bar_ms,
            "execution_bar_ms": self.execution_bar_ms,
            "quantity_sats": self.quantity_sats,
            "price_ref": self.price_ref,
            "price_exec": self.price_exec,
            "notional_ref_cents": self.notional_ref_cents,
            "custos": {
                "taxa": self.fee_cents,
                "spread": self.spread_cents,
                "slippage": self.slippage_cents,
                "penalidade": self.penalty_cents,
                "total": self.custo_total_cents,
            },
            # Viaja junto do numero, sempre (criterio 5).
            "fidelity_level": self.fidelity_level,
            "ledger_transaction_id": self.ledger_transaction_id,
        }


# ---------------------------------------------------------------- aritmetica


def _teto(a: int, b: int) -> int:
    """Divisao inteira com teto. Usada onde arredondar favorece o custo."""
    return -(-a // b)


def notional_cents(quantity_sats: int, price: int, *, para_cima: bool) -> int:
    """Quantidade x preco, em centavos de USD, sem ponto flutuante.

    `para_cima` no que o experimento PAGA, para baixo no que ele RECEBE.
    O centavo de arredondamento vai sempre contra o experimento - se fosse a
    favor, milhares de operacoes acumulariam uma vantagem que nao existe.
    """
    bruto = quantity_sats * price
    return _teto(bruto, DIVISOR_NOCIONAL) if para_cima else bruto // DIVISOR_NOCIONAL


def _bps_sobre(valor_cents: int, bps: Decimal) -> int:
    """Custo em bps sobre um nocional. Sempre para cima."""
    return _teto(valor_cents * int(bps * 100), 100 * 10_000)


def preco_adverso(barra: loader.BarraCarregada, lado: Lado) -> int:
    """O pior preco que existiu na barra, para o lado da operacao.

    Compra ao topo, venda ao fundo. Nao a abertura nem o fechamento: escolher
    um preco melhor que o adverso e supor que a ordem pegou o melhor momento
    da barra - que e exatamente a afirmacao que a fidelidade 1 nao sustenta.
    """
    return barra.high if lado == "compra" else barra.low


def preco_executado(price_ref: int, lado: Lado, config: ExperimentConfig) -> int:
    """A referencia piorada por spread, slippage e penalidade.

    O spread entra pela metade porque o valor configurado e o spread CHEIO, e
    quem atravessa paga meia distancia ate o meio do book em cada ponta.
    """
    ajuste = (
        config.spread_bps / 2 + config.slippage_bps + config.penalty_bps
    ) / BPS
    if lado == "compra":
        # Para CIMA: pagar mais.
        alvo = Decimal(price_ref) * (1 + ajuste)
        return int(alvo.to_integral_value(rounding=ROUND_CEILING))
    # Para BAIXO: receber menos.
    alvo = Decimal(price_ref) * (1 - ajuste)
    return int(alvo.to_integral_value(rounding=ROUND_FLOOR))


# ---------------------------------------------------------------------------
# NUCLEO PURO DE PRECIFICACAO
#
# Sem banco, sem estado. Existe para que o caminho persistido (uma execucao
# gravada no ledger) e o caminho em memoria (as mil repeticoes do B1) usem
# exatamente o MESMO calculo.
#
# Se cada um tivesse a sua copia, "os baselines usam o mesmo simulador"
# (criterio 6 do incremento 4) dependeria de as duas copias continuarem
# iguais - e elas nao continuariam. Assim a paridade e por construcao.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Custos:
    """Os quatro custos, sempre separados (criterio 3 do incremento 3)."""

    fee: int
    spread: int
    slippage: int
    penalty: int

    @property
    def de_preco(self) -> int:
        """Os que estao embutidos no preco executado, e nao no caixa a parte."""
        return self.spread + self.slippage + self.penalty

    @property
    def total(self) -> int:
        return self.fee + self.de_preco


def dimensionar(orcamento_cents: int, price_exec: int, config: ExperimentConfig) -> int:
    """Quantos satoshis cabem no orcamento, ja contando a taxa.

    Para BAIXO: comprar de menos e conservador, comprar de mais estouraria o
    caixa - e caixa negativo significaria comprar fiado.
    """
    fator_taxa = 1 + config.taker_fee_bps / BPS
    teto_nocional = int(Decimal(orcamento_cents) / fator_taxa)
    return teto_nocional * DIVISOR_NOCIONAL // price_exec


def custear(
    quantity_sats: int,
    price_ref: int,
    price_exec: int,
    lado: Lado,
    config: ExperimentConfig,
) -> tuple[int, Custos]:
    """Nocional de referencia e a decomposicao dos custos."""
    comprando = lado == "compra"
    nocional_ref = notional_cents(quantity_sats, price_ref, para_cima=comprando)
    nocional_exec = notional_cents(quantity_sats, price_exec, para_cima=comprando)
    return nocional_ref, Custos(
        # A taxa incide sobre o que foi de fato negociado.
        fee=_bps_sobre(nocional_exec, config.taker_fee_bps),
        spread=_bps_sobre(nocional_ref, config.spread_bps / 2),
        slippage=_bps_sobre(nocional_ref, config.slippage_bps),
        penalty=_bps_sobre(nocional_ref, config.penalty_bps),
    )


# ------------------------------------------------------------------ estado


def posicao_sats(conn: sqlite3.Connection, run_id: int) -> int:
    linha = conn.execute(
        "SELECT quantity_sats FROM position_atual WHERE run_id = ?", (run_id,)
    ).fetchone()
    return int(linha["quantity_sats"]) if linha else 0


def custo_da_posicao_cents(conn: sqlite3.Connection, run_id: int) -> int:
    """Base de custo = o saldo da conta de posicao DESTE run.

    Por run, e nao global: contas globais fariam um run herdar a posicao do
    anterior. Nao ha segunda fonte - a base de custo e o saldo, sempre.
    """
    return livro.saldo_da_conta(conn, contas.POSICAO_BTC, run_id=run_id)


def caixa_cents(conn: sqlite3.Connection, run_id: int) -> int:
    return livro.saldo_da_conta(conn, contas.CAIXA_SIM, run_id=run_id)


# --------------------------------------------------------------- execucao


def _barra_de_execucao(
    conn: sqlite3.Connection,
    dataset_id: int,
    decision_bar_ms: int,
    config: ExperimentConfig,
) -> loader.BarraCarregada:
    barra = loader.proxima_barra(
        conn, dataset_id, decision_bar_ms, saltos=config.latency_bars
    )
    if barra is None:
        raise SemBarraParaExecutar(
            f"nao ha {config.latency_bars} barra(s) apos {decision_bar_ms} "
            "dentro da janela disponivel"
        )
    return barra


def comprar(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    dataset_id: int,
    decision_bar_ms: int,
    config: ExperimentConfig,
    fracao_do_caixa: Decimal = Decimal("1.0"),
    rule_id: int | None = None,
) -> Execucao:
    """Entra comprado. D1 fixou long/flat: so entra quem esta zerado."""
    if posicao_sats(conn, run_id) != 0:
        raise PosicaoInvalida("ja ha posicao aberta; long/flat nao acumula")
    if not (Decimal(0) < fracao_do_caixa <= Decimal(1)):
        raise ValueError("fracao do caixa precisa estar em (0, 1]")

    barra = _barra_de_execucao(conn, dataset_id, decision_bar_ms, config)
    price_ref = preco_adverso(barra, "compra")
    price_exec = preco_executado(price_ref, "compra", config)

    disponivel = caixa_cents(conn, run_id)
    orcamento = int(Decimal(disponivel) * fracao_do_caixa)
    if orcamento <= 0:
        raise CaixaInsuficiente("caixa insuficiente para comprar")

    quantity_sats = dimensionar(orcamento, price_exec, config)
    if quantity_sats <= 0:
        raise CaixaInsuficiente(
            f"caixa de {disponivel} centavos nao compra nem 1 satoshi ao "
            f"preco executado {price_exec}"
        )

    return _registrar(
        conn,
        run_id=run_id,
        dataset_id=dataset_id,
        lado="compra",
        decision_bar_ms=decision_bar_ms,
        barra=barra,
        quantity_sats=quantity_sats,
        price_ref=price_ref,
        price_exec=price_exec,
        config=config,
        rule_id=rule_id,
    )


def vender(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    dataset_id: int,
    decision_bar_ms: int,
    config: ExperimentConfig,
    rule_id: int | None = None,
) -> Execucao:
    """Zera a posicao. Long/flat nao tem saida parcial nem venda a descoberto."""
    quantity_sats = posicao_sats(conn, run_id)
    if quantity_sats <= 0:
        raise PosicaoInvalida("nao ha posicao para vender; long/flat nao vende a descoberto")

    barra = _barra_de_execucao(conn, dataset_id, decision_bar_ms, config)
    price_ref = preco_adverso(barra, "venda")
    price_exec = preco_executado(price_ref, "venda", config)

    return _registrar(
        conn,
        run_id=run_id,
        dataset_id=dataset_id,
        lado="venda",
        decision_bar_ms=decision_bar_ms,
        barra=barra,
        quantity_sats=quantity_sats,
        price_ref=price_ref,
        price_exec=price_exec,
        config=config,
        rule_id=rule_id,
    )


def _registrar(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    dataset_id: int,
    lado: Lado,
    decision_bar_ms: int,
    barra: loader.BarraCarregada,
    quantity_sats: int,
    price_ref: int,
    price_exec: int,
    config: ExperimentConfig,
    rule_id: int | None = None,
) -> Execucao:
    """Decompoe os custos e lanca no ledger. Cada custo, uma linha (criterio 3).

    A decomposicao e possivel porque existe um preco de REFERENCIA: spread,
    slippage e penalidade sao a distancia entre ele e o executado, atribuida a
    cada componente na proporcao dos bps configurados. Sem referencia, os tres
    seriam indistinguiveis dentro do preco e o criterio 3 nao teria como ser
    satisfeito - um campo "custo" agregado e o que sobraria.
    """
    comprando = lado == "compra"
    # MESMO nucleo que o B1 usa em memoria. Nao ha segunda formula.
    nocional_ref, custos = custear(
        quantity_sats, price_ref, price_exec, lado, config
    )
    fee_cents = custos.fee
    spread_cents = custos.spread
    slippage_cents = custos.slippage
    penalty_cents = custos.penalty
    custos_de_preco = custos.de_preco

    despesas = [
        Lancamento(contas.DESPESA_TAXA, fee_cents, "taxa taker"),
        Lancamento(contas.DESPESA_SLIPPAGE, slippage_cents, "slippage"),
        Lancamento(contas.DESPESA_PENALIDADE, penalty_cents, "penalidade"),
        Lancamento(contas.DESPESA_SPREAD, spread_cents, "spread"),
    ]
    despesas = [l for l in despesas if l.valor_minor != 0]

    if comprando:
        saida_caixa = nocional_ref + custos_de_preco + fee_cents
        lancamentos = [
            Lancamento(contas.CAIXA_SIM, -saida_caixa, "compra"),
            # A posicao entra pela REFERENCIA. O que se pagou a mais que ela
            # sao os custos, e eles ja estao nas contas de despesa - embuti-los
            # na posicao esconderia o custo dentro do ativo.
            Lancamento(contas.POSICAO_BTC, nocional_ref, "posicao"),
            *despesas,
        ]
    else:
        entrada_caixa = nocional_ref - custos_de_preco - fee_cents
        base_de_custo = custo_da_posicao_cents(conn, run_id)
        # Positivo = prejuizo, coerente com despesa. Negativo = ganho.
        resultado = base_de_custo - nocional_ref
        lancamentos = [
            Lancamento(contas.CAIXA_SIM, entrada_caixa, "venda"),
            Lancamento(contas.POSICAO_BTC, -base_de_custo, "posicao zerada"),
            *despesas,
        ]
        if resultado != 0:
            lancamentos.append(
                Lancamento(contas.RESULTADO, resultado, "resultado realizado")
            )

    with bloco_atomico(conn, "execucao"):
        tx_id = registrar(
            conn,
            kind="operacao",
            run_id=run_id,
            lancamentos=lancamentos,
            occurred_at=str(barra.open_time_ms),
            memo=f"{lado} simulada, fidelidade {config.fidelity_level}",
        )
        cur = conn.execute(
            "INSERT INTO execution (run_id, dataset_id, decision_bar_ms,"
            " execution_bar_ms, side, quantity_sats, price_ref, price_exec,"
            " notional_ref_cents, fee_cents, spread_cents, slippage_cents,"
            " penalty_cents, fidelity_level, ledger_transaction_id, rule_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, dataset_id, decision_bar_ms, barra.open_time_ms, lado,
                quantity_sats, price_ref, price_exec, nocional_ref, fee_cents,
                spread_cents, slippage_cents, penalty_cents,
                config.fidelity_level, tx_id, rule_id,
            ),
        )
        execution_id = int(cur.lastrowid)

    log.info(
        "simulador.execucao",
        extra={
            "execution_id": execution_id,
            "side": lado,
            "quantity_sats": quantity_sats,
            "price_ref": price_ref,
            "price_exec": price_exec,
            "custo_total_cents": fee_cents + custos_de_preco,
            "fidelity_level": config.fidelity_level,
        },
    )
    return Execucao(
        id=execution_id,
        side=lado,
        decision_bar_ms=decision_bar_ms,
        execution_bar_ms=barra.open_time_ms,
        quantity_sats=quantity_sats,
        price_ref=price_ref,
        price_exec=price_exec,
        notional_ref_cents=nocional_ref,
        fee_cents=fee_cents,
        spread_cents=spread_cents,
        slippage_cents=slippage_cents,
        penalty_cents=penalty_cents,
        fidelity_level=config.fidelity_level,
        ledger_transaction_id=tx_id,
        rule_id=rule_id,
    )


# ------------------------------------------------------------------ resumo

# Texto obrigatorio em todo resultado agregado. Existe para que ninguem olhe
# um numero desta simulacao e conclua algo que a fidelidade 1 nao sustenta
# (secao 8.4.1, secao 14).
CONDICOES_DE_VALIDADE = (
    "Fidelidade 1 (barras OHLCV). Execucao sempre taker, ao limite adverso da "
    "barra, com spread, slippage e penalidade por cima. NAO ha afirmacao "
    "possivel sobre spread real de book, posicao em fila ou preenchimento "
    "maker. Nenhuma conclusao estatistica."
)


def resumo(conn: sqlite3.Connection, run_id: int) -> dict:
    """Agregado das execucoes, com fidelidade e condicoes de validade juntas."""
    linha = conn.execute(
        """
        SELECT COUNT(*) AS execucoes,
               COALESCE(SUM(fee_cents), 0)      AS taxa,
               COALESCE(SUM(spread_cents), 0)   AS spread,
               COALESCE(SUM(slippage_cents), 0) AS slippage,
               COALESCE(SUM(penalty_cents), 0)  AS penalidade,
               MIN(fidelity_level) AS fid_min,
               MAX(fidelity_level) AS fid_max
        FROM execution WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()

    # Sem execucao nenhuma, MIN/MAX devolvem NULL. Um run recem-aberto tem
    # de conseguir mostrar o resumo - e nao declarar fidelidade que ainda nao
    # se aplica a coisa alguma.
    vazio = linha["fid_min"] is None

    custos = {
        "taxa": int(linha["taxa"]),
        "spread": int(linha["spread"]),
        "slippage": int(linha["slippage"]),
        "penalidade": int(linha["penalidade"]),
    }
    return {
        "run_id": run_id,
        "execucoes": int(linha["execucoes"]),
        "posicao_sats": posicao_sats(conn, run_id),
        "custos_cents": {**custos, "total": sum(custos.values())},
        # Se algum dia houver execucao com fidelidade diferente no mesmo run,
        # o agregado tem de dizer isso em vez de escolher uma delas.
        "fidelity_level": (
            None if vazio or linha["fid_min"] != linha["fid_max"]
            else int(linha["fid_max"])
        ),
        # Vazio conta como homogeneo: nao ha divergencia entre zero coisas.
        "fidelidade_homogenea": vazio or linha["fid_min"] == linha["fid_max"],
        "condicoes_validade": CONDICOES_DE_VALIDADE,
    }
