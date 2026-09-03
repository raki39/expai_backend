"""A avaliacao posterior: evento novo, filho da decisao (R25.3, regra 17).

A expectativa e a confianca sao declaradas **antes** de qualquer execucao e
ficam gravadas na decisao. Depois que as maos rapidas rodam, existe informacao
nova - o que de fato aconteceu. Essa informacao **nao entra editando a
decisao**: entra como um evento filho, e `agent_event` recusa `UPDATE` por
gatilho desde a migracao 3, de modo que a alternativa nem esta disponivel.

## Por que nao e um no do grafo

O grafo da 0A tem quatro nos (D14, ADR 0005) e termina em `registrar_intencao`,
**antes** da execucao. As maos rapidas nao sao nos do grafo (regra 3). A
avaliacao acontece depois delas, entao nao cabe no grafo sem desfazer as duas
decisoes - e a R25.1 pede "funcoes nomeadas e ordenadas, cada uma emitindo um
evento imutavel", nao pede que sejam nos de LangGraph. Aqui e uma funcao
nomeada, chamada depois da execucao, que emite seu evento.

## O que este evento NAO faz

Nao copia a expectativa declarada. Ela vive na decisao, que e o pai deste
evento e e imutavel: duplica-la aqui criaria duas gravacoes da mesma
afirmacao, e este projeto ja registrou cinco vezes o que acontece quando um
valor para de descrever o que dizia. O leitor chega ao declarado seguindo a
aresta - que e justamente o vinculo que a R25.3 exige que exista.

Nao copia o pre-registro. Vale o mesmo motivo: ele vive na hipotese, que e
imutavel, e o leitor chega la pela aresta.

## O veredito, que na 0A nao existia

A 0A fechou com `veredito_da_expectativa = None` e o motivo por extenso: a
expectativa era texto livre ("espero entre 3 e 8 operacoes"), julgar se ela se
cumpriu exigiria nova inferencia, e a 0A nao promovia conhecimento. Aquele
`None` era a resposta certa para o campo que existia.

Na 0B o campo e outro. O pre-registro da secao 8.2 traz `efeito_minimo` e
`condicoes_falseamento` **estruturados e declarados antes da execucao**, e o
veredito passa a ser uma comparacao - `hipotese.veredito.emitir`, derivada,
com os tres valores da secao 14.4 mais o `None` de quando nao se sabe.

O que continua verdade: nada aqui e escrito a mao, e `None` nao e `False`.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from ..config.schema import ExperimentConfig
from ..hipotese import poder
from ..hipotese import registro as hipotese_registro
from ..hipotese import veredito as veredito_mod
from ..hipotese.schema import PreRegistroBruto
from ..ledger.livro import fx_micro, registrar_custo_reflexao
from ..maos_rapidas import executor
from ..simulador import execucao as simulador

log = logging.getLogger(__name__)

# A decisao a que uma avaliacao se pendura. Um nome so, num lugar so: se o no
# for renomeado, e aqui que a busca falha - e nao silenciosamente na aresta.
NO_DA_DECISAO = "registrar_intencao"

# Continua existindo, e continua sendo a resposta certa QUANDO E O CASO: um
# run sem pre-registro (o da regra padrao da D23, por exemplo) nao tem o que
# julgar. O que mudou e que ele deixou de ser a unica resposta possivel.
MOTIVO_SEM_PRE_REGISTRO = (
    "este run nao tem pre-registro: a regra veio do padrao (D23) e nenhuma"
    " hipotese foi declarada, entao nao ha efeito minimo nem condicao de"
    " falseamento contra o que julgar"
)


def evento_da_decisao(conn: sqlite3.Connection, run_id: int) -> int | None:
    """O evento que declarou a intencao deste run, se houve um.

    Nao ha, quando o cerebro nao produziu regra valida ou o teto o calou: o
    run rodou com a regra padrao (D23) e **nenhuma expectativa foi
    declarada**. Nesse caso nao existe o que avaliar, e inventar um pai para o
    evento seria pendurar uma comparacao em quem nao afirmou nada.
    """
    linha = conn.execute(
        "SELECT id FROM agent_event"
        " WHERE run_id = ? AND node = ? AND kind = 'intencao'"
        " ORDER BY id DESC LIMIT 1",
        (run_id, NO_DA_DECISAO),
    ).fetchone()
    return int(linha["id"]) if linha else None


def faixa_contra_o_acaso(patrimonio_cents: int, b1: dict | None) -> str:
    """Onde o resultado caiu na distribuicao do acaso com o MESMO giro.

    Quatro faixas, e as quatro importam. Com tres, um agente pior que 95% das
    entradas ao acaso recebia a mesma descricao de um agente mediano - defeito
    real, encontrado na leitura do painel.
    """
    if b1 is None:
        return "sem_controle"
    if patrimonio_cents < b1["p5"]:
        return "abaixo_p5"
    if patrimonio_cents < b1["p50"]:
        return "entre_p5_e_p50"
    if patrimonio_cents <= b1["p95"]:
        return "entre_p50_e_p95"
    return "acima_p95"


def _veredito_do_run(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    patrimonio_cents: int,
    idas_e_voltas: int,
    b1_casado: dict | None,
    retornos_bps: list[int],
    duracao_barra_ms: int,
) -> dict:
    """Monta o bloco de veredito. Derivado, ponta a ponta.

    Sem pre-registro nao ha veredito, e o motivo fica escrito - continua
    valendo o desenho da 0A: `None` com a razao ao lado, nunca um `False` que
    ninguem calculou.
    """
    hip = hipotese_registro.do_run(conn, run_id)
    if hip is None:
        return {
            "veredito": None,
            "motivo": MOTIVO_SEM_PRE_REGISTRO,
            "hypothesis_id": None,
        }

    bruto = PreRegistroBruto.model_validate(
        {
            "enunciado": hip["enunciado"],
            "metrica_primaria": hip["metrica_primaria"],
            "efeito_minimo": hip["efeito_minimo"],
            "sharpe_esperado_milesimos": hip["sharpe_esperado_milesimos"],
            "criterio_parada": hip["criterio_parada"],
            "condicoes_falseamento": hip["condicoes_falseamento"],
        }
    )

    # `n_bruto` sao as barras em que houve POSICAO, nao as da janela: barra
    # fora do mercado nao observa nada sobre a vantagem da regra.
    bruto_n = executor.barras_expostas(conn, run_id, duracao_barra_ms)
    efetivo = poder.efetivo_de_bruto(retornos_bps, bruto_n)

    realizado = veredito_mod.observar(
        conn,
        run_id=run_id,
        patrimonio_cents=patrimonio_cents,
        idas_e_voltas=idas_e_voltas,
        b1_casado=b1_casado,
    )
    v = veredito_mod.emitir(
        bruto,
        realizado,
        n_efetivo=efetivo.efetivo,
        n_minimo=hip["n_minimo"],
    )
    saida = v.como_dict()
    saida["hypothesis_id"] = hip["id"]
    saida["testavel"] = hip["testavel"]
    saida["amostra"]["n_bruto"] = efetivo.bruto
    saida["amostra"]["autocorrelacao_ppm"] = efetivo.autocorrelacao_ppm
    saida["amostra"]["fator_ppm"] = efetivo.fator_ppm
    saida["metricas_indisponiveis"] = realizado.indisponiveis
    return saida


def registrar(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    config: ExperimentConfig,
    b1_casado: dict | None,
    operacoes: int,
    reflexoes: int,
    retornos_bps: list[int] | None = None,
    duracao_barra_ms: int = 900_000,
) -> int | None:
    """Emite o evento de avaliacao. Devolve o id, ou `None` se nao ha o que avaliar.

    O patrimonio vem do LEDGER (regra 16), nao de um acumulador: e o mesmo
    numero que os baselines usam, entao a comparacao e por construcao.
    """
    pai = evento_da_decisao(conn, run_id)
    if pai is None:
        log.info("avaliacao.sem_decisao_declarada", extra={"run_id": run_id})
        return None

    patrimonio = simulador.caixa_cents(conn, run_id)
    faixa = faixa_contra_o_acaso(patrimonio, b1_casado)
    idas = executor.idas_e_voltas(conn, run_id)
    julgamento = _veredito_do_run(
        conn,
        run_id=run_id,
        patrimonio_cents=patrimonio,
        idas_e_voltas=idas,
        b1_casado=b1_casado,
        retornos_bps=retornos_bps or [],
        duracao_barra_ms=duracao_barra_ms,
    )

    payload = {
        "declarado_no_evento": pai,
        "realizado": {
            "patrimonio_final_cents": patrimonio,
            "operacoes": operacoes,
            "idas_e_voltas": idas,
            "reflexoes": reflexoes,
        },
        "contra_o_acaso": (
            {
                "b1_p5_cents": b1_casado["p5"],
                "b1_p50_cents": b1_casado["p50"],
                "b1_p95_cents": b1_casado["p95"],
                "operacoes_alvo": b1_casado.get("operacoes_alvo"),
                "repeticoes": b1_casado.get("repeticoes"),
                "excesso_sobre_p50_cents": patrimonio - b1_casado["p50"],
            }
            if b1_casado
            else None
        ),
        "faixa": faixa,
        # O julgamento do pre-registro (secao 8.2), derivado. Substitui o
        # `veredito_da_expectativa: None` da 0A, que existia porque a
        # expectativa era prosa.
        "pre_registro": julgamento,
        "em_amostra": True,
    }

    event_id, _ = registrar_custo_reflexao(
        conn,
        run_id=run_id,
        node="avaliar_resultado",
        kind="avaliacao",
        custo_usd_minor=0,
        custo_usd_micro=0,
        fx_rate_micro=fx_micro(config.fx_brl_per_usd),
        fx_rate_date=config.fx_rate_date,
        parent_event_id=pai,
        evaluation_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )
    log.info(
        "avaliacao.registrada",
        extra={
            "run_id": run_id,
            "event_id": event_id,
            "pai": pai,
            "faixa": faixa,
            "veredito": julgamento.get("veredito"),
        },
    )
    return event_id


def do_run(conn: sqlite3.Connection, run_id: int) -> dict | None:
    """A avaliacao do run com as duas metades juntas: declarado e realizado.

    O declarado e lido do PAI, seguindo a aresta. E a leitura que prova que o
    vinculo da R25.3 serve para algo alem de existir.
    """
    linha = conn.execute(
        "SELECT filho.id AS id, filho.occurred_at AS occurred_at,"
        "       filho.evaluation_json AS evaluation_json,"
        "       pai.id AS decisao_id, pai.occurred_at AS decisao_em,"
        "       pai.expectation AS expectativa, pai.outputs_digest AS regra_hash"
        "  FROM agent_event filho"
        "  JOIN agent_event pai ON pai.id = filho.parent_event_id"
        " WHERE filho.run_id = ? AND filho.kind = 'avaliacao'"
        " ORDER BY filho.id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if linha is None:
        return None

    # A confianca vem do EVENTO da decisao, que e o pai deste. Nao de
    # `rule_proposal`: o `agent_event_id` de la aponta para o evento de
    # `propor_regra`, um passo antes, e juntar por ele nunca casaria.
    confianca = conn.execute(
        "SELECT confidence_ppm FROM agent_event WHERE id = ?",
        (int(linha["decisao_id"]),),
    ).fetchone()

    return {
        "avaliacao_event_id": int(linha["id"]),
        "avaliada_em": linha["occurred_at"],
        "decisao": {
            "event_id": int(linha["decisao_id"]),
            "declarada_em": linha["decisao_em"],
            "expectativa": linha["expectativa"],
            "confianca_ppm": (
                int(confianca["confidence_ppm"])
                if confianca and confianca["confidence_ppm"] is not None
                else None
            ),
            "regra_hash": linha["regra_hash"],
        },
        "comparacao": json.loads(linha["evaluation_json"]),
    }
