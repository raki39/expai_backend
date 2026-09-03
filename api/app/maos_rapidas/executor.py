"""Mãos rapidas: o laco deterministico por barra.

**Zero chamadas de modelo aqui dentro** (secao 3.2, regra 3). Este modulo
recebe uma `Regra` ja pronta e a executa. De onde ela veio - do catalogo
congelado do B3, ou de uma reflexao do cerebro lento - e informacao que o
executor nao tem e nao precisa ter. E por isso que o B3, que nunca passou por
LLM nenhum, roda por este mesmo caminho sem nenhuma adaptacao.

As maos rapidas **nao sao nos do grafo**. Nao ha LangGraph aqui, nem import
capaz de puxa-lo.

Deterministico no sentido forte (R12): mesma regra, mesmo dataset e mesma
config produzem a mesma sequencia de lancamentos, e portanto o mesmo digest.
Nao ha `random`, nao ha relogio, nao ha iteracao sobre estrutura sem ordem.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from ..config.schema import ExperimentConfig
from ..dataset import loader
from ..dataset.loader import BarraCarregada
from ..regra.schema import Regra
from ..regra.sinais import Sinal, avaliar, stop_disparado
from ..simulador import execucao as simulador

log = logging.getLogger(__name__)

# Instante grande o bastante para "tudo o que esta disponivel". A guarda de
# periodo reservado nao depende disto - ela vive na view (criterio 4 do
# incremento 1) e nao ha valor de decision_ts que a contorne.
TUDO_DISPONIVEL = 10**15


@dataclass(frozen=True)
class ResultadoRegra:
    run_id: int
    rule_id: int
    regra_hash: str
    barras_avaliadas: int
    entradas: int
    saidas: int
    execucoes: int
    digest: str
    fechou_no_fim: bool
    entradas_recusadas_por_caixa: int

    @property
    def operacoes(self) -> int:
        """Idas e voltas completas. E a contagem que B1 precisa casar."""
        return min(self.entradas, self.saidas)

    def como_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "rule_id": self.rule_id,
            "regra_hash": self.regra_hash,
            "barras_avaliadas": self.barras_avaliadas,
            "operacoes": self.operacoes,
            "entradas": self.entradas,
            "saidas": self.saidas,
            "execucoes": self.execucoes,
            "digest": self.digest,
            "fechou_no_fim": self.fechou_no_fim,
            "entradas_recusadas_por_caixa": self.entradas_recusadas_por_caixa,
        }


def idas_e_voltas(conn: sqlite3.Connection, run_id: int) -> int:
    """Quantas idas e voltas o run fez. A UNIDADE da comparacao.

    Uma definicao so, num lugar so. Havia duas: `COUNT(*) / 2` sobre as
    execucoes numa rota, e a contagem de compras no relatorio. As duas dao o
    mesmo numero enquanto toda compra fecha - e divergem em silencio no
    unico caso em que importa, o run que termina comprado.

    Compras, e nao metade das execucoes: a D1 fixou long/flat, entao ha no
    maximo uma posicao aberta e cada compra abre exatamente uma ida e volta.
    Dividir por dois SUPOE que a ultima fechou.
    """
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM execution"
            " WHERE run_id = ? AND side = 'compra'",
            (run_id,),
        ).fetchone()["n"]
    )


def digest_do_run(conn: sqlite3.Connection, run_id: int) -> str:
    """Hash da sequencia ordenada de lancamentos do run (criterio 2).

    Nao entra id nenhum na conta - nem de transacao, nem de lancamento. Ids
    sao chaves de superficie: dois runs economicamente identicos os teriam
    diferentes, e o digest passaria a dizer "sao diferentes" quando eles nao
    sao. O que identifica o run e a SEQUENCIA de tipo, conta e valor.

    **So o livro simulado entra.** O digest responde "o experimento se
    reproduziu?", e o livro real responde outra pergunta: "quanto dinheiro
    saiu da conta desta vez?". Com o cache de respostas quente, a segunda
    execucao do mesmo run nao gasta nada de verdade e o livro real fica
    diferente - corretamente. Misturar os dois faria o digest acusar
    divergencia onde o experimento se reproduziu perfeitamente, e a regra 7 ja
    diz que os dois livros nunca se somam.

    Isto NAO altera nenhum digest ja publicado: todo run existente ate aqui e
    de baseline, e baseline nao chama modelo nenhum - todos os lancamentos
    deles ja sao do livro simulado. Comparado, nao suposto.
    """
    h = hashlib.sha256()
    for linha in conn.execute(
        "SELECT t.kind AS kind, a.code AS code, e.amount_minor AS valor"
        " FROM ledger_entry e"
        " JOIN ledger_transaction t ON t.id = e.transaction_id"
        " JOIN account a ON a.id = e.account_id"
        " WHERE t.run_id = ? AND t.posted_at IS NOT NULL"
        "   AND a.book = 'simulado'"
        " ORDER BY t.id, e.id",
        (run_id,),
    ):
        h.update(f"{linha['kind']}|{linha['code']}|{linha['valor']}\n".encode())
    return h.hexdigest()


def carregar_janela(
    conn: sqlite3.Connection, dataset_id: int
) -> list[BarraCarregada]:
    """Toda a janela disponivel - o periodo reservado segue fora, pela view."""
    return loader.carregar(conn, dataset_id, decision_ts_ms=TUDO_DISPONIVEL)


def rodar(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    dataset_id: int,
    regra: Regra,
    rule_id: int,
    config: ExperimentConfig,
    barras: Sequence[BarraCarregada] | None = None,
) -> ResultadoRegra:
    """Executa a regra sobre a janela inteira, barra a barra."""
    barras = list(barras) if barras is not None else carregar_janela(conn, dataset_id)
    if not barras:
        raise ValueError("janela vazia")

    sinais = avaliar(barras, regra)
    fracao = Decimal(regra.position_fraction_bps) / Decimal(10_000)

    # A decisao na barra i executa em i+latencia. Decidir depois disto seria
    # pedir execucao numa barra que nao existe - ou, pior, numa reservada.
    ultima_decidivel = len(barras) - 1 - config.latency_bars
    if ultima_decidivel < regra.janela_minima:
        raise ValueError(
            f"janela de {len(barras)} barras nao comporta a regra "
            f"(minimo {regra.janela_minima} + latencia {config.latency_bars})"
        )

    aberta = False
    preco_de_entrada = 0
    entradas = saidas = recusadas = 0

    for i in range(ultima_decidivel + 1):
        barra = barras[i]

        # O limite de perda e conferido ANTES do sinal: se a posicao ja
        # rompeu o limite, a regra nao tem mais o que opinar sobre ela.
        if (
            aberta
            and regra.stop_loss_bps is not None
            and stop_disparado(barra, preco_de_entrada, regra.stop_loss_bps)
        ):
            simulador.vender(
                conn, run_id=run_id, dataset_id=dataset_id,
                decision_bar_ms=barra.open_time_ms, config=config, rule_id=rule_id,
            )
            aberta = False
            saidas += 1
            continue

        sinal = sinais[i]
        if sinal == Sinal.ENTRAR and not aberta:
            try:
                execucao = simulador.comprar(
                    conn, run_id=run_id, dataset_id=dataset_id,
                    decision_bar_ms=barra.open_time_ms, config=config,
                    fracao_do_caixa=fracao, rule_id=rule_id,
                )
            except simulador.CaixaInsuficiente:
                # Nao e erro: o caixa acabou. Contado e seguido em frente,
                # porque abortar o run inteiro esconderia o resultado real.
                recusadas += 1
                continue
            aberta = True
            preco_de_entrada = execucao.price_exec
            entradas += 1
        elif sinal == Sinal.SAIR and aberta:
            simulador.vender(
                conn, run_id=run_id, dataset_id=dataset_id,
                decision_bar_ms=barra.open_time_ms, config=config, rule_id=rule_id,
            )
            aberta = False
            saidas += 1

    # Fecha o que ficou aberto. Uma posicao em aberto no fim tornaria o
    # resultado incomparavel: o caixa nao a inclui e o patrimonio nao tem
    # preco declarado. Fechar paga os custos de saida, que e o honesto.
    fechou_no_fim = False
    if aberta:
        simulador.vender(
            conn, run_id=run_id, dataset_id=dataset_id,
            decision_bar_ms=barras[ultima_decidivel].open_time_ms,
            config=config, rule_id=rule_id,
        )
        saidas += 1
        fechou_no_fim = True

    execucoes = conn.execute(
        "SELECT COUNT(*) AS n FROM execution WHERE run_id = ?", (run_id,)
    ).fetchone()["n"]

    resultado = ResultadoRegra(
        run_id=run_id,
        rule_id=rule_id,
        regra_hash=regra.hash(),
        barras_avaliadas=len(barras),
        entradas=entradas,
        saidas=saidas,
        execucoes=int(execucoes),
        digest=digest_do_run(conn, run_id),
        fechou_no_fim=fechou_no_fim,
        entradas_recusadas_por_caixa=recusadas,
    )
    log.info("maos_rapidas.run", extra=resultado.como_dict())
    return resultado
