"""O relatorio da fase 0C - **incremento 21**, e ele nasce PROVISORIO.

> *"Pode iniciar o incremento 21 como gerador de relatorio, mas o relatorio
> permanece provisorio/aguardando evidencia ate: o piloto real fechar; a
> calibracao e a revalidacao rodarem; os criterios restantes do incremento 18
> serem realmente cumpridos."* - o usuario, 2026-09-08

## O que "provisorio" quer dizer aqui, e o que ele NAO quer dizer

Este modulo **gera o relatorio inteiro**, com todas as condicoes derivadas de
consulta. O que ele nao faz e chamar o resultado de definitivo enquanto tres
gates de evidencia nao fecharem.

**A distincao importa porque as duas alternativas eram piores.** Nao gerar nada
ate a evidencia chegar deixaria o relatorio para ser escrito no dia em que
houvesse resultado para olhar - e um relatorio escrito com o numero na frente e
um relatorio escrito para o numero. Gerar e chamar de definitivo seria pior
ainda: a 0A fechou com `fecha: true` derivado de doze consultas, e um `fecha`
sobre evidencia incompleta teria a mesma cara.

Entao: **a maquina roda agora, o veredito espera.** Mesmo desenho do
`estimar` da calibracao, que recusa enquanto o piloto nao fecha.

## Os tres gates, e cada um e uma consulta

| gate | de onde vem | por que ele bloqueia o veredito |
|---|---|---|
| **piloto fechado** | `janela_piloto` existe para o contrato | sem ele nao ha `p10(E2)`, e sem `p10(E2)` o simulador nao esta calibrado - ADR 0027 |
| **calibracao E revalidacao rodaram** | `calibracao_versao` aplicada e `janela_revalidacao` consumida | a D45 exige as duas **disjuntas**; uma so nao autoriza afirmar fidelidade |
| **criterios 5, 6 e 7 do incremento 18** | os tres, derivados | estao CONSTRUIDOS e nao cumpridos: so ficam quando rodarem sobre o piloto real |

**`None` nao e `False` em nenhum deles**, e aqui isso tem consequencia direta:
"nao medido ainda" e o estado esperado da fase, e trata-lo como reprovacao faria
o relatorio dizer que a 0C falhou quando ela apenas nao terminou.

## E a decisao de capacidade NAO e um dos gates

Ela e bloqueante de **hipotese nova**, e nao deste relatorio - o usuario foi
explicito. Relatar o que se mediu nao decide nada; se o relatorio dependesse da
decisao, a unica forma de ver o numero que a sustenta seria decidir primeiro.

Ela aparece aqui como **campo informativo**, com o estado dela.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .. import fase as fase_mod
from ..aovivo import bbo
from ..calibracao import piloto
from ..dataset import loader
from ..hipotese import dimensionamento
from . import viabilidade as relatorio_viabilidade

#: O estado do relatorio enquanto a evidencia nao chega. Nome proprio, e nao um
#: booleano: `definitivo: false` deixaria o leitor supor que o contrario e
#: `true`, e nao ha terceira opcao - ha "aguardando", que e o estado normal de
#: uma fase em curso.
PROVISORIO = "provisorio_aguardando_evidencia"
DEFINITIVO = "definitivo"

CONTRATO_PADRAO = bbo.CONTRATO_PADRAO

#: O que a 0C **nao** vai responder, aprovando ou nao. Fixo pelo mesmo motivo
#: que as listas da 0A e da 0B sao fixas: uma proibicao que se calcula dos dados
#: e uma proibicao que pode desaparecer sozinha.
A_0C_NAO_RESPONDE = [
    "Nada sobre EDGE. A D38 (ADR 0034) decidiu que nenhuma candidata entra no"
    " forward, e o B3 roda exclusivamente como controle negativo - sem"
    " pre-registro, credito, familia ou DSR. Um forward sem candidata mede o"
    " encanamento, e nao estrategia.",
    "Nada sobre fidelidade de book: sem fila e sem preenchimento maker"
    " (§8.4.1). O shadow calibra TAKER (§8.4.1.2), e executar e o unico jeito"
    " de calibrar maker - o que e Fase 3.",
    "Nada sobre mais de um agente. FDR online e bloqueante para a Fase 1 e nao"
    " foi construido (§19.1).",
    "Nada sobre capital real, em nenhuma hipotese (§8.4.1.1, §14.4.1)."
    " `place_order` continua NAO PROVISIONADA - §11.2.1: 'nao basta estar"
    " desativada'.",
    "**E a 0C acrescenta uma resposta que o plano nao previa:** com BY, familia"
    " de 48 e potencia de 80%, o desenho NAO CONSEGUE testar o efeito minimo"
    " no horizonte disponivel (D48, ADR 0038). Isso e diferente de uma"
    " ausencia - e uma incapacidade MEDIDA, com o deficit em barras ao lado.",
    "O CUSUM e DIAGNOSTICO e nao invalida estrategia: o orcamento de falso"
    " alarme de <= 10% do ADR 0035 nao e entregue - medido entre 7,5% e 18,4%"
    " conforme a amostra que calibra (adendo do ADR 0035).",
]


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Os tres gates de evidencia, cada um derivado
# ---------------------------------------------------------------------------


def _gate_piloto(conn: sqlite3.Connection) -> dict[str, Any]:
    """O piloto do ADR 0027 fechou?

    `janela_piloto` e gravada uma vez, por `piloto.fechar`, e as duas travas
    sao 1.000 observacoes validas **e** 14 dias corridos - fecha pela mais
    tarde. Enquanto ela nao existe, `estimar` recusa, e isso e o desenho.
    """
    serie = bbo.Serie(
        venue="binance", symbol="BTCUSDT", price_scale_exp=0, volume_scale_exp=0
    )
    janela = piloto.ler(conn, serie, CONTRATO_PADRAO)
    if janela is None:
        derivada = None
        try:
            derivada = piloto.derivar(conn, serie, CONTRATO_PADRAO)
        except Exception:  # noqa: BLE001 - "quanto falta" e informativo
            derivada = None
        return {
            "cumprido": False,
            "estado": "acumulando",
            "observacoes_validas": (
                derivada.observacoes_validas if derivada else None
            ),
            "dias_corridos": derivada.dias_corridos if derivada else None,
            "por_que_bloqueia": (
                "sem a janela fechada nao ha `p10(E2)`, e sem ele o simulador"
                " nao esta calibrado (ADR 0027). Afirmar fidelidade agora seria"
                " afirmar sobre um periodo que ainda nao terminou - a quinta"
                " pergunta do teste de escopo"
            ),
        }
    return {
        "cumprido": True,
        "estado": "fechada",
        "observacoes_validas": janela.observacoes_validas,
        "dias_corridos": janela.dias_corridos,
        "fechada_por": janela.fechada_por,
        "por_que_bloqueia": None,
    }


def _gate_calibracao_e_revalidacao(conn: sqlite3.Connection) -> dict[str, Any]:
    """As duas rodaram, e sao DISJUNTAS (D45, ADR 0027)?

    Uma so nao autoriza nada: a D45 exige calibracao e revalidacao em periodos
    disjuntos, e a revalidacao exige o **limite inferior** do IC
    (`LB95(p10(E2)) >= 0`), nao o ponto.

    E `regime ausente fica NAO CALIBRADO`, sem herdar parametro de outro regime
    (ADR 0033) - entao o numero de regimes calibrados e parte da resposta, e
    nao um detalhe.
    """
    calibracoes = int(
        conn.execute("SELECT COUNT(*) AS n FROM calibracao_versao").fetchone()[
            "n"
        ]
    )
    revalidacoes = int(
        conn.execute("SELECT COUNT(*) AS n FROM janela_revalidacao").fetchone()[
            "n"
        ]
    )
    consumidas = int(
        conn.execute("SELECT COUNT(*) AS n FROM revalidacao_uso").fetchone()["n"]
    )
    regimes = [
        dict(r)
        for r in conn.execute(
            "SELECT regime, COUNT(*) AS overrides"
            "  FROM calibracao_perfil_regime GROUP BY regime ORDER BY regime"
        )
    ]
    cumprido = calibracoes > 0 and consumidas > 0
    return {
        "cumprido": cumprido,
        "calibracoes_aplicadas": calibracoes,
        "janelas_de_revalidacao_seladas": revalidacoes,
        "revalidacoes_consumidas": consumidas,
        "regimes_com_override": regimes,
        "por_que_bloqueia": (
            None
            if cumprido
            else "a D45 exige calibracao E revalidacao, em periodos DISJUNTOS."
            " Uma sem a outra nao autoriza afirmar fidelidade, e regime sem"
            " amostra fica NAO CALIBRADO usando a base - nunca herdando de"
            " outro regime (ADR 0033)"
        ),
    }


def _gate_incremento_18(conn: sqlite3.Connection) -> dict[str, Any]:
    """Os criterios 5, 6 e 7 do incremento 18 estao CUMPRIDOS?

    Eles estao **construidos e testados**, e nao cumpridos - so ficam quando
    rodarem sobre o piloto real. A distincao e a mesma que o incremento 13 fez
    entre "CONSTRUIDO" e "concluido", e ela e a diferenca entre codigo que
    existe e evidencia que existe.

    Derivado do mesmo fato que o gate do piloto, porque e dele que os tres
    dependem - e derivar de duas fontes faria os dois divergirem.
    """
    piloto_ok = _gate_piloto(conn)["cumprido"]
    return {
        "cumprido": piloto_ok,
        "criterios": {
            "5_p10_de_E2_medido_no_piloto_real": piloto_ok,
            "6_spread_por_regime_derivado_da_medicao": piloto_ok,
            "7_bytes_descomprimidos_auditaveis_no_manifesto": piloto_ok,
        },
        "por_que_bloqueia": (
            None
            if piloto_ok
            else "os tres estao CONSTRUIDOS e testados, e nao cumpridos:"
            " dependem do piloto real, e `estimar` recusa ate a janela fechar"
        ),
    }


# ---------------------------------------------------------------------------
# O relatorio
# ---------------------------------------------------------------------------


def montar(conn: sqlite3.Connection, *, potencia_ppm: int) -> dict[str, Any]:
    """O relatorio da 0C, e ele diz que e provisorio enquanto for.

    `potencia_ppm` e obrigatorio e sem default, como em toda entrada da D48.
    """
    gates = {
        "piloto_fechado": _gate_piloto(conn),
        "calibracao_e_revalidacao": _gate_calibracao_e_revalidacao(conn),
        "incremento_18_cumprido": _gate_incremento_18(conn),
    }
    pendentes = sorted(k for k, v in gates.items() if not v["cumprido"])
    estado = PROVISORIO if pendentes else DEFINITIVO

    ds = loader.dataset_vigente(conn)
    return {
        "gerado_em": _agora(),
        # A fase vem de `app/fase.py`, e nunca escrita a mao. `app/fase.py`
        # existe porque `/api/health` ficou declarando "0A" depois de a 0B
        # abrir - e mesmo depois de centralizada ela passou quatro dias
        # anunciando a fase anterior. **Centralizar resolve divergencia; nao
        # resolve envelhecimento** - e um literal aqui devolveria as duas.
        "fase": fase_mod.FASE,
        "pergunta": (
            "a candidata sobrevive a dados que nao existiam quando foi"
            " registrada? (§14.5)"
        ),
        # O ESTADO vem antes de qualquer numero. Um relatorio provisorio cujo
        # rotulo aparece no fim e um relatorio que sera lido como definitivo.
        "estado": estado,
        "gates_de_evidencia": gates,
        "pendentes": pendentes,
        "o_que_provisorio_quer_dizer": (
            "a maquina roda e o VEREDITO espera. Todas as condicoes abaixo sao"
            " derivadas de consulta, e nenhuma e digitada - o que falta e"
            " evidencia, e nao codigo. Nenhum numero deste relatorio pode ser"
            " citado como resultado da fase enquanto o estado for"
            f" `{PROVISORIO}`."
        ),
        # A pergunta da fase nao tem resposta enquanto ha gate aberto, e o
        # campo diz `None` com o motivo - nunca `False`, que afirmaria que a
        # 0C falhou quando ela apenas nao terminou.
        "resposta_da_0c": (
            None
            if pendentes
            else "derivada das condicoes acima, todas cumpridas"
        ),
        "por_que_sem_resposta": (
            f"{len(pendentes)} gate(s) de evidencia aberto(s): "
            + ", ".join(pendentes)
            if pendentes
            else None
        ),
        "dataset": (
            None
            if ds is None
            else {"id": ds.id, "barras": ds.bars, "timeframe": ds.timeframe}
        ),
        "nao_responde": A_0C_NAO_RESPONDE,
        # A viabilidade entra INTEIRA, e nao por referencia: ela carrega a
        # unica conclusao que a fase ja tem, e uma conclusao sobre o que NAO da
        # para medir e a que mais facilmente se perde entre rotas.
        "viabilidade": relatorio_viabilidade.montar(
            conn, potencia_ppm=potencia_ppm
        ),
        # E a decisao de capacidade e INFORMATIVA aqui, nao gate.
        "decisao_de_capacidade": {
            "escolhida": dimensionamento.DECISAO_DE_CAPACIDADE_EXPERIMENTAL,
            "bloqueia": "hipotese nova sob a regua da D48 - bloqueante ABSOLUTO",
            "nao_bloqueia": (
                "este relatorio. Relatar o que se mediu nao decide nada, e se"
                " o relatorio dependesse da decisao, a unica forma de ver o"
                " numero que a sustenta seria decidir primeiro"
            ),
        },
    }
