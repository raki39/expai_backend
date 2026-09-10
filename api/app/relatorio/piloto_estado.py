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

## Os DOIS bloqueadores, e nenhum escondido atrás do outro

> *"Acrescente uma projeção que mostre os dois possíveis bloqueadores: data
> mínima pelo calendário; data estimada pela contagem, usando a taxa válida das
> últimas 24/72 horas."* — o usuário, 2026-09-10

A versão anterior publicava UMA data — a mais tarde —, e o leitor não via qual
trava estava perto de virar. E ela tinha um defeito de relógio: projetava o
calendário como `agora + dias inteiros que faltam`, então a data **deslizava
com a hora da leitura** — 17:17 numa leitura, 17:59 na seguinte, sobre o mesmo
estado. O calendário é um instante FIXO: primeira válida + 14 dias corridos, a
trava 1 de `piloto.derivar`.

**A contagem é ancorada no ALCANCE do dado, e não no relógio.** A taxa das
últimas 24/72 horas é medida sobre as últimas 24/72 horas **da grade gravada**,
e a projeção parte do último instante gravado. O mesmo estado produz a mesma
projeção — e a defasagem entre o dado e o relógio vai publicada ao lado, porque
um coletor parado congelaria a projeção com cara de saudável.

## As lacunas registradas, SEPARADAS do piloto

> *"Não altere nem reinicie o piloto por causa da lacuna de 21,8 horas, salvo
> se uma regra pré-registrada exigir isso. Registre separadamente a causa,
> componente, recuperação e impacto da lacuna."* — o usuário, 2026-09-10

Causa, componente e recuperação são **história registrada**: não saem do banco.
As bordas e o impacto **saem do banco**, e o registro confere as próprias bordas
contra o dado a cada leitura. Se o `bbo_amostra` deixar de mostrar a lacuna onde
o registro diz que ela está, `o_registro_descreve_o_dado` vira `False` — em vez
de o texto continuar afirmando.

**Este módulo está FORA do fechamento transitivo do alvo de certificação.**
Conferido: o fechamento tem `app.relatorio` e `app.relatorio.portao_a`, e mais
nada de `relatorio`. Publicar o piloto não invalida os cinco escopos.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

# As travas do ADR 0027 vivem em `calibracao.piloto` e sao IMPORTADAS. Elas
# estavam copiadas aqui - duas definicoes da mesma regra em modulos diferentes,
# e a copia e a que envelhece calada.
from ..calibracao.piloto import DIAS_MINIMOS, MS_POR_DIA, OBSERVACOES_MINIMAS

MS_POR_HORA = 3_600_000

#: As janelas da taxa recente, em horas - as duas que o usuario pediu. A de 24h
#: reage rapido a uma queda; a de 72h amortece uma queda isolada. Nenhuma e "a
#: certa", e e por isso que as duas vao publicadas.
JANELAS_DE_TAXA_HORAS = (24, 72)

#: A consequencia do ADR 0033, escrita UMA vez. Ela acompanha o bloco de
#: regimes em todos os ramos - inclusive no de "sem dado", onde ela vale mais:
#: zero regimes observados significa TODOS nao calibrados.
_SEM_AMOSTRA = (
    "NAO CALIBRADO, usando a base - nunca herdando de outro regime (ADR 0033)."
    " E resultado que atravessa regime nao calibrado fica inconclusivo quanto"
    " a fidelidade"
)

#: As lacunas que aconteceram e ja foram EXPLICADAS. Causa, componente e
#: recuperacao sao historia: nao ha consulta que as derive. As bordas sao
#: conferidas contra o dado a cada leitura (`lacunas_registradas`).
LACUNAS_REGISTRADAS: tuple[dict, ...] = (
    {
        "id": "coletor-boot-zlib-2026-09-07",
        # 2026-09-07 18:45 UTC e 2026-09-08 16:30 UTC: as duas validas que
        # cercam a lacuna, lidas de `/api/relatorio/checkpoint` em 2026-09-10.
        "ultima_valida_antes_ms": 1_788_806_700_000,
        "primeira_valida_depois_ms": 1_788_885_000_000,
        "componente": (
            "o COLETOR de BBO, em Singapura - `coletor/main.py:selar_no_boot`"
            " -> `coletor/arquivo.py:manifesto`. Nao a `api`, e nao o rele"
        ),
        "causa": (
            "ciclo de crash no BOOT. A conferencia de integridade listava"
            " `(EOFError, OSError, gzip.BadGzipFile)`, e `zlib.error` - bloco"
            " deflate danificado no MEIO de um membro gzip - nao e `OSError`."
            " A selagem roda no boot, entao cada restart morria no mesmo"
            " arquivo, e a coleta morria junto"
        ),
        "origem_do_arquivo_danificado": (
            "leitura PROVAVEL, nao confirmada por carimbo: SIGKILL durante a"
            " escrita, num redeploy - a Railway derruba o container em"
            " redeploy, e e isso que produz bloco deflate cortado no meio"
        ),
        "recuperacao": (
            "commit 377898b do backend: uma travessia so (`_descomprimir`)"
            " para a leitura e a conferencia, `varrer` devolvendo fato em vez"
            " de levantar, e nenhuma falha de selagem alcancando o boot. O"
            " coletor foi de 62 para 69 testes"
        ),
        "recuperacao_commit": "377898b",
        # 2026-09-08 16:25:55 UTC. Commit nao e deploy: a conferencia abaixo
        # diz "consistente", e nao "provado".
        "recuperacao_commit_ms": 1_788_884_755_000,
    },
)


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def _agora_ms() -> int:
    """O relogio de LEITURA, isolado para que o teste prove quem depende dele."""
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _grade_ms(conn: sqlite3.Connection) -> int:
    linha = conn.execute(
        "SELECT grade_ms FROM bbo_amostra ORDER BY t_grid_ms DESC LIMIT 1"
    ).fetchone()
    return int(linha["grade_ms"])


def _taxa(conn: sqlite3.Connection, *, ate_ms: int, horas: int) -> dict:
    """A fracao valida das ultimas `horas` DA GRADE GRAVADA, terminando em `ate_ms`."""
    linha = conn.execute(
        "SELECT COUNT(*) AS n, SUM(disponivel) AS ok FROM bbo_amostra"
        " WHERE t_grid_ms >= ? AND t_grid_ms < ?",
        (ate_ms - horas * MS_POR_HORA, ate_ms),
    ).fetchone()
    n, ok = int(linha["n"] or 0), int(linha["ok"] or 0)
    return {
        "janela_horas": horas,
        "instantes": n,
        "validas": ok,
        "taxa_valida_ppm": (ok * 1_000_000 // n) if n else None,
    }


def _projetar(
    taxa: dict,
    *,
    faltam: int,
    alcance_ms: int,
    grade_ms: int,
    fim_calendario_ms: int,
) -> dict:
    """Quando a trava de contagem fecharia SE a taxa desta janela se mantiver.

    A regra e a de `piloto.derivar`: a trava fecha em `t da 1.000-esima valida
    + uma grade`. Com fracao valida `ok/n`, faltam `ceil(faltam * n / ok)`
    instantes de grade a partir do alcance - arredondado para CIMA, porque a
    1.000-esima nao chega antes do instante inteiro que a contem.
    """
    if not taxa["validas"]:
        return {
            **taxa,
            "data_estimada": None,
            "por_que_sem_data": (
                "nenhuma observacao valida nesta janela: a esta taxa a trava"
                " de contagem nao fecha nunca"
            ),
        }
    instantes = -(-faltam * taxa["instantes"] // taxa["validas"])
    fim_ms = alcance_ms + instantes * grade_ms
    return {
        **taxa,
        "instantes_de_grade_necessarios": instantes,
        "data_estimada": _iso(fim_ms),
        "bloqueador": "calendario" if fim_calendario_ms >= fim_ms else "contagem",
        "primeira_possibilidade": _iso(max(fim_ms, fim_calendario_ms)),
        "folga_horas_milesimos": (fim_calendario_ms - fim_ms) * 1_000 // MS_POR_HORA,
    }


def lacunas_registradas(
    conn: sqlite3.Connection, *, alcance_ms: int | None = None
) -> list[dict]:
    """Cada lacuna registrada, com as bordas CONFERIDAS e o impacto DERIVADO."""
    saida = []
    for reg in LACUNAS_REGISTRADAS:
        ini = reg["ultima_valida_antes_ms"]
        fim = reg["primeira_valida_depois_ms"]
        bordas = {
            int(l["t_grid_ms"]): int(l["disponivel"])
            for l in conn.execute(
                "SELECT t_grid_ms, disponivel FROM bbo_amostra"
                " WHERE t_grid_ms IN (?, ?)",
                (ini, fim),
            )
        }
        dentro = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(disponivel), 0) AS ok"
            "  FROM bbo_amostra WHERE t_grid_ms > ? AND t_grid_ms < ?",
            (ini, fim),
        ).fetchone()
        motivos = {
            (l["motivo"] or "sem_motivo"): int(l["n"])
            for l in conn.execute(
                "SELECT motivo, COUNT(*) AS n FROM bbo_amostra"
                " WHERE t_grid_ms > ? AND t_grid_ms < ? AND disponivel = 0"
                " GROUP BY motivo ORDER BY motivo",
                (ini, fim),
            )
        }
        barras_do_rele = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM stream_bar"
                " WHERE open_time_ms > ? AND open_time_ms < ?",
                (ini, fim),
            ).fetchone()["n"]
        )
        linha_grade = conn.execute(
            "SELECT grade_ms FROM bbo_amostra WHERE t_grid_ms = ?", (fim,)
        ).fetchone()
        grade = int(linha_grade["grade_ms"]) if linha_grade else None
        esperados = ((fim - ini) // grade - 1) if grade else None

        descreve = (
            bordas.get(ini) == 1 and bordas.get(fim) == 1 and int(dentro["ok"]) == 0
        )
        saida.append(
            {
                "id": reg["id"],
                "ultima_valida_antes": _iso(ini),
                "primeira_valida_depois": _iso(fim),
                "o_registro_descreve_o_dado": descreve,
                "por_que_pode_nao_descrever": (
                    None
                    if descreve
                    else (
                        "este banco nao tem uma valida em cada borda e nenhuma"
                        " valida entre elas. Num banco que nao e o de producao"
                        " isso e esperado; no de producao, o registro parou de"
                        " descrever o dado"
                    )
                ),
                # ------------------------------------------ o que e HISTORIA
                "componente": reg["componente"],
                "causa": reg["causa"],
                "origem_do_arquivo_danificado": reg["origem_do_arquivo_danificado"],
                "recuperacao": reg["recuperacao"],
                "recuperacao_commit": reg["recuperacao_commit"],
                "recuperacao_commit_em": _iso(reg["recuperacao_commit_ms"]),
                "a_volta_e_consistente_com_a_correcao": (
                    grade is not None
                    and 0 <= fim - reg["recuperacao_commit_ms"] <= grade
                ),
                "o_que_essa_consistencia_NAO_prova": (
                    "commit nao e deploy. A primeira valida depois da lacuna e"
                    " o primeiro instante de grade apos o commit da correcao -"
                    " isso e consistente com a correcao ter encerrado a lacuna,"
                    " e nao prova que encerrou"
                ),
                # --------------------------------------- o que SAI do banco
                "impacto": {
                    "duracao_horas_milesimos": (fim - ini) * 1_000 // MS_POR_HORA,
                    "instantes_de_grade_sem_validade": int(dentro["n"]),
                    "instantes_esperados_entre_as_bordas": esperados,
                    "por_motivo": motivos,
                    "na_trava_de_calendario": (
                        "NENHUM. Ela conta da primeira valida do piloto, e nao"
                        " de observacoes - a lacuna nao a move nem para antes"
                        " nem para depois"
                    ),
                    "na_trava_de_contagem": (
                        "atrasa pela propria duracao: cada instante de grade"
                        " sem validade e uma observacao que nao entrou, e a"
                        " 1.000-esima chega esse tanto de grade mais tarde"
                    ),
                    # A janela de uma taxa CONTEM a lacuna quando as duas se
                    # sobrepoem - e e isso que explica as duas taxas
                    # discordarem.
                    "a_taxa_de_72h_contem_esta_lacuna": (
                        None
                        if alcance_ms is None
                        else fim > alcance_ms - 72 * MS_POR_HORA
                    ),
                    "a_taxa_de_24h_contem_esta_lacuna": (
                        None
                        if alcance_ms is None
                        else fim > alcance_ms - 24 * MS_POR_HORA
                    ),
                    "rele_no_mesmo_intervalo": {
                        "barras": barras_do_rele,
                        "esperadas": esperados,
                        "o_que_isso_mostra": (
                            "o rele de klines tambem esta em Singapura. Barras"
                            " completas no mesmo intervalo dizem que a queda"
                            " foi do COLETOR, e nao da rede ate a Binance nem"
                            " da `api`"
                        ),
                    },
                },
                # ----------------------------------------------- o PILOTO
                "o_que_foi_feito_com_o_piloto": (
                    "NADA. Ele nao foi alterado nem reiniciado: o inicio"
                    " continua na primeira valida, e as duas travas continuam"
                    " as do ADR 0027"
                ),
                "regra_pre_registrada_que_exigiria_reinicio": None,
                "por_que_nenhuma": (
                    "o ADR 0027 fixa so as duas travas - 1.000 validas e 14"
                    " dias corridos, a mais tarde. O ADR 0032 trata queda como"
                    " atraso recuperavel, e ausencia como `disponivel = 0` com"
                    " motivo, que entra na cobertura. Nenhum dos dois pede"
                    " continuidade"
                ),
            }
        )
    return saida


def _explicada_por(inicio_ms: int | None, fim_ms: int | None) -> str | None:
    for reg in LACUNAS_REGISTRADAS:
        if (reg["ultima_valida_antes_ms"], reg["primeira_valida_depois_ms"]) == (
            inicio_ms, fim_ms,
        ):
            return reg["id"]
    return None


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
            "lacunas_registradas": lacunas_registradas(conn),
        }

    agora_ms = _agora_ms()
    grade_ms = _grade_ms(conn)
    # O ALCANCE do dado: o proximo instante de grade depois do ultimo gravado.
    # E dele que a projecao parte - e `piloto.derivar` usa a mesma definicao
    # para recusar fechar uma janela cujo periodo ainda nao existe.
    alcance_ms = int(ultima) + grade_ms

    # ------------------------------------------------------------- os dias
    #
    # Contados da PRIMEIRA VALIDA, e nao da primeira linha: a grade abre antes
    # de o coletor existir, e os instantes anteriores a ele sao estruturais.
    # Chama-los de dia de piloto descreveria a nossa data de deploy como se
    # fosse o mercado - o defeito que `cobertura_total` x `cobertura_observada`
    # existe para nao cometer.
    decorridos_ms = (agora_ms - int(pri_valida)) if pri_valida else 0
    dias_completos = decorridos_ms // MS_POR_DIA

    # --------------------------------- as taxas, sobre a grade GRAVADA
    taxas = {
        h: _taxa(conn, ate_ms=alcance_ms, horas=h) for h in JANELAS_DE_TAXA_HORAS
    }

    # ------------------------------------------------------- a maior lacuna
    #
    # Lacuna e a maior distancia entre duas VALIDAS consecutivas: e o tempo em
    # que o piloto nao acumulou. A grade e continua por construcao, entao a
    # lacuna de linhas nao diz nada - a de validade diz.
    maior_lacuna_ms = 0
    inicio_da_lacuna = fim_da_lacuna = None
    anterior = None
    for l in conn.execute(
        "SELECT t_grid_ms FROM bbo_amostra WHERE disponivel = 1"
        " ORDER BY t_grid_ms"
    ):
        t = int(l["t_grid_ms"])
        if anterior is not None and t - anterior > maior_lacuna_ms:
            maior_lacuna_ms = t - anterior
            inicio_da_lacuna, fim_da_lacuna = anterior, t
        anterior = t

    # ---------------------------------------------- os DOIS bloqueadores
    faltam = max(0, OBSERVACOES_MINIMAS - validas)
    fim_calendario_ms = (
        int(pri_valida) + DIAS_MINIMOS * MS_POR_DIA if pri_valida else None
    )
    contagem: dict
    if fim_calendario_ms is None:
        contagem = {
            "alcancada": False,
            "por_que_sem_projecao": (
                "nenhuma observacao valida ainda: o piloto comeca na primeira"
                " valida, e sem ela nao ha calendario nem contagem a projetar"
            ),
        }
    elif faltam == 0:
        milesima = conn.execute(
            "SELECT t_grid_ms FROM bbo_amostra WHERE disponivel = 1"
            " ORDER BY t_grid_ms LIMIT 1 OFFSET ?",
            (OBSERVACOES_MINIMAS - 1,),
        ).fetchone()
        fim_contagem_ms = int(milesima["t_grid_ms"]) + grade_ms
        contagem = {
            "alcancada": True,
            "data": _iso(fim_contagem_ms),
            "bloqueador": (
                "calendario" if fim_calendario_ms >= fim_contagem_ms else "contagem"
            ),
        }
    else:
        contagem = {
            "alcancada": False,
            **{
                f"pela_taxa_das_ultimas_{h}h": _projetar(
                    taxas[h],
                    faltam=faltam,
                    alcance_ms=alcance_ms,
                    grade_ms=grade_ms,
                    fim_calendario_ms=fim_calendario_ms,
                )
                for h in JANELAS_DE_TAXA_HORAS
            },
            "sinal_da_folga": (
                "folga POSITIVA: o calendario e o bloqueador, e essa e a"
                " quantidade de horas de coleta perdida - a esta taxa - que"
                " inverteria qual trava vence. NEGATIVA: a contagem ja e o"
                " bloqueador"
            ),
        }

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
            "faltam": int(max(0, DIAS_MINIMOS - dias_completos)),
            "contados_da_primeira_valida_em": _iso(pri_valida),
            "por_que_da_primeira_valida": (
                "a grade abre antes de o coletor existir, e os instantes"
                " anteriores a ele sao estruturais. Conta-los como dia de"
                " piloto descreveria a nossa data de deploy como se fosse o"
                " mercado"
            ),
        },
        "ultimas_24h": taxas[24],
        "ultimas_72h": taxas[72],
        "as_taxas_sao_do_DADO": (
            "as ultimas 24/72 horas DA GRADE GRAVADA, terminando no alcance do"
            " dado - e nao no relogio de leitura. Assim o mesmo estado da a"
            " mesma taxa, e a defasagem do dado vai publicada a parte"
        ),
        "maior_lacuna": {
            "duracao_ms": maior_lacuna_ms,
            "duracao_horas_milesimos": maior_lacuna_ms * 1_000 // MS_POR_HORA,
            "comecou_em": _iso(inicio_da_lacuna),
            "terminou_em": _iso(fim_da_lacuna),
            "explicada_por": _explicada_por(inicio_da_lacuna, fim_da_lacuna),
            "o_que_e": (
                "a maior distancia entre duas observacoes VALIDAS consecutivas"
                " - o tempo em que o piloto nao acumulou. A grade e continua"
                " por construcao, entao lacuna de LINHA nao diz nada"
            ),
            "se_explicada_por_for_None": (
                "a maior lacuna NAO esta em `lacunas_registradas`: ela nao tem"
                " causa, componente nem recuperacao escritos, e alguem precisa"
                " escreve-los"
            ),
        },
        "lacunas_registradas": lacunas_registradas(conn, alcance_ms=alcance_ms),
        "janela": {
            "primeira_ms": primeira, "primeira": _iso(primeira),
            "ultima_ms": ultima, "ultima": _iso(ultima),
        },
        "estimativa_OPERACIONAL_de_fechamento": {
            "os_dois_bloqueadores": {
                "calendario": {
                    "data_minima": _iso(fim_calendario_ms),
                    "regra": (
                        "primeira valida + 14 dias corridos - a trava 1 de"
                        " `piloto.derivar`. E um instante FIXO: nao depende de"
                        " taxa, e nao depende da hora da leitura"
                    ),
                    "e_ainda_exige": (
                        "que o dado ALCANCE essa data. `piloto.derivar` recusa"
                        " fechar uma janela cujo periodo ainda nao existe, entao"
                        " o coletor precisa estar entregando nesse instante"
                    ),
                },
                "contagem": contagem,
            },
            "e_uma_POSSIBILIDADE_e_nao_uma_promessa": (
                "a data mais cedo que as duas travas permitem SE a taxa recente"
                " se mantiver. Uma queda do coletor empurra a contagem; nada"
                " traz o calendario para antes. Nenhuma data aqui e compromisso"
            ),
            "ancorada_em": {
                "alcance_do_dado": _iso(alcance_ms),
                "defasagem_do_dado_horas_milesimos": (
                    (agora_ms - alcance_ms) * 1_000 // MS_POR_HORA
                ),
                "por_que_publicar_a_defasagem": (
                    "a projecao parte do dado, e nao do relogio. Um coletor"
                    " parado congelaria a projecao com cara de saudavel - e a"
                    " defasagem crescendo e o que denuncia isso"
                ),
            },
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
