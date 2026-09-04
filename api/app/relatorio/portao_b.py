"""O Portão B: **existe candidata digna de auditoria?** (§14.4, incremento 14).

> "O Portão B **não aprova um edge**. Ele avalia apenas evidência
> retrospectiva, em fidelidade 1–2, e por isso decide somente se existe uma
> estratégia candidata que mereça seguir para auditoria (14.4.1) e forward
> (8.5)." — §14.4

## Ele não é calculado se o A não passou

R49 é literal, e aqui é **imposto**, não convencionado: sem o Portão A
aprovado integralmente, este módulo não produz número nenhum — nem parcial,
nem "só para ver". Calcular antes seria produzir exatamente o número que a fase
existe para não produzir cedo demais.

## Quem é candidata

As hipóteses **do agente** na família corrente. Não são "as que sobreviveram ao
lote": o critério 6 de §14.4 é o próprio DSR, e pré-filtrar por ele tornaria o
critério tautológico — a lista de §14.4 existe para decidir, não para
confirmar uma decisão já tomada.

## A ordem dos critérios é deliberada

Os baratos primeiro. Se um deles reprova de forma **definitiva**, o
walk-forward não roda — e o critério 5 fica `None` com o motivo escrito, em vez
de `False`. `None` ali diria "a amostra não alcançou"; a verdade é "não foi
medido porque outro critério já decidiu", e são coisas diferentes.

## Três resultados, e `inconclusivo` não é sucesso

R51, e §14.4 escreve a consequência: *"nem promove nem descarta. A hipótese
permanece em observação e **não pode ser citada como evidência de sucesso**"*.
Falta de amostra é inconclusivo, nunca rejeitado — tratar os dois como a mesma
coisa é "descartar abordagens por impaciência, que é o erro simétrico ao de
promover ruído".
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from ..b4 import braco as b4_braco
from ..config.schema import ExperimentConfig
from ..estatistica import dsr as dsr_mod
from ..hipotese import registro as hipotese_registro
from ..maos_rapidas import baselines
from ..simulador import execucao as simulador
from ..validador import contador, forward, promocao
from . import portao_a as portao_a_mod

log = logging.getLogger(__name__)

PASSOU = "passou"
REJEITADO = "rejeitado"
INCONCLUSIVO = "inconclusivo"

#: Fixo, e não derivado: é o que a Fase 0 **não** responde mesmo aprovando, e
#: §14.4.1 escreve com todas as letras. Derivar dos dados permitiria que a
#: lista encolhesse sozinha.
O_QUE_APROVAR_NAO_SIGNIFICA = [
    "Passar no Portao B e tratado como SUSPEITA DE DEFEITO ate prova em"
    " contrario (§14.4.1): em fidelidade 1-2, com um agente e so evidencia"
    " retrospectiva, a probabilidade de um bug produzir o sinal e maior que a"
    " de haver edge real.",
    "Nao e evidencia de que a estrategia sobrevive a custos de execucao reais."
    " Em fidelidade 1-2, passar elimina estrategias obviamente ruins, e nada"
    " alem disso.",
    "Nenhuma aprovacao de Fase 0 autoriza capital real, em nenhuma hipotese"
    " (§8.4.1.1, §14.4.1). Continua nao existindo `place_order`.",
    "Uma candidata retrospectiva e o INSUMO da 0C, e nao um resultado: a"
    " conclusao sobre edge pertence a ela.",
]


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _candidatas(
    conn: sqlite3.Connection, config_version_id: int
) -> list[dict]:
    """As hipóteses do agente nesta família, com o run de cada uma."""
    return [
        dict(l)
        for l in conn.execute(
            "SELECT h.id AS hypothesis_id, h.run_id AS run_id,"
            "       h.testavel AS testavel, h.metrica_primaria AS metrica,"
            "       h.efeito_minimo AS efeito_minimo, h.n_minimo AS n_minimo,"
            "       h.content_hash AS content_hash"
            "  FROM hypothesis h JOIN run r ON r.id = h.run_id"
            " WHERE h.agente_origem = ? AND r.config_version_id = ?"
            " ORDER BY h.id",
            (hipotese_registro.AGENTE_ORIGEM, config_version_id),
        )
    ]


def _por_credito(conn: sqlite3.Connection, config_version_id: int) -> dict:
    """Sobreviventes por crédito consumido, nos dois braços (§14.3, critério 4).

    É comparação de **braço**, e não de candidata: §14.4 pergunta se o agente
    "supera B4 em hipóteses sobreviventes por crédito de teste consumido", e
    isso é uma razão entre dois totais. O mesmo número vale para toda candidata
    do lote.
    """
    from .. import creditos as creditos_mod

    saida = {}
    for braco in ("agente", "b4"):
        saldo = creditos_mod.saldo(
            conn, braco=braco, config_version_id=config_version_id
        )
        saida[braco] = {
            "creditos_consumidos": saldo.consumido if saldo else 0,
            "orcamento": saldo.orcamento if saldo else 0,
        }
    resumo_b4 = b4_braco.resumo(conn, config_version_id)
    saida["b4"]["sustentadas"] = resumo_b4["sustentadas"]

    sustentadas_agente = sum(
        1
        for c in _candidatas(conn, config_version_id)
        if (promocao.parecer_derivado(conn, int(c["hypothesis_id"])) or {}).get(
            "veredito"
        )
        == "sustentada"
    )
    saida["agente"]["sustentadas"] = sustentadas_agente

    for braco in ("agente", "b4"):
        consumido = saida[braco]["creditos_consumidos"]
        saida[braco]["por_credito_ppm"] = (
            saida[braco]["sustentadas"] * 1_000_000 // consumido
            if consumido
            else None
        )

    a, b = saida["agente"]["por_credito_ppm"], saida["b4"]["por_credito_ppm"]
    saida["agente_supera_b4"] = None if a is None or b is None else a > b
    saida["por_que_sem_comparacao"] = (
        None
        if a is not None and b is not None
        else (
            "um dos bracos nao consumiu credito nesta config_version; sem"
            " denominador nao ha taxa, e devolver zero afirmaria que ela foi"
            " medida. §14.3 exige que os dois tenham rodado sob a MESMA"
            " config_version"
        )
    )
    return saida


def _uma_candidata(
    conn: sqlite3.Connection,
    candidata: dict,
    *,
    dataset_id: int | None,
    config: ExperimentConfig,
    config_version_id: int,
    por_credito: dict,
    rodar_forward: bool,
) -> dict:
    """Os seis critérios de §14.4 para uma candidata."""
    hid = int(candidata["hypothesis_id"])
    run_id = int(candidata["run_id"])
    patrimonio = simulador.caixa_cents(conn, run_id)
    semente = config.seed_capital_usd_cents

    # 1. Liquido positivo DEPOIS DE TODOS OS CUSTOS, custo de IA incluido.
    #    `caixa_cents` sai do ledger, e a reflexao e lancada no livro simulado
    #    (D21) - entao o custo do proprio pensamento ja esta dentro. Foi
    #    exatamente a ausencia dele que fez o numero-heroi do painel divergir
    #    da tabela em nove centavos.
    c1 = patrimonio > semente

    # 2 e 3. Contra os baselines, recalculados do banco.
    parecer = promocao.parecer_derivado(conn, hid) or {}
    detalhe = parecer.get("detalhe") or {}
    b1 = baselines.b1_do_run(conn, run_id)

    # Do proprio observador, e nao de uma segunda conta: `veredito.observar` e
    # quem sabe se o baseline daquele run e comparavel (mesma config_version),
    # e ele ja devolve o motivo quando nao e. Reimplementar a subtracao aqui
    # daria um numero que ignora a fronteira de §10.2.3 em silencio.
    from ..hipotese import veredito as veredito_mod
    from ..maos_rapidas import executor as executor_mod

    obs = veredito_mod.observar(
        conn,
        run_id=run_id,
        patrimonio_cents=patrimonio,
        idas_e_voltas=executor_mod.idas_e_voltas(conn, run_id),
        b1_casado=b1,
    )
    excesso_b2 = obs.de("excesso_sobre_b2_cents")
    excesso_b3 = obs.de("excesso_sobre_b3_cents")
    c2 = (
        None
        if excesso_b2 is None or excesso_b3 is None
        else (excesso_b2 > 0 and excesso_b3 > 0)
    )
    c3 = None if b1 is None else patrimonio > int(b1["p95"])

    # 4. Por credito, e a comparacao e de braco.
    c4 = por_credito["agente_supera_b4"]

    # 6. DSR do que ficou gravado, deflacionado pelo contador GLOBAL.
    est = (detalhe.get("estatistica") or {})
    momentos = est.get("momentos") or {}
    c6: bool | None = None
    dsr_bloco: dict = {"disponivel": False, "por_que": est.get("por_que")}
    if momentos.get("n") and int(momentos["n"]) >= 2:
        try:
            dsr_bloco = dsr_mod.calcular(
                sharpe_por_observacao=(
                    momentos["sharpe_por_observacao_milionesimos"] / 1_000_000
                ),
                n=int(momentos["n"]),
                tentativas=max(1, contador.total(conn)),
                assimetria=momentos["assimetria_milesimos"] / 1_000,
                curtose_bruta=momentos["curtose_milesimos"] / 1_000,
                limiar_milesimos=config.dsr_minimo_milesimos,
            ).como_dict()
            c6 = bool(dsr_bloco.get("aprovado"))
        except dsr_mod.DSRImpossivel as erro:
            dsr_bloco = {"disponivel": False, "por_que": str(erro)}

    # 5. Walk-forward. Roda DEPOIS dos baratos, e so se nenhum deles ja
    #    reprovou de forma definitiva - o criterio 5 ficar `None` por "nao foi
    #    medido" e diferente de ficar `None` por falta de amostra.
    baratos = {"1": c1, "2": c2, "3": c3, "4": c4, "6": c6}
    ja_reprovou = any(v is False for v in baratos.values())
    c5: bool | None = None
    forward_bloco: dict = {}
    if ja_reprovou:
        forward_bloco = {
            "executado": False,
            "por_que": (
                "outro criterio ja reprovou de forma definitiva; rodar o"
                " walk-forward mudaria o custo e nao a resposta, e o criterio"
                " fica `None` por NAO TER SIDO MEDIDO - que e diferente de"
                " `None` por falta de amostra"
            ),
        }
    elif not rodar_forward or dataset_id is None:
        forward_bloco = {
            "executado": False,
            "por_que": (
                "o walk-forward nao foi solicitado nesta leitura; ele executa"
                " a regra sobre as tres janelas e por isso escreve runs, o que"
                " uma rota de leitura nao faz"
            ),
        }
    else:
        try:
            r = forward.rodar(
                conn,
                hypothesis_id=hid,
                dataset_id=dataset_id,
                config=config,
                config_version_id=config_version_id,
            )
            forward_bloco = {"executado": True, **r.como_dict()}
            if r.nao_observadas == 0:
                c5 = r.mantidas >= r.como_dict()["minimo_de_janelas"]
        except (forward.SemJanelas, forward.SemRegra) as erro:
            forward_bloco = {"executado": False, "por_que": str(erro)}

    criterios = {
        "b1_liquido_positivo_apos_todos_os_custos": c1,
        "b2_supera_b2_e_b3": c2,
        "b3_acima_do_p95_de_b1": c3,
        "b4_supera_b4_por_credito": c4,
        "b5_walk_forward_em_3_janelas": c5,
        "b6_dsr_no_minimo": c6,
    }
    reprovando = sorted(k for k, v in criterios.items() if v is False)
    sem_medida = sorted(k for k, v in criterios.items() if v is None)
    if reprovando:
        resultado = REJEITADO
    elif sem_medida:
        resultado = INCONCLUSIVO
    else:
        resultado = PASSOU

    return {
        "hypothesis_id": hid,
        "run_id": run_id,
        "content_hash": candidata["content_hash"],
        "testavel": bool(candidata["testavel"]),
        "patrimonio_final_cents": patrimonio,
        "capital_semente_cents": semente,
        "excesso_sobre_b2_cents": excesso_b2,
        "excesso_sobre_b3_cents": excesso_b3,
        "b1_casado": b1,
        "dsr": dsr_bloco,
        "walk_forward": forward_bloco,
        "criterios": criterios,
        "reprovando": reprovando,
        "sem_medida": sem_medida,
        "resultado": resultado,
        "parecer_in_sample": parecer.get("veredito"),
    }


def montar(
    conn: sqlite3.Connection,
    *,
    config_version_id: int,
    config: ExperimentConfig,
    dataset_id: int | None = None,
    rodar_forward: bool = False,
) -> dict:
    """O Portão B, ou a recusa de calculá-lo.

    `rodar_forward` é `False` por padrão porque o walk-forward **escreve**:
    ele executa a regra sobre as três janelas e abre runs. Uma rota de leitura
    que escrevesse a cada carregamento do painel produziria runs sem que
    ninguém tivesse pedido — e o registro é append-only, então eles ficariam.
    """
    a = portao_a_mod.montar(
        conn,
        config_version_id=config_version_id,
        config=config,
        dataset_id=dataset_id,
    )
    if not a["passa"]:
        # R49, IMPOSTO. Nao ha bloco parcial, nem "so para ver": o retorno nao
        # contem criterio nenhum, porque um numero exibido e um numero que
        # alguem le.
        return {
            "gerado_em": _agora(),
            "portao": "B",
            "pergunta": "existe candidata digna de auditoria?",
            "avaliado": False,
            "por_que": (
                "R49: o Portao B so e avaliado se o Portao A passar"
                " INTEGRALMENTE. O A esta em "
                + ("reprova" if a["reprova"] else "pendente")
                + " — "
                + ", ".join(a["reprovando"] + a["pendentes"])
            ),
            "portao_a": {
                "passa": a["passa"],
                "reprovando": a["reprovando"],
                "pendentes": a["pendentes"],
            },
            "o_que_aprovar_nao_significa": O_QUE_APROVAR_NAO_SIGNIFICA,
        }

    por_credito = _por_credito(conn, config_version_id)
    candidatas = [
        _uma_candidata(
            conn, c,
            dataset_id=dataset_id,
            config=config,
            config_version_id=config_version_id,
            por_credito=por_credito,
            rodar_forward=rodar_forward,
        )
        for c in _candidatas(conn, config_version_id)
    ]
    passaram = [c for c in candidatas if c["resultado"] == PASSOU]
    inconclusivas = [c for c in candidatas if c["resultado"] == INCONCLUSIVO]

    return {
        "gerado_em": _agora(),
        "portao": "B",
        "pergunta": "existe candidata digna de auditoria?",
        "avaliado": True,
        "config_version_id": config_version_id,
        "candidatas": candidatas,
        "quantas": len(candidatas),
        "passaram": [c["hypothesis_id"] for c in passaram],
        "inconclusivas": [c["hypothesis_id"] for c in inconclusivas],
        "por_credito": por_credito,
        # A resposta do portao. Sem candidata nenhuma nao ha o que julgar - e
        # isso NAO e um quarto resultado: a R51 fala do desfecho de uma
        # candidata avaliada, e "nao ha candidata" e o estado anterior a isso.
        "ha_candidata_digna_de_auditoria": bool(passaram),
        "sem_candidata": not candidatas,
        "por_que_sem_candidata": (
            None if candidatas else
            "nenhuma hipotese do agente nesta config_version; o Portao B"
            " pergunta se existe candidata, e a resposta e nao por ausencia, e"
            " nao por reprovacao"
        ),
        "auditoria": (
            "Passar no Portao B DISPARA AUDITORIA, e nao comemoracao"
            " (§14.4.1). O roteiro esta em `/api/relatorio/auditoria`."
            if passaram
            else None
        ),
        "o_que_aprovar_nao_significa": O_QUE_APROVAR_NAO_SIGNIFICA,
    }
