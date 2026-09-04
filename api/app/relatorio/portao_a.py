"""O relatório do Portão A: **o protocolo funciona?** (§14.4, incremento 13).

> "Reprovar no Portão A **não é resultado ruim; é o resultado mais informativo
> possível a esse custo**, porque significa que o mecanismo central do projeto
> ainda não existe. A resposta é consertar o protocolo e repetir, não seguir
> para o Portão B." — §14.4

Mesma forma do relatório da 0A, e pelo mesmo motivo: **cada condição é um
booleano que sai de uma consulta**, nunca uma frase. Um relatório de portão
escrito à mão diria "o protocolo funciona" com a mesma confiança tivesse ele
funcionado ou não.

## Três resultados, e não dois

O relatório da 0A tratava `None` como neutro — "não se aplica a este run" — e
`fecha` era a conjunção dos que não eram falsos. Aqui **não pode**: o Portão A
é "obrigatório, eliminatório", e um critério que ninguém mediu não é um
critério satisfeito.

| Resultado | Condição |
|---|---|
| `passa` | nenhuma condição falsa **e** nenhuma pendente |
| `reprova` | alguma condição falsa |
| `pendente` | nenhuma falsa, mas alguma ainda não foi medida |

`None` continua sendo diferente de `False` — só deixa de ser diferente de
"aprovado".

## O que este relatório NÃO faz

Não avalia o Portão B. R49 é literal: *"Portão B só avaliado se o Portão A
passar integralmente"*. Enquanto A não passa, calcular B seria produzir o
número que a fase existe para não produzir cedo demais.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ..a1a import braco as a1a_braco
from ..a1a import catalogo as a1a_catalogo
from ..a1b import braco as a1b_braco
from ..a1b import calibre as a1b_calibre
from ..config.schema import ExperimentConfig
from ..dataset import janelas
from ..hipotese import registro as hipotese_registro
from ..ledger import livro
from ..maos_rapidas import baselines
from ..validador import contador, estados


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# A1a — controles determinísticos, tolerância zero
# ---------------------------------------------------------------------------


def _a1a(conn: sqlite3.Connection, config_version_id: int) -> dict:
    resumo = a1a_braco.resumo(conn, config_version_id)
    # Do `node` do evento, e nao do texto do pre-registro. A primeira versao
    # lia a familia do enunciado e falhava exatamente no controle de
    # DUPLICACAO - cujo enunciado e, por construcao, o texto reescrito de
    # outra hipotese. O relatorio dizia "familia faltando" sobre um controle
    # que tinha rodado, e reprovava o portao por isso: um portao que reprova
    # por defeito proprio e pior que nenhum.
    injetadas = {
        l["familia_de_defeito"]
        for l in resumo["hipoteses"]
        if l.get("familia_de_defeito")
    }
    esperadas = {f.familia_de_defeito for f in a1a_catalogo.FAMILIAS}
    return {
        **resumo,
        "familias_injetadas": sorted(injetadas),
        "familias_faltando": sorted(esperadas - injetadas),
        # A pergunta do portao. `promovidos` e LISTA, e nao contagem: §14.4
        # manda dizer QUAL controle passou, porque o nome dele e o ponteiro
        # para onde procurar o defeito.
        "nenhum_promovido": not resumo["promovidos"],
        "todas_as_familias_injetadas": not (esperadas - injetadas),
    }


# ---------------------------------------------------------------------------
# A2 — sanidade do simulador: B1 negativo e proporcional ao giro
# ---------------------------------------------------------------------------


def _a2(
    conn: sqlite3.Connection, config_version_id: int, config: ExperimentConfig
) -> dict:
    """B1 perde, e perde proporcionalmente ao número de operações.

    Na 0A isto era sanidade observada (−2,49 USD por ida e volta); §14.4 o
    torna **portão**. Se operar ao acaso desse lucro, nada medido no simulador
    significaria coisa alguma.

    A proporcionalidade precisa de **dois giros diferentes** sob a mesma
    config. Com um só, ela vale `None` e o motivo fica escrito: um ponto não
    tem inclinação, e afirmar que tem seria inventar a segunda medida.
    """
    corridas = baselines.todos_os_b1(conn, config_version_id)
    semente = config.seed_capital_usd_cents
    for c in corridas:
        c["perda_cents"] = semente - c["p50"]
        c["perda_por_ida_e_volta_cents"] = (
            c["perda_cents"] / c["operacoes_alvo"] if c["operacoes_alvo"] else None
        )

    negativo = bool(corridas) and all(c["perda_cents"] > 0 for c in corridas)
    por_giro = sorted(
        {c["operacoes_alvo"]: c for c in corridas}.values(),
        key=lambda c: c["operacoes_alvo"],
    )
    proporcional: bool | None = None
    por_que: str | None = None
    if len(por_giro) < 2:
        por_que = (
            "so ha um giro de B1 sob esta config_version; um ponto nao tem"
            " inclinacao, e afirmar proporcionalidade a partir dele seria"
            " inventar a segunda medida"
        )
    else:
        # Monotonica: mais operacoes, mais perda. Nao exigimos linearidade
        # exata - o custo composto sobre um caixa que encolhe nao e linear, e
        # exigir que fosse reprovaria o simulador por estar certo.
        proporcional = all(
            b["perda_cents"] > a["perda_cents"]
            for a, b in zip(por_giro, por_giro[1:])
        )

    return {
        "corridas": corridas,
        "capital_semente_cents": semente,
        "negativo": negativo if corridas else None,
        "por_que_sem_negativo": (
            None if corridas else
            "nenhum B1 rodou sob esta config_version; sem sorteio nao ha"
            " sanidade a conferir"
        ),
        "proporcional_ao_giro": proporcional,
        "por_que_sem_proporcional": por_que,
    }


# ---------------------------------------------------------------------------
# A3 — ausência de vazamento, verificada por consulta
# ---------------------------------------------------------------------------


def _a3(
    conn: sqlite3.Connection,
    dataset_id: int | None,
    config_version_id: int,
) -> dict:
    """Nenhum acesso a dado posterior ao timestamp da decisão.

    §14.4 exige que isto seja "verificado por teste automatizado, não por
    inspeção". A suíte tem os testes; **este bloco é o outro lado**: as mesmas
    perguntas feitas ao banco de produção, sobre o que de fato ficou gravado.
    Um teste verde numa máquina não diz nada sobre as linhas que existem lá.
    """
    executou_na_decisao = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM execution"
            " WHERE execution_bar_ms <= decision_bar_ms"
        ).fetchone()["n"]
    )
    # Execucao dentro de conjunto que o agente nao pode ver. O `acesso` vem de
    # `dataset_split`, que e a fronteira do incremento 9 - a consulta pergunta
    # se alguma execucao caiu do lado do validador.
    #
    # POR config_version e POR conjunto, e nao um numero so.
    #
    # A primeira versao devolvia a contagem global, e em producao ela deu
    # 5.377 sem dizer de quem. Um numero desse tamanho sem atribuicao nao
    # permite decidir nada: ele tanto pode ser o codigo de hoje vazando quanto
    # os runs da 0A, que rodaram sobre a janela inteira porque a D22 mandava e
    # a divisao por finalidade nem existia.
    #
    # Sao perguntas diferentes com respostas diferentes, e o relatorio precisa
    # separa-las para que a segunda nao passe por vazamento e a primeira nao
    # se esconda atras dela.
    por_origem = [
        {
            "config_version_id": int(l["cv"]),
            "finalidade": l["finalidade"],
            "execucoes": int(l["n"]),
            "runs": int(l["runs"]),
        }
        for l in conn.execute(
            "SELECT r.config_version_id AS cv, s.finalidade AS finalidade,"
            "       COUNT(*) AS n, COUNT(DISTINCT e.run_id) AS runs"
            "  FROM execution e"
            "  JOIN run r ON r.id = e.run_id"
            "  JOIN dataset_split s ON s.dataset_id = e.dataset_id"
            " WHERE s.acesso = 'validador'"
            "   AND e.execution_bar_ms >= s.from_ms"
            "   AND e.execution_bar_ms <  s.to_ms_exclusive"
            " GROUP BY r.config_version_id, s.finalidade"
            " ORDER BY r.config_version_id, s.finalidade"
        )
    ]
    fora_do_conjunto = sum(x["execucoes"] for x in por_origem)
    nesta_familia = sum(
        x["execucoes"]
        for x in por_origem
        if x["config_version_id"] == config_version_id
    )

    # A PERGUNTA DE DESENHO que o numero global levanta, e que nao e a mesma
    # que o portao faz.
    #
    # O walk-forward e os 30% que a D27 recortou dos 56.064 que a 0A usou
    # INTEIROS. Entao os runs da 0A executaram sobre ele, e os resultados
    # foram lidos - B3 em US$ 151,34, o agente em US$ 620,32. §8.5.1 diz que
    # "um holdout que depende de boa vontade ja foi consumido", e a duvida e
    # se a mesma frase alcanca o walk-forward.
    #
    # O holdout NAO tem esse problema: ele e a reserva da D11, carvada na
    # ingestao e nunca tocada - e a linha `holdout_so_pelo_validador` ao lado
    # e a prova disso.
    #
    # Isto e FATO reportado, e nao criterio: transformar em portao seria eu
    # decidir sozinho o alcance de §8.5.1.
    ja_visto = [
        x for x in por_origem
        if x["finalidade"] == "walk_forward"
        and x["config_version_id"] != config_version_id
    ]
    holdout_por_outro = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM holdout_access"
            " WHERE solicitante <> 'validador'"
        ).fetchone()["n"]
    )
    sem_vazamento_nas_janelas = (
        janelas.conferir_sem_vazamento(conn, dataset_id)
        if dataset_id is not None
        else {"conferido": None, "motivo": "nenhum dataset vigente"}
    )
    conferidas = {
        "nenhuma_execucao_na_barra_da_decisao": executou_na_decisao == 0,
        # NESTA familia. As de outras config_versions aparecem em
        # `execucoes_em_conjunto_do_validador_por_origem`, com o numero e o
        # motivo - elas nao somem, mas tambem nao reprovam o lote corrente por
        # terem rodado sob um desenho em que a divisao nem existia.
        "nenhuma_execucao_em_conjunto_do_validador": nesta_familia == 0,
        "holdout_so_pelo_validador": holdout_por_outro == 0,
        "janelas_sem_sobreposicao": sem_vazamento_nas_janelas.get("conferido"),
    }
    return {
        "conferencias": conferidas,
        "execucoes_na_barra_da_decisao": executou_na_decisao,
        "execucoes_em_conjunto_do_validador": nesta_familia,
        "execucoes_em_conjunto_do_validador_global": fora_do_conjunto,
        "execucoes_em_conjunto_do_validador_por_origem": por_origem,
        "por_que_o_global_nao_reprova": (
            "os runs da 0A executaram sobre a janela inteira porque a D22"
            " mandava e a divisao por finalidade so passou a existir no"
            " incremento 9. Aplicar a fronteira de hoje a eles seria o"
            " relatorio reprovando o passado por uma regra que nao existia."
            " O que ISSO levanta - se o walk-forward continua sendo"
            " out-of-sample depois de a 0A ter rodado sobre ele - e pergunta"
            " de desenho, e esta em `walk_forward_ja_visto`"
        ),
        "walk_forward_ja_visto": {
            "execucoes": sum(x["execucoes"] for x in ja_visto),
            "runs": sum(x["runs"] for x in ja_visto),
            "por_config_version": ja_visto,
            "o_que_isso_levanta": (
                "o walk-forward sao 30% dos 56.064 que a 0A usou INTEIROS, e"
                " os resultados daqueles runs foram lidos. §8.5.1 diz que 'um"
                " holdout que depende de boa vontade ja foi consumido' - e a"
                " duvida e se a mesma frase alcanca o walk-forward. O holdout"
                " nao tem esse problema: e a reserva da D11, carvada na"
                " ingestao e nunca tocada"
            ),
            "e_fato_e_nao_criterio": (
                "reportado, e nao gateado: transformar isto em portao seria"
                " decidir sozinho o alcance de §8.5.1"
            ),
        },
        "acessos_ao_holdout_por_outro": holdout_por_outro,
        "janelas": sem_vazamento_nas_janelas,
        "sem_vazamento": (
            None
            if any(v is None for v in conferidas.values())
            else all(conferidas.values())
        ),
    }


# ---------------------------------------------------------------------------
# O IC foi definido ANTES do teste? (critério 6 do incremento 13)
# ---------------------------------------------------------------------------


def _ic_antes_do_teste(
    conn: sqlite3.Connection, config_version_id: int
) -> dict:
    """A data da config que fixou o IC é anterior à primeira execução de A1b?

    §14.4 pede "IC definido **antes** do teste", e o critério 6 do plano é
    explícito sobre a forma: *"verificável no histórico da config, não na nossa
    palavra"*. Então isto é uma comparação de datas entre duas tabelas
    append-only, e não uma afirmação nossa de que fomos honestos.

    `None` quando ainda não há execução: não há ordem a conferir entre um
    evento que aconteceu e outro que não aconteceu.
    """
    versao = conn.execute(
        "SELECT created_at FROM config_version WHERE id = ?",
        (config_version_id,),
    ).fetchone()
    primeira = conn.execute(
        "SELECT MIN(created_at) AS quando FROM a1b_execucao"
        " WHERE config_version_id = ?",
        (config_version_id,),
    ).fetchone()
    quando_config = versao["created_at"] if versao else None
    quando_a1b = primeira["quando"] if primeira else None
    if quando_config is None or quando_a1b is None:
        return {
            "config_criada_em": quando_config,
            "primeira_execucao_em": quando_a1b,
            "antes": None,
            "por_que": (
                "ainda nao ha execucao de A1b sob esta config; nao ha ordem a"
                " conferir entre um evento que aconteceu e outro que nao"
            ),
        }
    return {
        "config_criada_em": quando_config,
        "primeira_execucao_em": quando_a1b,
        "antes": quando_config < quando_a1b,
        "por_que": (
            "a config que fixou o IC e o numero de execucoes tem de ser"
            " anterior a primeira execucao: um IC escolhido depois de ver a"
            " proporcao e a regua trocada depois do resultado (§8.2)"
        ),
    }


# ---------------------------------------------------------------------------
# A4 — integridade contábil como portão
# ---------------------------------------------------------------------------


def _a4(conn: sqlite3.Connection, config_version_id: int) -> dict:
    """Ledger reconcilia, custo de IA por decisão, e nada some do registro.

    A terceira condição é a que §14.4 escreve por último e a mais fácil de
    esquecer: *"nenhuma tentativa testada some do registro"*. Ela é conferida
    contra o **contador global** do incremento 10, que §8.6 diz que nunca é
    zerado — e a conferência é nos dois sentidos, porque só um deles deixaria
    o registro acumular linhas que nenhuma tentativa produziu.
    """
    partidas = livro.conferir_partidas_dobradas(conn)
    saldos = livro.reconciliar(conn)
    vinculo = livro.conferir_vinculo_inferencia(conn)
    arredondamento = livro.conferir_arredondamento_do_custo(conn)

    # Toda hipotese que teve credito cobrado tem estado na maquina? Uma
    # tentativa paga sem linha de estado seria exatamente "sumir do registro".
    testadas_sem_estado = [
        int(l["hypothesis_id"])
        for l in conn.execute(
            "SELECT DISTINCT e.hypothesis_id AS hypothesis_id"
            "  FROM test_credit_entry e"
            " WHERE NOT EXISTS ("
            "   SELECT 1 FROM hypothesis_state s"
            "    WHERE s.hypothesis_id = e.hypothesis_id)"
        )
    ]
    # E o contador global bate com o que existe na tabela de hipoteses?
    linhas = int(
        conn.execute("SELECT COUNT(*) AS n FROM hypothesis").fetchone()["n"]
    )
    global_ = contador.total(conn)

    return {
        "partidas_dobradas_violadas": partidas,
        "saldos_divergentes": saldos,
        "vinculo_inferencia": vinculo,
        "arredondamento_do_custo_divergente": arredondamento,
        "testadas_sem_estado": testadas_sem_estado,
        "hipoteses_na_tabela": linhas,
        "contador_global": global_,
        "conferencias": {
            "ledger_reconcilia": (
                not partidas and not saldos and not arredondamento
            ),
            "custo_de_ia_por_decisao": not any(vinculo.values()),
            "nenhuma_tentativa_some": (
                not testadas_sem_estado and linhas == global_
            ),
        },
    }


# ---------------------------------------------------------------------------
# O relatório
# ---------------------------------------------------------------------------

#: Fixo, e não derivado: é o que o Portão A **não** responde, e proibição que
#: se calcula dos dados é proibição que pode desaparecer sozinha.
NAO_RESPONDE = [
    "O Portao A nao diz nada sobre o agente. Ele pergunta se o PROTOCOLO"
    " rejeita defeito (§14.4: 'primariamente um teste do validador').",
    "Passar aqui nao autoriza avaliar o Portao B por conta propria: R49 exige"
    " que A passe INTEGRALMENTE antes.",
    "Nenhuma aprovacao de Fase 0 autoriza capital real, em nenhuma hipotese"
    " (§8.4.1.1, §14.4.1).",
    "A1b exercita o pipeline estatistico, e nao o simulador nem o ledger."
    " Quem cobre esse lado e A1a.",
]


def montar(
    conn: sqlite3.Connection,
    *,
    config_version_id: int,
    config: ExperimentConfig,
    dataset_id: int | None = None,
) -> dict:
    """O relatório inteiro, com cada condição derivada de consulta."""
    a1a = _a1a(conn, config_version_id)
    a1b = a1b_braco.resumo(
        conn, config_version_id, config, dataset_id=dataset_id
    )
    a2 = _a2(conn, config_version_id, config)
    a3 = _a3(conn, dataset_id, config_version_id)
    a4 = _a4(conn, config_version_id)
    ic_antes = _ic_antes_do_teste(conn, config_version_id)

    def _do_desenho(nome: str, campo: str) -> bool | None:
        bloco = a1b["desenhos"].get(nome) or {}
        if not bloco.get("completo"):
            return None
        return bloco.get("promocao_do_lote", {}).get(campo)

    condicoes: dict[str, bool | None] = {
        # A1a - tolerancia zero. As duas: nenhuma promovida, e as seis de fato
        # injetadas. So a primeira passaria com zero controles rodados.
        "a1a_todas_as_familias_injetadas": (
            a1a["todas_as_familias_injetadas"] if a1a["quantas"] else None
        ),
        "a1a_nenhum_controle_promovido": (
            a1a["nenhum_promovido"] if a1a["quantas"] else None
        ),
        # A1b - os dois desenhos. `None` enquanto as 200 execucoes de cada um
        # nao estiverem gravadas: uma proporcao sobre 30 execucoes nao e o
        # criterio que a D29 fixou antes do teste.
        #
        # O criterio do desenho 1 e "limite superior do IC <= alvo", e nao "o
        # IC contem o alvo" - D37, ADR 0024. A redacao original da D29 e
        # aritmeticamente inalcancavel sob BY: na nula global BY rejeita com
        # probabilidade no maximo alfa / H(48) = 2,24%, entao um IC de 200
        # execucoes em torno disso nunca alcanca 10%. Ela reprovaria um BY
        # CORRETO por ele ser conservador, que e o oposto do que o criterio
        # existe para pegar - e §14.4 diz "compativel com o nivel", nao
        # "igual a ele".
        #
        # A correcao e anterior a qualquer resultado de A1b em producao, e a
        # leitura antiga continua sendo CALCULADA e reportada ao lado.
        "a1b_nula_global_no_alvo": _do_desenho(
            a1b_calibre.NULA_GLOBAL, "limite_superior_ate_o_alvo"
        ),
        "a1b_com_sinal_no_limite": _do_desenho(
            a1b_calibre.COM_SINAL, "limite_superior_ate_o_alvo"
        ),
        # Criterio 6 do incremento 13. Comparacao de datas entre duas tabelas
        # append-only, e nao uma afirmacao nossa de que fomos honestos.
        "a1b_ic_definido_antes_do_teste": ic_antes["antes"],
        # A2 - o simulador e honesto.
        "a2_b1_negativo": a2["negativo"],
        "a2_b1_proporcional_ao_giro": a2["proporcional_ao_giro"],
        # A3 - sem vazamento, conferido sobre o que ficou gravado.
        "a3_sem_vazamento": a3["sem_vazamento"],
        # A4 - integridade contabil.
        "a4_ledger_reconcilia": a4["conferencias"]["ledger_reconcilia"],
        "a4_custo_de_ia_por_decisao": a4["conferencias"][
            "custo_de_ia_por_decisao"
        ],
        "a4_nenhuma_tentativa_some": a4["conferencias"][
            "nenhuma_tentativa_some"
        ],
    }

    reprovando = sorted(n for n, ok in condicoes.items() if ok is False)
    pendentes = sorted(n for n, ok in condicoes.items() if ok is None)

    return {
        "gerado_em": _agora(),
        "portao": "A",
        "pergunta": "o protocolo rejeita defeito?",
        "config_version_id": config_version_id,
        "condicoes": condicoes,
        "reprovando": reprovando,
        "pendentes": pendentes,
        # TRES resultados, e nao dois. `None` continua diferente de `False`;
        # o que ele deixa de ser e diferente de "aprovado".
        "passa": not reprovando and not pendentes,
        "reprova": bool(reprovando),
        "pendente": bool(pendentes) and not reprovando,
        "se_reprova": (
            "Reprovar no Portao A nao e resultado ruim: e o resultado mais"
            " informativo possivel a esse custo, porque significa que o"
            " mecanismo central do projeto ainda nao existe. A resposta e"
            " consertar o protocolo e repetir, nao seguir para o Portao B"
            " (§14.4)"
        ),
        "portao_b": {
            "avaliado": False,
            "por_que": (
                "R49: o Portao B so e avaliado se o Portao A passar"
                " INTEGRALMENTE. Calcula-lo antes seria produzir o numero que"
                " a fase existe para nao produzir cedo demais"
            ),
        },
        "a1a": a1a,
        "a1b": a1b,
        "ic_antes_do_teste": ic_antes,
        "a2": a2,
        "a3": a3,
        "a4": a4,
        "nao_responde": NAO_RESPONDE,
    }
