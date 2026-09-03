"""Roda A1a: seis controles negativos determinísticos, pelo MESMO caminho.

> "Um conjunto de hipóteses construídas para revelar defeito é injetado **pelo
> mesmo caminho das reais**. Tolerância zero: uma única promoção reprova a
> fase." — §14.4, critério A1a

"Pelo mesmo caminho" é a exigência inteira. Um controle que rodasse num lote
separado não enfrentaria a multiplicidade do lote real, e o defeito que só se
manifesta sob ela — que é justamente o que a tolerância zero existe para pegar
— não apareceria. Por isso cada controle abre run, emite evento, registra
pré-registro, entra na máquina de estados e paga crédito, exatamente como uma
hipótese do agente ou de B4.

## O que este módulo NÃO decide

Ele não decide se a fase passa. Ele injeta, observa e devolve o que aconteceu.
Comparar o observado com o esperado é do relatório do Portão A — e a separação
existe porque uma injeção que soubesse o resultado esperado poderia produzi-lo
sem que nada tivesse acontecido.

## Os seis lugares na família fechada

A D25 reservou 6 dos 48 para A1a, um por família de defeito de §14.4. Cada
controle registra **uma** hipótese, e é ela que ocupa o lugar. Isso encarece o
limiar de BY para todo mundo, inclusive para o agente — e §14.4 já diz que
isso é conservador na direção certa.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

from .. import creditos as creditos_mod
from ..config.schema import ExperimentConfig
from ..dataset import loader
from ..hipotese import registro as hipotese_registro
from ..hipotese.poder import sharpe_minimo_testavel
from ..hipotese.schema import (
    SHARPE_MAX_MILESIMOS,
    ClausulaFalseamento,
    PreRegistroBruto,
)
from ..ledger import livro
from ..maos_rapidas import executor
from ..regra.schema import CruzamentoMedias, Regra, condicoes_da_config
from ..regra import registro as registro_de_regra
from ..simulador import execucao as simulador
from ..validador import estados as validador_estados
from ..validador import promocao as validador_promocao
from . import catalogo, injecoes

log = logging.getLogger(__name__)

#: O braço de crédito. Separado de `a1b` porque as tolerâncias são
#: diferentes: aqui uma promoção reprova a fase, lá uma promoção ocasional é o
#: comportamento esperado de um procedimento com FDR positivo.
BRACO = "a1a"

#: O dono dos runs de controle. Nem o agente, nem B4, nem baseline.
AGENT_ID = "a1a-0001"

#: A métrica dos controles estatísticos. A mesma de B4, e pelo mesmo motivo:
#: o validador a avalia de ponta a ponta e ela cumpre a regra 14. Fixa, como
#: em B4 — um controle que escolhesse a régua poderia comprar sobrevivência.
METRICA = "excesso_sobre_b3_cents"

#: Giro alto de propósito no controle de custos: com ~27 bps por ida e volta,
#: é o giro que faz a diferença entre bruto e líquido dominar o resultado.
#: 2/3 é o cruzamento mais rápido que o catálogo aceita.
REGRA_DE_GIRO_ALTO = {"rapida": 2, "lenta": 3}


class BaselineAusente(Exception):
    """Sem B3 sob esta config não há contra o que medir (mesmo motivo de B4)."""


class SeparacaoAusente(Exception):
    """Sem os quatro conjuntos de §8.5.1 o controle leria o que é do validador."""


@dataclass(frozen=True)
class ResultadoDeUm:
    chave: str
    familia_de_defeito: str
    tipo: str
    run_id: int
    hypothesis_id: int | None
    tentativas: list[dict]
    #: `True` quando TODA tentativa da injeção foi recusada.
    barrado: bool
    veredito: str | None
    motivo: str | None
    creditos_cobrados: int | None
    estado_final: str | None
    promovido: bool
    observado: dict

    def como_dict(self) -> dict:
        return {
            "chave": self.chave,
            "familia_de_defeito": self.familia_de_defeito,
            "tipo": self.tipo,
            "run_id": self.run_id,
            "hypothesis_id": self.hypothesis_id,
            "tentativas": self.tentativas,
            "barrado": self.barrado,
            "veredito": self.veredito,
            "motivo": self.motivo,
            "creditos_cobrados": self.creditos_cobrados,
            "estado_final": self.estado_final,
            "promovido": self.promovido,
            "observado": self.observado,
        }


@dataclass(frozen=True)
class ResultadoA1a:
    config_version_id: int
    dataset_id: int
    controles: list[ResultadoDeUm]
    cpu_micros: int

    def como_dict(self) -> dict:
        promovidos = [c.chave for c in self.controles if c.promovido]
        return {
            "braco": BRACO,
            "mensagem": (
                f"{len(self.controles)} controles deterministicos,"
                f" {sum(c.creditos_cobrados or 0 for c in self.controles)}"
                f" creditos."
                + (
                    f" PROMOVIDO(S): {', '.join(promovidos)}."
                    if promovidos
                    else " Nenhum promovido."
                )
            ),
            "config_version_id": self.config_version_id,
            "dataset_id": self.dataset_id,
            "quantos": len(self.controles),
            "creditos_consumidos": sum(
                c.creditos_cobrados or 0 for c in self.controles
            ),
            # §14.4, tolerancia ZERO. A lista, e nao a contagem: o relatorio
            # precisa dizer QUAL controle passou.
            "promovidos": promovidos,
            "cpu_micros": self.cpu_micros,
            "controles": [c.como_dict() for c in self.controles],
        }


def _pre_registro(
    familia: catalogo.Familia,
    *,
    config: ExperimentConfig,
    duracao_barra_ms: int,
    horizonte_barras: int,
) -> PreRegistroBruto:
    """O pré-registro do controle. Declara a procedência em maiúsculas.

    Mesma disciplina do enunciado de B4, e pelo mesmo motivo: se um controle
    escrevesse aqui uma frase plausível sobre o mercado, o registro ficaria com
    duas afirmações indistinguíveis — uma pensada e uma construída para revelar
    defeito — e a leitura do lote perderia sentido no próprio dado.
    """
    efeito = config.seed_capital_usd_cents * 500 // 10_000
    piso = sharpe_minimo_testavel(
        duracao_barra_ms=duracao_barra_ms,
        horizonte_barras=max(1, horizonte_barras),
    )
    return PreRegistroBruto(
        enunciado=(
            f"CONTROLE NEGATIVO DETERMINISTICO (A1a, secao 14.4)."
            f" Familia de defeito: {familia.familia_de_defeito}."
            f" Esta hipotese foi CONSTRUIDA PARA REVELAR DEFEITO e nao para"
            f" descrever o mercado. Injecao: {familia.o_que_injeta}."
            f" Guarda esperada: {familia.guarda_esperada}."
            f" Promover esta linha reprova a fase (tolerancia zero)."
        ),
        metrica_primaria=METRICA,
        efeito_minimo=efeito,
        sharpe_esperado_milesimos=min(piso, SHARPE_MAX_MILESIMOS),
        criterio_parada="fim_da_janela",
        condicoes_falseamento=[
            ClausulaFalseamento(
                metrica=METRICA, comparador="menor_que", valor=efeito
            ),
            ClausulaFalseamento(
                metrica="idas_e_voltas", comparador="maior_que", valor=20_000
            ),
        ],
    )


def _pre_registro_duplicado(
    conn: sqlite3.Connection, config_version_id: int
) -> tuple[PreRegistroBruto, dict] | None:
    """A mesma afirmação testável de uma hipótese já registrada, reescrita.

    O disfarce é **só o enunciado**: métrica, efeito mínimo, Sharpe, critério
    de parada e condições de falseamento saem idênticos da linha original.
    É a definição de "duplicação disfarçada".

    `None` quando não há hipótese anterior nesta config — o controle não tem o
    que duplicar, e dizer isso é melhor que duplicar a si mesmo, que mediria
    outra coisa.
    """
    linha = conn.execute(
        "SELECT h.* FROM hypothesis h JOIN run r ON r.id = h.run_id"
        " WHERE r.config_version_id = ? AND h.agente_origem <> ?"
        " ORDER BY h.id DESC LIMIT 1",
        (config_version_id, hipotese_registro.AGENTE_ORIGEM_A1A),
    ).fetchone()
    if linha is None:
        return None
    original = hipotese_registro.como_dict(linha)
    bruto = PreRegistroBruto(
        enunciado=(
            "CONTROLE NEGATIVO DETERMINISTICO (A1a, secao 14.4). Duplicacao"
            " disfarcada da hipotese "
            f"{original['id']}: a afirmacao testavel abaixo e identica a dela"
            " campo por campo, e so este texto foi reescrito."
        ),
        metrica_primaria=original["metrica_primaria"],
        efeito_minimo=original["efeito_minimo"],
        sharpe_esperado_milesimos=original["sharpe_esperado_milesimos"],
        criterio_parada=original["criterio_parada"],
        condicoes_falseamento=[
            ClausulaFalseamento(**c) for c in original["condicoes_falseamento"]
        ],
    )
    return bruto, original


def _evento(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    config: ExperimentConfig,
    chave: str,
) -> int:
    """Evento não cognitivo, pela mesma função que grava o do agente.

    `provider` nulo e custo zero, como em B4: um controle que aparecesse como
    reflexão inflaria a contagem de decisões cognitivas da fase.
    """
    event_id, _ = livro.registrar_custo_reflexao(
        conn,
        run_id=run_id,
        node=f"a1a_{chave}",
        kind="proposta_nao_cognitiva",
        custo_usd_minor=0,
        custo_usd_micro=0,
        fx_rate_micro=livro.fx_micro(config.fx_brl_per_usd),
        fx_rate_date=config.fx_rate_date,
        expectation=None,
        confidence_ppm=None,
    )
    return event_id


def _injetar(
    familia: catalogo.Familia,
    conn: sqlite3.Connection,
    *,
    dataset_id: int,
    run_id: int,
    decision_ts_ms: int,
) -> list[injecoes.Tentativa]:
    if familia.chave == "acesso_ao_futuro":
        return injecoes.acesso_ao_futuro(
            conn, dataset_id=dataset_id, run_id=run_id,
            decision_ts_ms=decision_ts_ms,
        )
    if familia.chave == "violacao_do_embargo":
        return injecoes.violacao_do_embargo(conn, dataset_id=dataset_id)
    if familia.chave == "preco_impossivel":
        return injecoes.preco_impossivel(
            conn, dataset_id=dataset_id, run_id=run_id,
            decision_ts_ms=decision_ts_ms,
        )
    if familia.chave == "ledger_adulterado":
        return injecoes.ledger_adulterado(conn, run_id=run_id)
    if familia.chave == "lucro_so_sem_custos":
        return injecoes.metrica_sem_custo()
    # `duplicacao_disfarcada` injeta no PRE-REGISTRO, e nao aqui: o disfarce e
    # a propria hipotese. A lista vazia diz isso.
    return []


def rodar(
    conn: sqlite3.Connection,
    *,
    dataset_id: int,
    config: ExperimentConfig,
    config_version_id: int,
    settings=None,
) -> ResultadoA1a:
    """Os seis controles, cada um no seu run.

    `settings` entra e não é usado, como em B4: mantém a rota simétrica sem
    que o controle ganhe acesso a credencial nenhuma.
    """
    if not loader.esta_dividido(conn, dataset_id):
        raise SeparacaoAusente(
            f"o dataset {dataset_id} nao tem a divisao por finalidade da"
            " secao 8.5.1; rode a separacao antes de A1a"
        )
    if not conn.execute(
        "SELECT 1 FROM run WHERE agent_id = 'baseline-B3'"
        " AND config_version_id = ?",
        (config_version_id,),
    ).fetchone():
        raise BaselineAusente(
            f"nenhum B3 sob a config_version {config_version_id}; a metrica"
            f" dos controles estatisticos e {METRICA!r} e sem o baseline ela"
            " nao tem contra o que ser medida. Rode a comparacao antes de A1a"
        )

    barras = executor.carregar_janela(
        conn, dataset_id, finalidade=executor.FINALIDADE_DE_EXECUCAO
    )
    if not barras:
        raise ValueError("janela de execucao vazia")
    duracao = (
        int(barras[1].open_time_ms - barras[0].open_time_ms)
        if len(barras) >= 2
        else 900_000
    )
    decision_ts_ms = int(barras[-1].open_time_ms)

    creditos_mod.conceder(
        conn,
        braco=BRACO,
        config_version_id=config_version_id,
        creditos=config.creditos_por_braco,
    )

    comeco = time.perf_counter_ns()
    saida: list[ResultadoDeUm] = []
    for familia in catalogo.FAMILIAS:
        saida.append(
            _rodar_um(
                conn,
                familia,
                dataset_id=dataset_id,
                config=config,
                config_version_id=config_version_id,
                barras=barras,
                duracao=duracao,
                decision_ts_ms=decision_ts_ms,
            )
        )

    cpu = (time.perf_counter_ns() - comeco) // 1_000
    log.info(
        "a1a.braco",
        extra={
            "quantos": len(saida),
            "promovidos": [c.chave for c in saida if c.promovido],
            "cpu_micros": cpu,
        },
    )
    return ResultadoA1a(
        config_version_id=config_version_id,
        dataset_id=dataset_id,
        controles=saida,
        cpu_micros=cpu,
    )


def _rodar_um(
    conn: sqlite3.Connection,
    familia: catalogo.Familia,
    *,
    dataset_id: int,
    config: ExperimentConfig,
    config_version_id: int,
    barras,
    duracao: int,
    decision_ts_ms: int,
) -> ResultadoDeUm:
    run_id, _ = livro.abrir_run(
        conn,
        config_version_id=config_version_id,
        seed_capital_usd_cents=config.seed_capital_usd_cents,
        agent_id=AGENT_ID,
    )
    event_id = _evento(conn, run_id=run_id, config=config, chave=familia.chave)

    observado: dict = {}
    duplicado = None
    if familia.chave == "duplicacao_disfarcada":
        duplicado = _pre_registro_duplicado(conn, config_version_id)
        if duplicado is None:
            observado["nao_injetado"] = (
                "nao ha hipotese anterior nesta config_version para duplicar;"
                " duplicar a si mesmo mediria outra coisa"
            )
    bruto = (
        duplicado[0]
        if duplicado is not None
        else _pre_registro(
            familia,
            config=config,
            duracao_barra_ms=duracao,
            horizonte_barras=len(barras),
        )
    )

    regra = Regra(
        params=CruzamentoMedias(
            **(
                REGRA_DE_GIRO_ALTO
                if familia.chave == "lucro_so_sem_custos"
                else {"rapida": 20, "lenta": 50}
            )
        ),
        position_fraction_bps=10_000,
        condicoes_validade=condicoes_da_config(config),
    )
    rule_id = registro_de_regra.registrar(conn, regra)
    hypothesis_id, _ = hipotese_registro.registrar(
        conn,
        run_id=run_id,
        agent_event_id=event_id,
        bruto=bruto,
        condicoes_validade=regra.condicoes_validade.model_dump(mode="json"),
        duracao_barra_ms=duracao,
        horizonte_barras=len(barras),
        rule_id=rule_id,
        agente_origem=hipotese_registro.AGENTE_ORIGEM_A1A,
    )
    validador_promocao.admitir(conn, hypothesis_id, run_id=run_id)

    if duplicado is not None:
        original = duplicado[1]
        atual = hipotese_registro.por_id(conn, hypothesis_id)
        observado["duplicata"] = {
            "hypothesis_original": original["id"],
            "content_hash_original": original["content_hash"],
            "content_hash_da_duplicata": atual["content_hash"],
            # O achado, com o numero ao lado: o disfarce DERROTA o hash,
            # porque `enunciado` entra nele. A consequencia e de PRECO -
            # cobra 1 em vez dos 3 de §8.6.1 -, e nao de multiplicidade: a
            # linha ocupa lugar na familia de 48 e entra no contador global
            # do DSR do mesmo jeito.
            "reconhecida_como_reteste": (
                original["content_hash"] == atual["content_hash"]
            ),
            "o_que_protege": (
                "o teto da familia e o contador global contam esta linha, e e"
                " a multiplicidade que BY corrige. O preco do teste, esse"
                " sai errado: 1 credito em vez dos 3 de §8.6.1"
            ),
        }

    tentativas = _injetar(
        familia, conn, dataset_id=dataset_id, run_id=run_id,
        decision_ts_ms=decision_ts_ms,
    )
    barrado = bool(tentativas) and all(t.barrada for t in tentativas)

    veredito = motivo = estado_final = None
    creditos = None
    if familia.tipo == catalogo.ESTATISTICO:
        resultado = executor.rodar(
            conn,
            run_id=run_id,
            dataset_id=dataset_id,
            regra=regra,
            rule_id=rule_id,
            config=config,
            barras=barras,
        )
        liquido = simulador.caixa_cents(conn, run_id)
        custos = livro.carteira(conn, run_id=run_id)["simulado_usd"][
            "custo_execucao_minor"
        ]
        observado["economia"] = {
            "idas_e_voltas": resultado.operacoes,
            "ordens_executadas": resultado.execucoes,
            "liquido_cents": liquido,
            # O que o resultado seria se os custos fossem ignorados. Sai da
            # DECOMPOSICAO do ledger, e nao de uma segunda simulacao: taxa,
            # spread, slippage e penalidade sao contas proprias desde o
            # incremento 3, e a soma delas e exatamente a diferenca.
            "bruto_cents": liquido + custos,
            "custo_de_execucao_cents": custos,
        }
        try:
            parecer = validador_promocao.avaliar_in_sample(
                conn, hypothesis_id=hypothesis_id, run_id=run_id
            )
            veredito, motivo, creditos = (
                parecer.veredito, parecer.motivo, parecer.creditos
            )
            estado_final = parecer.estado_final
        except validador_promocao.NaoAvaliavel as erro:
            motivo = str(erro)

    livro.encerrar_run(conn, run_id, "concluido")

    estado = validador_estados.atual(conn, hypothesis_id)
    estado_final = estado.estado if estado else estado_final
    return ResultadoDeUm(
        chave=familia.chave,
        familia_de_defeito=familia.familia_de_defeito,
        tipo=familia.tipo,
        run_id=run_id,
        hypothesis_id=hypothesis_id,
        tentativas=[t.como_dict() for t in tentativas],
        barrado=barrado,
        veredito=veredito,
        motivo=motivo,
        creditos_cobrados=creditos,
        estado_final=estado_final,
        # A definicao de PROMOVIDO e a da maquina de estados, e nao o veredito
        # em texto: §8.1 diz que promover e mover a hipotese, e e a transicao
        # que fica gravada. Ler o veredito aqui deixaria de fora uma promocao
        # que acontecesse por outro caminho - que e exatamente o defeito que
        # a tolerancia zero existe para pegar.
        promovido=validador_estados.promovida(estado.estado if estado else None),
        observado=observado,
    )


def resumo(conn: sqlite3.Connection, config_version_id: int) -> dict:
    """O estado do braço sob esta config, derivado do banco.

    Nada guardado: os controles são linhas de `hypothesis` com `agente_origem`
    próprio, e o resumo é uma consulta sobre elas — o mesmo desenho do resumo
    de B4, e pelo mesmo motivo (regra 16).
    """
    linhas = []
    for l in conn.execute(
        "SELECT h.id AS hypothesis_id, h.run_id AS run_id,"
        "       h.content_hash AS content_hash, h.enunciado AS enunciado,"
        "       h.metrica_primaria AS metrica_primaria"
        "  FROM hypothesis h JOIN run r ON r.id = h.run_id"
        " WHERE h.agente_origem = ? AND r.config_version_id = ?"
        " ORDER BY h.id",
        (hipotese_registro.AGENTE_ORIGEM_A1A, config_version_id),
    ):
        linha = dict(l)
        estado = validador_estados.atual(conn, int(l["hypothesis_id"]))
        linha["estado"] = estado.estado if estado else None
        linha["promovido"] = validador_estados.promovida(
            estado.estado if estado else None
        )
        linha["parecer"] = validador_promocao.parecer_derivado(
            conn, int(l["hypothesis_id"])
        )
        linha["testes"] = creditos_mod.testes_da_hipotese(
            conn, int(l["hypothesis_id"])
        )
        linhas.append(linha)
    return {
        "braco": BRACO,
        "config_version_id": config_version_id,
        "familias": [f.como_dict() for f in catalogo.FAMILIAS],
        "hipoteses": linhas,
        "quantas": len(linhas),
        "promovidos": [l["hypothesis_id"] for l in linhas if l["promovido"]],
        "creditos": creditos_mod.saldo(
            conn, braco=BRACO, config_version_id=config_version_id
        ),
        "tolerancia": (
            "zero: §14.4 diz que uma unica promocao de controle determinista"
            " reprova a fase, porque significa que existe um defeito no"
            " pipeline"
        ),
    }
