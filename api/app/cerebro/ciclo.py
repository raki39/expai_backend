"""O ciclo da 0A, fechado de ponta a ponta.

    abre run -> cerebro lento propoe regra -> maos rapidas executam ->
    ledger registra tudo -> resultado sai com as condicoes declaradas

E o unico lugar do projeto onde o cerebro e as maos aparecem na mesma funcao,
e mesmo aqui eles nao se tocam: o grafo termina, a regra fica gravada, e so
entao o executor a le. O executor continua sem saber de onde ela veio - e por
isso que o B3, que nunca passou por modelo nenhum, roda pelo mesmo caminho.

**Se o cerebro parar** - teto atingido, resposta invalida, provedor fora do ar
- as maos rapidas rodam a regra padrao e o run termina normalmente (secao 3.6
regra 2). O resultado diz, sempre, quantas reflexoes houve e qual regra
executou: um run sem reflexao nenhuma nao esta medindo cerebro, e confundir os
dois seria atribuir ao agente o resultado do baseline.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..config.schema import ExperimentConfig
from ..ledger import livro
from ..maos_rapidas import baselines, executor
from ..regra import registro as registro_de_regra
from ..regra.schema import Regra
from ..settings import Settings
from ..simulador import execucao as simulador
from ..simulador.execucao import condicoes_do_run
from . import avaliacao, grafo, propostas

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResultadoDoCiclo:
    run_id: int
    reflexoes: int
    parou_em: str | None
    motivo: str | None
    regra_veio_do_cerebro: bool
    rule_id: int
    regra_hash: str
    proposal_id: int | None
    expectativa: str | None
    confianca_ppm: int | None
    execucao: dict
    # O resultado economico do run. Sem ele o relatorio do agente diz quantas
    # operacoes houve e nao diz se sobrou dinheiro - que e a unica pergunta
    # que a comparacao com os baselines responde.
    patrimonio_final_cents: int
    custos: dict
    # A distribuicao do acaso com o MESMO giro deste run. Produzida aqui, e
    # nao num passo a parte, porque um controle que pode ficar dessincronizado
    # do que ele controla nao e controle nenhum - e foi exatamente assim que
    # o primeiro run do agente saiu, ao lado de um B1 casado com o B3.
    b1_casado: dict | None
    gasto: dict
    sobreposicao: dict
    condicoes_validade: str
    # O evento filho da decisao que compara o declarado com o realizado
    # (R25.3). `None` quando o cerebro nao declarou nada - nao ha o que
    # avaliar, e pendurar a comparacao em quem nao afirmou seria invencao.
    avaliacao_event_id: int | None

    def como_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "reflexoes": self.reflexoes,
            "parou_em": self.parou_em,
            "motivo": self.motivo,
            "regra_veio_do_cerebro": self.regra_veio_do_cerebro,
            "rule_id": self.rule_id,
            "regra_hash": self.regra_hash,
            "proposal_id": self.proposal_id,
            "expectativa": self.expectativa,
            "confianca_ppm": self.confianca_ppm,
            "execucao": self.execucao,
            "patrimonio_final_cents": self.patrimonio_final_cents,
            "custos_cents": self.custos,
            "b1_casado_com_este_run": self.b1_casado,
            "excesso_sobre_b1_p50_cents": (
                self.patrimonio_final_cents - self.b1_casado["p50"]
                if self.b1_casado else None
            ),
            "gasto": self.gasto,
            "sobreposicao_amostral": self.sobreposicao,
            "condicoes_validade": self.condicoes_validade,
            "avaliacao_event_id": self.avaliacao_event_id,
        }


def rodar(
    conn: sqlite3.Connection,
    *,
    dataset_id: int,
    config: ExperimentConfig,
    config_version_id: int,
    settings: Settings,
    adaptador: Any | None = None,
) -> ResultadoDoCiclo:
    """Abre um run proprio, roda o ciclo inteiro e o encerra."""
    barras = executor.carregar_janela(conn, dataset_id)
    if not barras:
        raise ValueError("janela vazia: nao ha o que observar nem executar")

    run_id, _ = livro.abrir_run(
        conn,
        config_version_id=config_version_id,
        seed_capital_usd_cents=config.seed_capital_usd_cents,
    )

    dep = grafo.Dependencias(
        conn=conn, config=config, settings=settings, adaptador=adaptador
    )
    estado = grafo.rodar(dep, run_id=run_id, barras=barras)

    ativa = propostas.regra_ativa(conn, run_id)
    if ativa is not None:
        regra: Regra = estado["regra"]
        rule_id = int(ativa["rule_id"])
        veio_do_cerebro = True
    else:
        # O cerebro nao produziu regra valida. As maos rapidas continuam.
        regra = grafo.regra_padrao(config)
        rule_id = registro_de_regra.registrar(conn, regra)
        veio_do_cerebro = False

    resultado = executor.rodar(
        conn,
        run_id=run_id,
        dataset_id=dataset_id,
        regra=regra,
        rule_id=rule_id,
        config=config,
        barras=barras,
    )
    livro.encerrar_run(conn, run_id, "concluido")

    # O controle do acaso, casado com o giro DESTE run (D19, ADR 0014).
    # Cada ida e volta paga um pedagio fixo e entrada aleatoria nao tem
    # vantagem nenhuma - entao um B1 que gire mais que o agente perde por
    # atrito, e o agente pareceria bom por ter operado menos. Produzido aqui
    # para que os dois numeros nunca existam separados.
    b1_casado = None
    if resultado.operacoes > 0:
        try:
            b1_casado = baselines.b1_casado_com(
                conn,
                dataset_id=dataset_id,
                config=config,
                config_version_id=config_version_id,
                operacoes_alvo=resultado.operacoes,
                # O MESMO tamanho de posicao da regra executada (secao 14.3).
                # Casar o giro e nao casar o tamanho mede dimensionamento em
                # vez de timing - o mesmo erro da D19, um nivel abaixo.
                fracao_bps=regra.position_fraction_bps,
                semente=config.default_seed,
                barras=barras,
            )
        except ValueError as erro:
            # Janela curta demais para sortear tantos pares, por exemplo.
            # O run do agente ja terminou e continua valido; o que falta e o
            # controle, e isso precisa ficar dito em vez de sumir.
            log.warning("cerebro.b1_casado_falhou", extra={"motivo": str(erro)})

    reflexoes = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM agent_event"
            " WHERE run_id = ? AND provider IS NOT NULL",
            (run_id,),
        ).fetchone()["n"]
    )

    # A avaliacao posterior: evento NOVO, filho da decisao (R25.3, regra 17).
    #
    # Depois do encerramento de proposito. "Posterior" e o adjetivo que define
    # este evento: ele existe justamente porque o resultado so e conhecido
    # quando tudo acabou. Emiti-lo antes seria declarar realizado o que ainda
    # nao aconteceu - e o erro simetrico ao de editar a decisao depois.
    avaliacao_event_id = avaliacao.registrar(
        conn,
        run_id=run_id,
        config=config,
        b1_casado=b1_casado,
        operacoes=resultado.operacoes,
        reflexoes=reflexoes,
    )

    # Do LEDGER, e nao de um acumulador do executor: o saldo tem uma fonte
    # so (regra 16). E o mesmo `caixa_cents` que os baselines usam, entao os
    # numeros sao comparaveis por construcao e nao por coincidencia.
    carteira = livro.carteira(conn, run_id=run_id)

    ciclo = ResultadoDoCiclo(
        run_id=run_id,
        reflexoes=reflexoes,
        parou_em=estado.get("parou_em"),
        motivo=estado.get("motivo"),
        regra_veio_do_cerebro=veio_do_cerebro,
        rule_id=rule_id,
        regra_hash=regra.hash(),
        proposal_id=int(ativa["proposal_id"]) if ativa else None,
        expectativa=ativa["expectation"] if ativa else None,
        confianca_ppm=ativa["confidence_ppm"] if ativa else None,
        execucao=resultado.como_dict(),
        patrimonio_final_cents=simulador.caixa_cents(conn, run_id),
        custos={
            "execucao_total": carteira["simulado_usd"]["custo_execucao_minor"],
            "posicao_aberta_minor": carteira["simulado_usd"]["posicao_btc_minor"],
            "reflexao_total": carteira["simulado_usd"]["tesouraria_minor"],
        },
        b1_casado=b1_casado,
        gasto=livro.gasto_com_reflexao(conn, run_id),
        sobreposicao=propostas.sobreposicao_amostral(conn, run_id),
        condicoes_validade=condicoes_do_run(conn, run_id),
        avaliacao_event_id=avaliacao_event_id,
    )
    log.info(
        "cerebro.ciclo",
        extra={
            "run_id": run_id,
            "reflexoes": reflexoes,
            "regra_veio_do_cerebro": veio_do_cerebro,
            "operacoes": resultado.operacoes,
            "patrimonio_final_cents": simulador.caixa_cents(conn, run_id),
        },
    )
    return ciclo


def caminho_percorrido(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """A sequencia de eventos do run, na ordem em que aconteceram (R25.1).

    Inclui paradas e erros. Um caminho que so mostra os runs bem-sucedidos
    nao e o caminho percorrido, e a metade agradavel dele.
    """
    return [
        dict(l)
        for l in conn.execute(
            "SELECT id, parent_event_id, occurred_at, node, kind, tier, provider,"
            " model, tokens_in, tokens_out, tokens_cache_read, tokens_cache_write,"
            " cost_usd_minor, cost_usd_micro, price_table_version, expectation,"
            " confidence_ppm, ledger_transaction_id, profile_id"
            " FROM agent_event WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
    ]
