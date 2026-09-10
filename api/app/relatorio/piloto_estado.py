"""As métricas do piloto que o relatório da 0C precisa publicar.

> *"consulte o estado atual do piloto e publique: observações recebidas,
> válidas e necessárias; dias completos e mínimo; taxa válida nas últimas 24
> horas; maior lacuna; regimes observados; estimativa apenas operacional da
> primeira data possível de fechamento."* — o usuário, 2026-09-10

**A estimativa de fechamento é OPERACIONAL, e o campo diz isso no nome.** Ela é
uma projeção de calendário sobre a taxa recente, e não uma afirmação sobre a
calibração — que continua recusada até a janela fechar (ADR 0027). Confundir as
duas seria começar a falar de fidelidade a partir de uma extrapolação, que é a
quinta pergunta do teste de escopo olhando para nós.

**Este módulo está FORA do fechamento transitivo do alvo de certificação.**
Conferido: o fechamento tem `app.relatorio` e `app.relatorio.portao_a`, e mais
nada de `relatorio`. Publicar o piloto não invalida os cinco escopos da `cv9`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

#: As duas travas da D45/ADR 0027. Obrigatórias as duas, e o piloto fecha na
#: MAIS TARDE — não na primeira que vencer.
OBSERVACOES_MINIMAS = 1_000
DIAS_MINIMOS = 14

DIA_MS = 86_400_000

#: A consequencia do ADR 0033, escrita UMA vez. Ela acompanha o bloco de
#: regimes em todos os ramos - inclusive no de "sem dado", onde ela vale mais:
#: zero regimes observados significa TODOS nao calibrados.
_SEM_AMOSTRA = (
    "NAO CALIBRADO, usando a base - nunca herdando de outro regime (ADR 0033)."
    " E resultado que atravessa regime nao calibrado fica inconclusivo quanto"
    " a fidelidade"
)


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def montar(conn: sqlite3.Connection) -> dict:
    """O estado do piloto, derivado de `bbo_amostra`. Nenhum número digitado."""
    linha = conn.execute(
        "SELECT COUNT(*) AS total,"
        "       SUM(disponivel) AS validas,"
        "       MIN(t_grid_ms) AS primeira,"
        "       MAX(t_grid_ms) AS ultima,"
        "       MIN(CASE WHEN disponivel = 1 THEN t_grid_ms END) AS pri_valida"
        "  FROM bbo_amostra"
    ).fetchone()
    total = int(linha["total"] or 0)
    validas = int(linha["validas"] or 0)
    primeira, ultima = linha["primeira"], linha["ultima"]
    pri_valida = linha["pri_valida"]

    if not total:
        return {
            "disponivel": False,
            "por_que": "nenhuma amostra de BBO gravada: o piloto nao comecou",
            "observacoes": {
                "recebidas": 0, "validas": 0,
                "necessarias": OBSERVACOES_MINIMAS,
            },
        }

    # ------------------------------------------------------------- os dias
    #
    # Contados da PRIMEIRA VALIDA, e nao da primeira linha: a grade abre antes
    # de o coletor existir, e os instantes anteriores a ele sao estruturais.
    # Chama-los de dia de piloto descreveria a nossa data de deploy como se
    # fosse o mercado - o defeito que `cobertura_total` x `cobertura_observada`
    # existe para nao cometer.
    agora_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    decorridos_ms = (agora_ms - int(pri_valida)) if pri_valida else 0
    dias_completos = decorridos_ms // DIA_MS

    # ------------------------------------------- a taxa das ultimas 24 horas
    janela = conn.execute(
        "SELECT COUNT(*) AS n, SUM(disponivel) AS ok FROM bbo_amostra"
        " WHERE t_grid_ms > ?",
        (agora_ms - DIA_MS,),
    ).fetchone()
    n_24h = int(janela["n"] or 0)
    ok_24h = int(janela["ok"] or 0)

    # ------------------------------------------------------- a maior lacuna
    #
    # Lacuna e a maior distancia entre duas VALIDAS consecutivas: e o tempo em
    # que o piloto nao acumulou. A grade e continua por construcao, entao a
    # lacuna de linhas nao diz nada - a de validade diz.
    maior_lacuna_ms = 0
    fim_da_lacuna = None
    anterior = None
    for l in conn.execute(
        "SELECT t_grid_ms FROM bbo_amostra WHERE disponivel = 1"
        " ORDER BY t_grid_ms"
    ):
        t = int(l["t_grid_ms"])
        if anterior is not None and t - anterior > maior_lacuna_ms:
            maior_lacuna_ms = t - anterior
            fim_da_lacuna = t
        anterior = t

    # -------------------------------------------- a projecao, e ela e do CAL
    faltam = max(0, OBSERVACOES_MINIMAS - validas)
    por_dia = (ok_24h * DIA_MS / max(1, DIA_MS)) if n_24h else 0
    dias_para_observacoes = (faltam / por_dia) if por_dia > 0 else None
    dias_para_calendario = max(0, DIAS_MINIMOS - dias_completos)
    espera_dias = (
        max(dias_para_observacoes, dias_para_calendario)
        if dias_para_observacoes is not None
        else None
    )
    primeira_possivel_ms = (
        agora_ms + int(espera_dias * DIA_MS) if espera_dias is not None else None
    )

    return {
        "disponivel": True,
        "estado": "acumulando" if faltam or dias_completos < DIAS_MINIMOS
        else "as duas travas alcancadas",
        "observacoes": {
            "recebidas": total,
            "validas": validas,
            "necessarias": OBSERVACOES_MINIMAS,
            "faltam": faltam,
            "fracao_valida_ppm": validas * 1_000_000 // total,
        },
        "dias": {
            "completos": int(dias_completos),
            "minimo": DIAS_MINIMOS,
            "faltam": int(dias_para_calendario),
            "contados_da_primeira_valida_em": _iso(pri_valida),
            "por_que_da_primeira_valida": (
                "a grade abre antes de o coletor existir, e os instantes"
                " anteriores a ele sao estruturais. Conta-los como dia de"
                " piloto descreveria a nossa data de deploy como se fosse o"
                " mercado"
            ),
        },
        "ultimas_24h": {
            "instantes": n_24h,
            "validas": ok_24h,
            "taxa_valida_ppm": (ok_24h * 1_000_000 // n_24h) if n_24h else None,
            "por_que_sem_taxa": (
                None if n_24h else "nenhum instante de grade nas ultimas 24h"
            ),
        },
        "maior_lacuna": {
            "duracao_ms": maior_lacuna_ms,
            "duracao_horas_milesimos": maior_lacuna_ms * 1_000 // 3_600_000,
            "terminou_em": _iso(fim_da_lacuna),
            "o_que_e": (
                "a maior distancia entre duas observacoes VALIDAS consecutivas"
                " - o tempo em que o piloto nao acumulou. A grade e continua"
                " por construcao, entao lacuna de LINHA nao diz nada"
            ),
        },
        "janela": {
            "primeira_ms": primeira, "primeira": _iso(primeira),
            "ultima_ms": ultima, "ultima": _iso(ultima),
        },
        "estimativa_OPERACIONAL_de_fechamento": {
            "primeira_data_possivel": _iso(primeira_possivel_ms),
            "dias_de_espera": (
                round(espera_dias, 2) if espera_dias is not None else None
            ),
            "trava_que_vence": (
                None
                if espera_dias is None
                else (
                    "observacoes"
                    if (dias_para_observacoes or 0) >= dias_para_calendario
                    else "calendario"
                )
            ),
            "sobre_que_taxa": (
                f"projetada sobre as {ok_24h} validas das ultimas 24h"
                if n_24h
                else "sem taxa recente: nao ha o que projetar"
            ),
            "o_que_isso_NAO_e": (
                "NAO e afirmacao sobre calibracao nem sobre fidelidade. E"
                " projecao de CALENDARIO sobre a taxa recente, e ela muda a"
                " cada queda do coletor. `estimar` continua RECUSANDO ate a"
                " janela fechar (ADR 0027), e tratar esta data como resultado"
                " seria comecar a falar de fidelidade a partir de uma"
                " extrapolacao"
            ),
        },
        "as_duas_travas": (
            "obrigatorias as duas, e o piloto fecha na MAIS TARDE. Alcancar"
            " 1.000 observacoes em cinco dias nao encurta os 14 dias, e"
            " esperar 14 dias com 300 observacoes nao dispensa as 1.000"
        ),
    }


def regimes_observados(conn: sqlite3.Connection) -> dict:
    """Quais regimes da D40 o forward já atravessou. **Derivado por leitura.**

    Importa porque o ADR 0033 é literal: **regime sem amostra fica NÃO
    CALIBRADO**, usando a base — nunca herdando de outro. Um piloto que fechar
    tendo visto um regime só calibra um regime só, e os outros dois seguem
    declarando `inconclusivo quanto a fidelidade`.

    **Classificado aqui, e não lido de tabela.** `calibracao_regime` só nasce
    com a calibração, e ela não aconteceu — nem pode acontecer antes de o
    piloto fechar. Então o regime é derivado do `stream_bar` com o mesmo
    detector congelado da D40, pela janela **causal**: nenhuma classificação
    usa a barra que ela classifica.

    Isto é **leitura**, e não calibração: nada é gravado, nada é aplicado, e o
    `spread_bps` executado segue sendo a base.
    """
    from ..regime import deteccao

    linhas = list(
        conn.execute(
            "SELECT open_time_ms, close FROM stream_bar ORDER BY open_time_ms"
        )
    )
    if len(linhas) < 2:
        return {
            "disponivel": False,
            "por_que": (
                f"o fluxo tem {len(linhas)} barra(s): nao ha retorno a"
                " classificar"
            ),
            # A CONSEQUENCIA vale mesmo sem dado - e vale MAIS sem dado.
            # Ela sumia deste ramo, e era justamente aqui que ela importava:
            # "nenhum regime observado" e o caso em que todos ficam nao
            # calibrados.
            "regime_sem_amostra_fica": _SEM_AMOSTRA,
            "e_com_zero_regimes_observados": (
                "TODOS os tres ficam nao calibrados, e todo resultado do"
                " forward sai inconclusivo quanto a fidelidade"
            ),
        }

    retornos: list[tuple[int, int | None]] = []
    anterior = None
    for l in linhas:
        fechamento = int(l["close"])
        r = (
            None
            if anterior in (None, 0)
            else (fechamento - anterior) * 10_000 // anterior
        )
        retornos.append((int(l["open_time_ms"]), r))
        anterior = fechamento

    classificacoes = deteccao.classificar_serie(retornos)
    cob = deteccao.cobertura(classificacoes)
    contagem: dict[str, int] = {}
    for c in classificacoes:
        chave = c.regime or "janela_incompleta"
        contagem[chave] = contagem.get(chave, 0) + 1

    return {
        "disponivel": True,
        "derivado_de": (
            f"{len(linhas)} barras de `stream_bar`, classificadas com o"
            " detector CONGELADO da D40 pela janela causal. Nada e gravado:"
            " isto e leitura, e nao calibracao"
        ),
        "barras_por_regime": dict(sorted(contagem.items())),
        "regimes_cobertos": cob["regimes_cobertos"],
        "quantos_cobertos": cob["quantidade"],
        "minimo_exigido": cob["minimo_exigido"],
        "cobertura_cumprida": cob["cumprida"],
        "episodios": cob["episodios"],
        "permanencia_barras": cob["permanencia_barras"],
        "cortes_congelados_mili_bps": {
            "inferior": deteccao.CORTE_INFERIOR_MILI_BPS,
            "superior": deteccao.CORTE_SUPERIOR_MILI_BPS,
        },
        "regime_sem_amostra_fica": _SEM_AMOSTRA,
        "o_que_a_cobertura_NAO_faz": (
            "ela nao promove nada e nao autoriza saida de quarentena: quem"
            " decide isso e o validador. Este bloco diz o FATO observado"
        ),
    }
