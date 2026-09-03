"""O grafo do cerebro lento: quatro nos, um `agent_event` por no.

    observar -> interpretar -> propor_regra -> registrar_intencao

`observar` e `registrar_intencao` sao deterministicos e nao custam nada.
`interpretar` e `propor_regra` chamam o modelo. Nenhum deles executa ordem:
quem executa sao as maos rapidas, depois, com a regra pronta na mao.

**Um evento por no, gravado antes de o fluxo continuar** (criterio 9, R25.1).
Um no que falha grava o evento de ERRO e o fluxo para ali - o caminho
percorrido inclui o que deu errado, senao o registro so conta a historia dos
runs que funcionaram, que e a historia menos util das duas.

**O estado do grafo e efemero** (criterio 12). Nao ha checkpointer, nao ha
persistencia de estado, e nenhuma projecao de carteira sai daqui: saldo vem
do ledger e de nenhum outro lugar (regra 16).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, TypedDict

from pydantic import ValidationError

from ..config.schema import ExperimentConfig
from ..dataset.loader import BarraCarregada
from ..hipotese import poder
from ..hipotese import registro as hipotese_registro
from ..ledger.livro import fx_micro, registrar_custo_reflexao
from ..regra import registro as registro_de_regra
from ..regra.schema import CruzamentoMedias, Regra
from ..settings import Settings
from . import contexto, propostas, prompts, reflexao
from .contrato import (
    SCHEMA_INTERPRETACAO,
    SCHEMA_PROPOSTA,
    Interpretacao,
    PropostaBruta,
    condicoes_da_config,
    montar_regra,
)
from .provedores import ProvedorIndisponivel
from .provedores.base import ErroDoProvedor

log = logging.getLogger(__name__)

# Teto de SAIDA por chamada. Nao e orcamento: cobra-se o que foi consumido,
# e o cap e barreira contra resposta desgovernada. Precisa ser folgado porque
# **o pensamento do modelo conta neste limite** - com 2.000 o pensamento
# consumiu tudo e a resposta voltou vazia, o que chegava a validacao disfarcado
# de "JSON invalido". A reserva do teto de gasto usa este numero como limite
# superior, entao aumenta-lo torna o teto mais conservador, nunca menos.
MAX_TOKENS_INTERPRETAR = 8_000
MAX_TOKENS_PROPOR = 8_000
TIER = "padrao"


class Estado(TypedDict, total=False):
    """Efemero. Vive o tempo de uma execucao do grafo e morre com ela."""

    run_id: int
    barras: list[BarraCarregada]
    resumo: contexto.ResumoDePeriodo
    interpretacao: Interpretacao
    proposta_bruta: PropostaBruta
    regra: Regra
    rule_id: int
    proposal_id: int
    hypothesis_id: int
    eventos: list[int]
    parou_em: str | None
    motivo: str | None


@dataclass(frozen=True)
class Dependencias:
    """O que os nos precisam e nao cabe no estado.

    Conexao de banco e configuracao nao sao estado de grafo: colocar uma
    conexao viva dentro do estado tornaria o estado nao-serializavel e
    convidaria alguem a persisti-lo depois, que e como um grafo vira segunda
    fonte de verdade.
    """

    conn: sqlite3.Connection
    config: ExperimentConfig
    settings: Settings
    adaptador: Any | None = None


def _duracao_da_barra(estado: Estado, config: ExperimentConfig) -> int:
    """Quanto dura uma barra, em ms, lido das BARRAS e nao do rotulo.

    A ingestao ja exige que barras consecutivas difiram de exatamente um
    intervalo - e a armadilha de unidade de timestamp registrada em
    `.aprendizado/binance-dados-notas.md` mostra que contagem de linha nao
    detecta o erro, mas espacamento detecta. Ler daqui e ler da mesma fonte
    que a conta de poder vai usar.

    Cai para o rotulo do timeframe so quando ha menos de duas barras, caso em
    que nao existe espacamento a medir.
    """
    barras = estado.get("barras") or []
    if len(barras) >= 2:
        return int(barras[1].open_time_ms - barras[0].open_time_ms)
    from ..dataset.binance import intervalo_ms

    return intervalo_ms(config.timeframe)


def regra_padrao(config: ExperimentConfig) -> Regra:
    """A regra que roda quando o cerebro nao fala (criterio 3).

    E o mesmo cruzamento de medias do B3, derivado da configuracao - nao uma
    constante ao lado dela. A consequencia e explicita e precisa ser lida como
    tal: **com o teto em zero, o agente E o baseline B3.** Por isso todo
    resultado do agente informa quantas reflexoes houve; um resultado com zero
    reflexoes nao esta medindo cerebro nenhum.
    """
    return Regra(
        params=CruzamentoMedias(rapida=config.b3_fast, lenta=config.b3_slow),
        condicoes_validade=condicoes_da_config(config),
    )


# ---------------------------------------------------------------------------
# Eventos deterministicos (custo zero, sem transacao no ledger)
# ---------------------------------------------------------------------------


def _evento(
    dep: Dependencias,
    *,
    run_id: int,
    node: str,
    kind: str,
    parent_event_id: int | None,
    outputs_digest: str | None = None,
    expectation: str | None = None,
    confidence_ppm: int | None = None,
) -> int:
    event_id, _ = registrar_custo_reflexao(
        dep.conn,
        run_id=run_id,
        node=node,
        kind=kind,
        custo_usd_minor=0,
        custo_usd_micro=0,
        fx_rate_micro=fx_micro(dep.config.fx_brl_per_usd),
        fx_rate_date=dep.config.fx_rate_date,
        parent_event_id=parent_event_id,
        outputs_digest=outputs_digest,
        expectation=expectation,
        confidence_ppm=confidence_ppm,
    )
    return event_id


def _ultimo_evento(estado: Estado) -> int | None:
    eventos = estado.get("eventos") or []
    return eventos[-1] if eventos else None


def _parar(estado: Estado, dep: Dependencias, no: str, motivo: str) -> dict:
    """Grava o evento de parada e devolve a atualizacao de estado."""
    event_id = _evento(
        dep,
        run_id=estado["run_id"],
        node=no,
        kind="parada",
        parent_event_id=_ultimo_evento(estado),
        outputs_digest=None,
        expectation=None,
    )
    log.info("cerebro.parou", extra={"no": no, "motivo": motivo})
    return {
        "eventos": [*(estado.get("eventos") or []), event_id],
        "parou_em": no,
        "motivo": motivo,
    }


# ---------------------------------------------------------------------------
# Os quatro nos
# ---------------------------------------------------------------------------


def no_observar(estado: Estado, dep: Dependencias) -> dict:
    """Estatisticas do periodo, em Python. Zero chamadas de modelo."""
    try:
        resumo = contexto.resumir(estado["barras"], dep.config)
    except Exception as erro:  # noqa: BLE001
        return _parar(estado, dep, "observar", f"{type(erro).__name__}: {erro}")

    event_id = _evento(
        dep,
        run_id=estado["run_id"],
        node="observar",
        kind="observacao",
        parent_event_id=None,
        outputs_digest=_digest(resumo.como_dict()),
    )
    return {
        "resumo": resumo,
        "eventos": [*(estado.get("eventos") or []), event_id],
    }


def no_interpretar(estado: Estado, dep: Dependencias) -> dict:
    resumo = estado["resumo"]
    try:
        chamada = reflexao.executar(
            dep.conn,
            run_id=estado["run_id"],
            node="interpretar",
            tier=TIER,
            sistema=prompts.SISTEMA,
            mensagens=(("user", prompts.mensagem_interpretar(resumo)),),
            schema=SCHEMA_INTERPRETACAO,
            schema_nome="interpretacao",
            max_tokens=MAX_TOKENS_INTERPRETAR,
            config=dep.config,
            settings=dep.settings,
            parent_event_id=_ultimo_evento(estado),
            adaptador=dep.adaptador,
        )
    except reflexao.TetoAtingido as teto:
        return _parar(estado, dep, "interpretar", teto.veredito.motivo)
    except (ErroDoProvedor, reflexao.TierNaoConfigurado,
            ProvedorIndisponivel) as erro:
        return _parar(estado, dep, "interpretar", f"{type(erro).__name__}: {erro}")

    eventos = [*(estado.get("eventos") or []), chamada.event_id]
    try:
        leitura = Interpretacao.model_validate_json(chamada.texto)
    except ValidationError as erro:
        return _parar(
            {**estado, "eventos": eventos},
            dep,
            "interpretar",
            # O motivo legivel, e nao a contagem: "1 erro(s)" manda procurar
            # no lugar errado, que e o modo de falha que este projeto ja
            # registrou quatro vezes. O que custa caro nao e falhar, e falhar
            # sem dizer onde.
            _motivo_legivel(erro),
        )
    return {"interpretacao": leitura, "eventos": eventos}


def no_propor_regra(estado: Estado, dep: Dependencias) -> dict:
    """Propoe a regra. Resposta invalida vira REJEICAO registrada.

    Criterio 2: a rejeicao e gravada com a resposta crua que a causou, e a
    regra ativa anterior permanece - o que aqui e estrutural, porque uma
    proposta rejeitada nao tem `rule_id` para apontar.
    """
    resumo = estado["resumo"]
    try:
        chamada = reflexao.executar(
            dep.conn,
            run_id=estado["run_id"],
            node="propor_regra",
            tier=TIER,
            sistema=prompts.SISTEMA,
            mensagens=(
                (
                    "user",
                    prompts.mensagem_propor(
                        resumo,
                        estado["interpretacao"],
                        horizonte_barras=len(estado["barras"]),
                        # O modelo precisa saber o piso ANTES de escolher o
                        # Sharpe. Sem isto ele declara um numero plausivel, a
                        # hipotese nasce nao testavel, e ele so descobre
                        # depois de ja ter proposto - que e o oposto do que a
                        # secao 8.3 pede ao mandar triar no pre-registro.
                        sharpe_minimo_milesimos=poder.sharpe_minimo_testavel(
                            duracao_barra_ms=_duracao_da_barra(
                                estado, dep.config
                            ),
                            horizonte_barras=max(1, len(estado["barras"])),
                        ),
                    ),
                ),
            ),
            schema=SCHEMA_PROPOSTA,
            schema_nome="proposta_de_regra",
            max_tokens=MAX_TOKENS_PROPOR,
            config=dep.config,
            settings=dep.settings,
            parent_event_id=_ultimo_evento(estado),
            adaptador=dep.adaptador,
        )
    except reflexao.TetoAtingido as teto:
        return _parar(estado, dep, "propor_regra", teto.veredito.motivo)
    except (ErroDoProvedor, reflexao.TierNaoConfigurado,
            ProvedorIndisponivel) as erro:
        return _parar(estado, dep, "propor_regra", f"{type(erro).__name__}: {erro}")

    eventos = [*(estado.get("eventos") or []), chamada.event_id]

    try:
        bruta = PropostaBruta.model_validate_json(chamada.texto)
        regra = montar_regra(bruta, dep.config)
    except ValidationError as erro:
        motivo = _motivo_legivel(erro)
        proposal_id = propostas.registrar_rejeitada(
            dep.conn,
            run_id=estado["run_id"],
            agent_event_id=chamada.event_id,
            resposta_crua=chamada.texto,
            motivo=motivo,
            observado_de_ms=resumo.de_ms,
            observado_ate_ms=resumo.ate_ms,
        )
        atualizado = _parar(
            {**estado, "eventos": eventos}, dep, "propor_regra", motivo
        )
        return {**atualizado, "proposal_id": proposal_id}

    return {
        "regra": regra,
        "eventos": eventos,
        # A expectativa vem do modelo e vai para o registro no proximo no,
        # ANTES de qualquer execucao (criterio 10).
        "interpretacao": estado["interpretacao"],
        "proposta_bruta": bruta,
    }


def no_registrar_intencao(estado: Estado, dep: Dependencias) -> dict:
    """Persiste a regra e a intencao declarada. Ainda sem executar nada."""
    regra: Regra = estado["regra"]
    bruta: PropostaBruta = estado["proposta_bruta"]  # type: ignore[typeddict-item]
    resumo = estado["resumo"]

    # O evento que produziu a proposta e o do no anterior. Se nao existir, o
    # grafo chegou aqui por um caminho impossivel e e melhor quebrar alto do
    # que gravar uma proposta apontando para evento nenhum.
    evento_da_proposta = _ultimo_evento(estado)
    assert evento_da_proposta is not None, "registrar_intencao sem evento pai"

    try:
        rule_id = registro_de_regra.registrar(dep.conn, regra)
        proposal_id = propostas.registrar_aceita(
            dep.conn,
            run_id=estado["run_id"],
            agent_event_id=evento_da_proposta,
            rule_id=rule_id,
            regra=regra,
            resposta_crua=bruta.model_dump_json(),
            expectativa=bruta.expectativa,
            confianca_ppm=bruta.confianca_ppm,
            observado_de_ms=resumo.de_ms,
            observado_ate_ms=resumo.ate_ms,
        )
        # O pre-registro da secao 8.2, gravado ANTES da execucao e imutavel a
        # partir daqui. Mesmo momento em que a intencao ja era declarada na
        # 0A - o que mudou e que agora ela e computavel.
        hypothesis_id, testavel = hipotese_registro.registrar(
            dep.conn,
            run_id=estado["run_id"],
            agent_event_id=evento_da_proposta,
            bruto=bruta.pre_registro,
            condicoes_validade=condicoes_da_config(dep.config).model_dump(
                mode="json"
            ),
            duracao_barra_ms=_duracao_da_barra(estado, dep.config),
            horizonte_barras=len(estado["barras"]),
            rule_id=rule_id,
        )
    except Exception as erro:  # noqa: BLE001
        return _parar(
            estado, dep, "registrar_intencao", f"{type(erro).__name__}: {erro}"
        )

    if not testavel:
        # Registrada, nao promovida, e o run continua (D33, ADR 0020). O
        # veredito dela ja esta selado como `inconclusiva`: `n_efetivo` nunca
        # alcanca um `n_minimo` maior que o horizonte inteiro.
        log.warning(
            "hipotese.nao_testavel",
            extra={
                "run_id": estado["run_id"],
                "hypothesis_id": hypothesis_id,
                "sharpe_declarado_milesimos": (
                    bruta.pre_registro.sharpe_esperado_milesimos
                ),
                "horizonte_barras": len(estado["barras"]),
            },
        )

    event_id = _evento(
        dep,
        run_id=estado["run_id"],
        node="registrar_intencao",
        kind="intencao",
        parent_event_id=_ultimo_evento(estado),
        outputs_digest=regra.hash(),
        # As DUAS metades da regra 17, no mesmo evento e antes da execucao.
        # `confidence_ppm` existia na coluna, era lido por
        # `caminho_percorrido` e nunca era escrito: o painel mostrava um campo
        # permanentemente vazio prometendo a confianca declarada. Sexta
        # ocorrencia do padrao neste projeto.
        expectation=bruta.expectativa,
        confidence_ppm=bruta.confianca_ppm,
    )
    return {
        "rule_id": rule_id,
        "proposal_id": proposal_id,
        "hypothesis_id": hypothesis_id,
        "eventos": [*(estado.get("eventos") or []), event_id],
    }


# ---------------------------------------------------------------------------
# Montagem
# ---------------------------------------------------------------------------


def _seguir(estado: Estado) -> str:
    return "fim" if estado.get("parou_em") else "segue"


def construir(dep: Dependencias):
    """Monta o grafo. LangGraph so entra aqui - e nunca nas maos rapidas."""
    from langgraph.graph import END, START, StateGraph

    grafo = StateGraph(Estado)
    grafo.add_node("observar", lambda e: no_observar(e, dep))
    grafo.add_node("interpretar", lambda e: no_interpretar(e, dep))
    grafo.add_node("propor_regra", lambda e: no_propor_regra(e, dep))
    grafo.add_node("registrar_intencao", lambda e: no_registrar_intencao(e, dep))

    grafo.add_edge(START, "observar")
    for origem, destino in (
        ("observar", "interpretar"),
        ("interpretar", "propor_regra"),
        ("propor_regra", "registrar_intencao"),
    ):
        grafo.add_conditional_edges(
            origem, _seguir, {"segue": destino, "fim": END}
        )
    grafo.add_edge("registrar_intencao", END)
    # Sem checkpointer: o estado do grafo e efemero (criterio 12).
    return grafo.compile()


def rodar(
    dep: Dependencias, *, run_id: int, barras: list[BarraCarregada]
) -> Estado:
    """Executa o grafo inteiro e devolve o estado final (que morre depois)."""
    compilado = construir(dep)
    return compilado.invoke(
        {"run_id": run_id, "barras": barras, "eventos": []}
    )


def _digest(dados: dict) -> str:
    import hashlib

    canonico = json.dumps(
        dados, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def _motivo_legivel(erro: ValidationError) -> str:
    """Motivo curto e util. E o que alguem vai ler ao diagnosticar depois."""
    partes = [
        f"{'.'.join(str(p) for p in e['loc']) or '(raiz)'}: {e['msg']}"
        for e in erro.errors()[:5]
    ]
    return "resposta fora do schema -- " + "; ".join(partes)
