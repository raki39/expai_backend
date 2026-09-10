"""O Portão A é uma COMPOSIÇÃO de cinco escopos, e nenhum deles basta sozinho.

> *"A1a certifica apenas que defeitos conhecidos não são promovidos. Sem A1b,
> uma implementação que rejeita tudo poderia passar."* — o usuário, 2026-09-09

O argumento é exato, e a 0B já tinha o número que o ilustra: o IC de Wilson
`[0,09% ; 2,78%]` sobre 200 execuções vem de **1 promoção em 200 lotes** de
nulas — *"o número que prova que ele não passou por ser surdo"*. Essa medição é
**A1b**, e não A1a. Um protocolo que recusasse tudo satisfaria
`nenhum_promovido` de graça e falharia em A1b.

## Os cinco escopos, e o que cada um responde

| escopo | a pergunta | como é certificado |
|---|---|---|
| **a1a** | defeito conhecido é barrado? | 6 controles injetados pelo caminho real, numa **cópia descartável selada**, em **8 etapas** (OP-1) |
| **a1b** | e ele não é surdo? | 400 execuções em **8 blocos de 50**, sem cópia: `calibre.rodar` é pura |
| **a2** | o simulador é honesto? | B1 perde, e perde proporcionalmente ao giro |
| **a3** | nada vazou? | purga e embargo conferidos nas três janelas |
| **a4** | o registro fecha? | ledger reconcilia, custo de IA por decisão, nenhuma tentativa some |

**O Portão só passa quando os cinco tiverem certificado para o MESMO
`alvo_de_certificacao_hash`.** Certificados de alvos diferentes não compõem: um
A1a de ontem com um A1b de hoje descreveria um laboratório que nunca existiu.

## Por que A1b não precisa de cópia — demonstrado, e não suposto

`calibre.rodar` **não importa `sqlite3`** e recebe todos os insumos por
parâmetro; `calibre.agregar` é pura sobre `list[Uma]`. Medido em
`test_a1b_parcelado`: rodar 0–9 de uma vez e rodar 0–4 e 5–9 em duas chamadas
produz execuções **idênticas** e agregação **idêntica**.

O que a independência exige é que os insumos sejam os mesmos, e três deles vêm
do banco — `base_bps`, `n_barras` e `tentativas_globais`. Por isso eles são
**congelados antes do primeiro bloco** e guardados na execução. Se
`tentativas_globais` andasse no meio, metade das execuções sairia deflacionada
por um `N` e metade por outro: a divergência [40, 41] que a 0B registrou.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

from ..a1a import catalogo as catalogo_a1a

A1A, A1B, A2, A3, A4 = "a1a", "a1b", "a2", "a3", "a4"
TODOS = (A1A, A1B, A2, A3, A4)

#: Os quatro estados de um escopo. `inaplicavel` existe para o dia em que um
#: critério deixe de fazer sentido num alvo — e não como saída de conveniência.
CERTIFICADO, PENDENTE, FALHOU, INAPLICAVEL = (
    "certificado", "pendente", "falhou", "inaplicavel",
)

#: Quantos casos cada escopo produz. Congelado, e conferido pelo gatilho do
#: manifesto: uma suíte que quebrou no meio não vira documento.
CASOS_ESPERADOS = {A1A: 6, A1B: 2, A2: 2, A3: 1, A4: 3}

#: O plano do A1a PARCELADO — OP-1, derivado da MEDIÇÃO, e não de um palpite.
#:
#: > *"Primeiro meça quanto tempo cada parte dos 207 segundos consome e derive
#: > as etapas dessa medição."* — o usuário, 2026-09-10
#:
#: Medido em 2026-09-10, cinco rodadas locais sobre 70.080 barras. Produção
#: mediu 196 a 207 s para o total que aqui deu 101 a 117 s, e a coluna da
#: direita reescala pela proporção (~1,9x):
#:
#: | unidade indivisível | local | parte | em produção |
#: |---|---|---|---|
#: | preparo: B2 + B3 + B1 x1000 | 5,3 a 6,5 s (uma de 19,3 s: o B3 fora da curva) | 5% | ~11 s |
#: | preparo: B4, 16 hipóteses | 26,9 a 28,9 s; **14,3 s** com `temp_store` | 26% | ~53 s; **~27 s** |
#: | a1a: duplicação disfarçada | 2,2 a 4,5 s | 3% | ~6 s |
#: | a1a: lucro só sem custos | 65,0 a 67,0 s; 61,9 s com `temp_store` | 64% | **~118 s** |
#: | a1a: os quatro estruturais | < 0,1 s cada | 0% | — |
#:
#: A unidade é o que não se parte sem partir um resultado ao meio: a comparação
#: de baselines, o braço B4 e cada família. O plano é UMA etapa por unidade, na
#: ordem em que o caminho de uma vez as roda — e o catálogo gera as seis
#: últimas, para que uma família nova ganhe etapa sem ninguém lembrar.
#:
#: **O limite, medido e declarado.** `lucro_so_sem_custos` é 94% `executor
#: .rodar` sobre 1.366 idas e voltas, e 72% `saldo_da_conta`: o saldo é sempre
#: DERIVADO dos lançamentos (regra 16), então cada compra agrega o run inteiro
#: e o run custa O(n²). Índice de cobertura comprou 1,2x; `mmap` e `cache`,
#: nada; `temp_store`, 1,08x aqui. A etapa fica ISOLADA, e não mais curta — e
#: continua acima dos 60 s do proxy do painel. O que o desenho garante é que
#: isso não perde nada: a etapa conclui no backend, a trava a marca em
#: andamento, e o pedido repetido não roda coisa alguma.
PLANO_A1A: tuple[str, ...] = ("preparo.baselines", "preparo.b4") + tuple(
    f"a1a.{familia.chave}" for familia in catalogo_a1a.FAMILIAS
)

#: Os escopos parcelados, e em quantos pedaços. A1b: 8 blocos de 50 = 400, que
#: é o que a D29 fixou. A1a: uma etapa por unidade do plano acima.
BLOCOS = {A1A: len(PLANO_A1A), A1B: 8}
POR_BLOCO = 50

PERGUNTA = {
    A1A: "um defeito conhecido e barrado?",
    A1B: "e o protocolo nao e surdo? (1 promocao em 200 lotes de nulas)",
    A2: "o simulador e honesto? B1 perde, e perde proporcionalmente ao giro",
    A3: "nada vazou entre treino e teste?",
    A4: "o registro fecha? ledger, custo de IA e nenhuma tentativa perdida",
}


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# A1a: o mecanismo ESPERADO ao lado do OBSERVADO
# ---------------------------------------------------------------------------


def classificar_bloqueio(controle) -> dict:
    """Como este controle foi contido — e `promovido=False` não basta.

    > *"Garanta ainda que o A1a não passa apenas por `promovido=False`: cada
    > controle deve publicar o mecanismo esperado de bloqueio e o observado. A
    > duplicação disfarçada, por exemplo, não foi barrada estruturalmente, mas
    > deixou de ser promovida estatisticamente — essa diferença precisa
    > continuar visível."*

    E ela é real: na certificação da `cv9`, `duplicacao_disfarcada` saiu
    `barrado=False` e `promovido=False`. As duas coisas juntas significam que a
    injeção **atravessou** as guardas estruturais e foi contida pela
    estatística — informação diferente de ter sido recusada na porta, e que um
    `promovido: false` sozinho apagaria.
    """
    from ..a1a import catalogo

    familia = catalogo.POR_CHAVE.get(controle.chave)
    mecanismos = [
        t.get("mecanismo")
        for t in controle.tentativas
        if t.get("barrada") and t.get("mecanismo")
    ]
    if controle.barrado:
        como = "estrutural"
        resumo = (
            "recusado na porta por CHECK, gatilho ou fronteira - a injecao nao"
            " chegou a produzir resultado"
        )
    elif not controle.promovido:
        como = "estatistico"
        resumo = (
            "ATRAVESSOU as guardas estruturais e foi contido pela estatistica:"
            f" veredito {controle.veredito!r}, sem promocao. E informacao"
            " diferente de ter sido barrado, e o campo existe para nao"
            " confundir as duas"
        )
    else:
        como = "nenhum"
        resumo = (
            "NAO foi contido: o controle foi promovido, e §14.4 diz que uma"
            " unica promocao reprova a fase"
        )
    return {
        "como_foi_contido": como,
        "resumo": resumo,
        "mecanismo_esperado": familia.guarda_esperada if familia else None,
        "mecanismo_observado": mecanismos or None,
        "barrado": controle.barrado,
        "promovido": controle.promovido,
        "veredito": controle.veredito,
        "contido": (not controle.promovido),
    }


# ---------------------------------------------------------------------------
# A1b: os oito blocos
# ---------------------------------------------------------------------------


def congelar_insumos(
    conn: sqlite3.Connection, *, dataset_id: int, config, config_version_id: int
) -> dict:
    """Os insumos do A1b, lidos UMA vez e guardados. Antes do primeiro bloco.

    `base_bps` vai inteiro: ele é a série do in-sample, e guardar só um hash
    dela obrigaria a reler o banco a cada bloco — que é justamente o que o
    congelamento existe para evitar.
    """
    from ..a1b import braco as a1b_braco

    ins = a1b_braco.insumos(conn, dataset_id)
    return {
        "base_bps": ins.base_bps,
        "n_barras": ins.n_barras,
        "duracao_barra_ms": ins.duracao_barra_ms,
        "tentativas_globais": ins.tentativas_globais,
        "semente": config.default_seed,
        "lote": config.a1b_lote,
        "por_bloco": POR_BLOCO,
        "blocos": BLOCOS[A1B],
        "congelado_em": _agora(),
        "por_que_congelado": (
            "`tentativas_globais` e o `N` do DSR: se ele andar entre blocos,"
            " metade das execucoes sai deflacionada por um numero e metade por"
            " outro - a divergencia [40, 41] que a 0B registrou. `base_bps` e"
            " `n_barras` definem o experimento"
        ),
    }


def indices_do_bloco(indice_bloco: int) -> dict[str, list[int]]:
    """Quais execuções cada bloco roda. **Determinístico e congelado.**

    Os 8 blocos cobrem os dois desenhos de 200 execuções cada: os blocos 0–3
    fazem a nula global e os 4–7 a nula com sinal. Fatia fixa por índice, então
    o bloco 5 roda os mesmos índices hoje e daqui a um ano.
    """
    from ..a1b import calibre

    metade = BLOCOS[A1B] // 2
    desenho = calibre.NULA_GLOBAL if indice_bloco < metade else calibre.COM_SINAL
    posicao = indice_bloco % metade
    inicio = posicao * POR_BLOCO
    return {desenho: list(range(inicio, inicio + POR_BLOCO))}


def rodar_bloco(congelado: dict, indice_bloco: int) -> tuple[list, int]:
    """Um bloco. **Sem banco nenhum** — `calibre.rodar` é pura.

    Devolve `(execucoes, micros)`. As execuções são `Uma`, e é o conteúdo delas
    que fica gravado: `calibre.agregar` é pura sobre a lista, então recarregar
    e agregar reproduz exatamente o número de rodar tudo de uma vez.
    """
    from ..a1b import calibre
    from ..config.schema import ExperimentConfig

    inicio = time.perf_counter_ns()
    feitas, _mags, _cpu = calibre.rodar(
        base_bps=congelado["base_bps"],
        config=ExperimentConfig(**congelado["config"])
        if "config" in congelado
        else ExperimentConfig(),
        duracao_barra_ms=congelado["duracao_barra_ms"],
        n_barras=congelado["n_barras"],
        tentativas_globais=congelado["tentativas_globais"],
        indices=indices_do_bloco(indice_bloco),
        semente=congelado["semente"],
    )
    return feitas, (time.perf_counter_ns() - inicio) // 1_000


def execucoes_gravadas(conn: sqlite3.Connection, execucao_id: int) -> list:
    """Recompõe as `Uma` de todos os blocos concluídos. Do CONTEÚDO."""
    from ..a1b.calibre import Uma

    fora = []
    for linha in conn.execute(
        "SELECT conteudo_json FROM certificacao_bloco"
        " WHERE execucao_id = ? AND estado = 'concluido'"
        " ORDER BY indice_bloco",
        (execucao_id,),
    ):
        for bruto in json.loads(linha["conteudo_json"]):
            fora.append(Uma(**bruto))
    return fora


def blocos_faltando(conn: sqlite3.Connection, execucao_id: int) -> list[int]:
    """Os índices que ainda não têm bloco CONCLUÍDO.

    Um bloco que falhou permanece na tabela e continua faltando aqui — ele não
    é apagado, e o `UNIQUE` impede que o retry o sobrescreva. Retomar exige uma
    execução nova, e o registro guarda a falha.
    """
    feitos = {
        int(l["indice_bloco"])
        for l in conn.execute(
            "SELECT indice_bloco FROM certificacao_bloco"
            " WHERE execucao_id = ? AND estado = 'concluido'",
            (execucao_id,),
        )
    }
    return [i for i in range(BLOCOS[A1B]) if i not in feitos]


# ---------------------------------------------------------------------------
# A COMPOSIÇÃO
# ---------------------------------------------------------------------------


def composicao(conn: sqlite3.Connection, alvo_hash: str) -> dict:
    """O estado de cada escopo para ESTE alvo, e se o Portão passa.

    Derivada, e não gravada: o estado é função dos manifestos selados, que são
    imutáveis. Guardar uma cópia criaria a segunda fonte de verdade que a
    regra 16 proíbe.
    """
    por_escopo = {}
    for escopo in TODOS:
        linha = conn.execute(
            "SELECT m.passa, m.selado_em, m.execucao_id"
            "  FROM certificacao_manifesto m"
            "  JOIN certificacao_execucao e ON e.id = m.execucao_id"
            " WHERE m.alvo_hash = ? AND e.escopo = ?"
            " ORDER BY m.execucao_id DESC LIMIT 1",
            (alvo_hash, escopo),
        ).fetchone()
        if linha is None:
            estado, detalhe = PENDENTE, "nenhum manifesto selado para este alvo"
        elif linha["passa"]:
            estado, detalhe = CERTIFICADO, f"selado em {linha['selado_em']}"
        else:
            estado, detalhe = FALHOU, f"manifesto selado com passa=0"
        por_escopo[escopo] = {
            "estado": estado,
            "pergunta": PERGUNTA[escopo],
            "detalhe": detalhe,
            "execucao_id": int(linha["execucao_id"]) if linha else None,
            "casos_esperados": CASOS_ESPERADOS[escopo],
            "blocos": BLOCOS.get(escopo, 0),
        }

    certificados = [e for e, v in por_escopo.items() if v["estado"] == CERTIFICADO]
    falhou = [e for e, v in por_escopo.items() if v["estado"] == FALHOU]
    pendentes = [e for e, v in por_escopo.items() if v["estado"] == PENDENTE]
    return {
        "alvo_de_certificacao_hash": alvo_hash,
        "escopos": por_escopo,
        "certificados": sorted(certificados),
        "falhou": sorted(falhou),
        "pendentes": sorted(pendentes),
        "passa": len(certificados) == len(TODOS),
        "por_que": (
            "os cinco escopos tem certificado para este alvo"
            if len(certificados) == len(TODOS)
            else (
                f"faltam {len(pendentes)} escopo(s) pendente(s)"
                f"{' e ' + str(len(falhou)) + ' falhou(aram)' if falhou else ''}:"
                f" {', '.join(sorted(pendentes + falhou))}"
            )
        ),
        "por_que_nao_basta_o_a1a": (
            "A1a certifica que defeito CONHECIDO nao e promovido, e um"
            " protocolo que recusasse tudo satisfaria isso de graca. Quem mede"
            " que ele nao e surdo e A1b - foi ele que produziu, na 0B, o IC de"
            " Wilson [0,09%; 2,78%] a partir de 1 promocao em 200 lotes de"
            " nulas"
        ),
        "certificados_de_alvos_diferentes_nao_compoem": (
            "um A1a de ontem com um A1b de hoje descreveria um laboratorio que"
            " nunca existiu: a composicao e sempre sobre UM alvo"
        ),
    }
