"""O relatorio de capacidade experimental da D48.

Ele responde uma pergunta que este projeto nunca tinha feito: **o desenho
consegue testar o que ele proprio declara como efeito minimo?**

A resposta, medida e nao afirmada, e **nao** - e o usuario mandou registra-la
explicitamente:

> *"Com BY, familia maxima atual e potencia de 80%, o desenho atual nao
> consegue testar o efeito minimo dentro do in-sample disponivel."*

## Por que isto e relatorio, e nao uma frase no CLAUDE.md

Porque todo insumo dele muda. O tamanho da familia esta na config versionada, o
FDR tambem, a variancia e a dependencia sao MEDIDAS do dataset vigente, e as
janelas saem da divisao da D27. Uma frase com os numeros de hoje envelheceria
calada no dia em que qualquer um deles mudasse - e este projeto conta vinte e
quatro vezes o custo disso.

Entao cada campo aqui e o resultado de uma consulta ou de uma medicao sobre as
barras. Nenhum e digitado.

## As duas leituras, publicadas lado a lado

| leitura | dimensiona contra | mede |
|---|---|---|
| **efeito minimo** (a regua da D48) | o menor efeito que IMPORTA | o que o desenho consegue descartar |
| **Sharpe declarado** (a leitura antiga) | o efeito que a hipotese ESPERA | o que o desenho consegue detectar se der certo |

As duas concordam no veredito por larga margem, e e por isso que a conclusao e
robusta: ela nao depende de qual das duas parametrizacoes alguem prefira.
Publicar so a que eu escolhi seria trocar a regua sem dizer.

## O que este relatorio NAO faz, e e o ponto

Ele **nao escolhe** nenhuma saida. Coletar mais dado, reduzir a familia das
proximas, exigir efeito minimo maior ou adotar outro procedimento sao quatro
opcoes prospectivas, e o usuario foi explicito:

> *"Nenhuma dessas opcoes deve ser escolhida agora olhando resultados ja
> produzidos."*

Elas ficam listadas em `OPCOES_PROSPECTIVAS` com o custo de cada uma, e o campo
`escolhida` nao existe. Um campo que existisse com valor `None` seria um convite
a preenche-lo na proxima vez que o numero incomodasse.
"""

from __future__ import annotations

import sqlite3
import statistics
from typing import Any

from ..dataset import loader, split
from ..hipotese import dimensionamento, poder

#: A conclusao que a D48 revelou, no texto do usuario. Fica em constante e vai
#: para a resposta: e a afirmacao que o relatorio existe para sustentar, e ela
#: precisa aparecer ao lado dos numeros que a produzem.
CONCLUSAO = (
    "Com BY, familia maxima atual e potencia de 80%, o desenho atual nao"
    " consegue testar o efeito minimo dentro do in-sample disponivel."
)

#: As quatro saidas possiveis, e nenhuma esta escolhida. A lista e do usuario.
#:
#: **Nao ha campo `escolhida`.** Escolher uma agora seria escolher olhando o
#: resultado que acabou de sair - a quinta pergunta do teste de escopo responde
#: "sim" -, e a decisao e prospectiva por construcao.
OPCOES_PROSPECTIVAS = [
    {
        "opcao": "coletar mais dados",
        "custo": (
            "calendario: a amostra exigida cresce com o quadrado de t, e o"
            " deficit e medido em dezenas de milhares de barras de 15 min"
        ),
    },
    {
        "opcao": "reduzir previamente o tamanho das proximas familias",
        "custo": (
            "menos hipoteses por lote; H(m) cai devagar (logaritmicamente),"
            " entao o ganho no limiar e pequeno perto do corte na exploracao"
        ),
    },
    {
        "opcao": "exigir efeito minimo maior",
        "custo": (
            "so hipoteses de efeito grande entram, e a secao 8.3 ja avisa que"
            " 'apenas efeitos grandes sao detectaveis no horizonte do projeto'"
        ),
    },
    {
        "opcao": "adotar outro procedimento estatistico valido",
        "custo": (
            "precisa controlar FDR sob dependencia arbitraria, que foi o que"
            " levou a BY na D26; trocar por um mais generoso e afrouxar"
        ),
    },
]

#: O que NAO e opcao, e o usuario nomeou os tres pelo nome.
#:
#: > "O perigo seria reagir ao resultado aumentando o Sharpe maximo ou
#: > afrouxando a estatistica so para o projeto continuar. Nao vamos fazer
#: > isso."
FORA_DE_COGITACAO = [
    "aumentar o teto de Sharpe do schema para a hipotese caber",
    "reduzir a familia depois de ver o resultado",
    "alterar o efeito minimo de hipotese ja registrada",
]


#: A conferência dimensional que o usuario pediu antes do incremento 21, e ela
#: virou campo porque a resposta e uma DISTINÇÃO, nao um numero.
#:
#: > "Nao deixe 'US$ 500' parecer um total constante se ele estiver sendo
#: > escalado proporcionalmente com o horizonte." - o usuario, 2026-09-08
#:
#: Ele **esta** sendo escalado, e a resposta e o segundo ramo da pergunta: o
#: efeito minimo e um total declarado SOBRE UM HORIZONTE, entao ele define uma
#: taxa, e e a taxa que se detecta.
_CONFERENCIA_DIMENSIONAL = {
    "o_que_e_detectado": "a TAXA por barra, nunca um total em centavos",
    "por_que_o_minimo_detectavel_CRESCE_com_o_horizonte": (
        "porque ele e um TOTAL sobre a janela: a taxa detectavel cai com"
        " 1/sqrt(n), e a janela cresce com n - entao o produto cresce com"
        " sqrt(n). Nao significa que mais dado piora nada"
    ),
    "por_que_ele_tambem_aparece_como_NECESSARIO": (
        "porque as duas contas resolvem variaveis diferentes da mesma"
        " equacao. `dimensionar` fixa a taxa e resolve o `n`; `capacidade`"
        " fixa o `n` e resolve a taxa. Elas fecham por construcao: a"
        " diferenca entre esperado e detectavel cruza zero exatamente em"
        " `n_bruto_necessario`"
    ),
    "o_numero_comparavel_entre_horizontes": (
        "`sharpe_anualizado_milesimos`. O efeito em centavos NAO e"
        " comparavel entre janelas, porque e um total sobre elas"
    ),
    "como_ler_a_tabela": (
        "esperado e detectavel crescem OS DOIS. A diferenca entre eles NAO e"
        " monotonica: ver o campo seguinte, que e o unico jeito de ler esta"
        " tabela sem tirar a conclusao errada"
    ),
    "a_diferenca_PIORA_antes_de_melhorar": (
        "o esperado cresce com n (linear na janela) e o detectavel com sqrt(n)"
        " (a sensibilidade melhora com a raiz). A raiz lidera no comeco e o"
        " linear vence no fim, entao a diferenca em centavos ATINGE UM MINIMO"
        " intermediario e so depois sobe para zero. Medido: -764 no in-sample,"
        " -788 no dobro dele (pior), -642 no dataset inteiro, e zero no"
        " cruzamento. Dobrar o dado piora o vao EM DINHEIRO antes de melhorar,"
        " ainda que o Sharpe exigido caia sempre - e e exatamente por isso que"
        " o numero comparavel entre horizontes e o Sharpe, e nao o dolar"
    ),
    "onde_esta_o_pior_ponto": (
        "em sqrt(n) = detectavel_por_barra / (2 x taxa_declarada), que sai da"
        " derivada de a*n - b*sqrt(n). Nao e publicado como campo porque nao"
        " decide nada: e curiosidade da forma da curva, e o que decide e o"
        " cruzamento"
    ),
}

#: E a segunda exigência da mesma conferência, que é sobre RESERVA e não sobre
#: aritmética.
#:
#: > "Deixe explicito que in-sample + walk-forward e dataset inteiro sao
#: > cenarios prospectivos de capacidade, nao dados que possam ser reutilizados
#: > livremente pela hipotese atual. Walk-forward e holdout continuam selados
#: > segundo suas finalidades originais." - o usuario, 2026-09-08
#:
#: Sem isto, a tabela convida ao pior erro possivel nesta fase: somar conjuntos
#: para "ter mais amostra" e descobrir depois que o que se gastou era o
#: conjunto que validava o resultado.
_PROSPECTIVO_NAO_E_REUTILIZAVEL = (
    "As linhas marcadas `prospectivo` respondem 'e se tivessemos mais dado?',"
    " e NAO 'a hipotese atual pode usar isto'. A separacao da secao 8.5.1"
    " continua inteira: o walk-forward segue selado para confirmar FORA da"
    " amostra em tres janelas (secao 14.4, criterio 5), o holdout tem uso"
    " UNICO por hipotese imposto por `UNIQUE (hypothesis_id)`, e a exploracao"
    " ja foi observada pelo agente (D34) - reusa-la como teste devolveria a"
    " sobreposicao amostral que caiu de 100% para zero. Anexar qualquer um"
    " deles ao in-sample nao produz amostra: gasta a evidencia que valida."
)


#: **OS NUMEROS DESTE RELATORIO ESTAO NAO VALIDADOS.** Rastreado em 2026-09-08,
#: por exigencia do usuario, e ele estava certo.
#:
#: > "Se a D48 dimensiona um efeito minimo de US$ 500 sobre o B3, a variancia e
#: > a dependencia precisam pertencer ao estimador da DIFERENCA entre estrategia
#: > e B3 - nao apenas aos retornos do mercado ou de um dos runs isoladamente."
#:
#: **Elas pertencem aos retornos do mercado.** `montar` mede `desvio_bps` e
#: `rho_ppm` sobre `loader.retornos_bps_entre`, que e a serie de fechamento a
#: fechamento do DATASET - a mesma para qualquer estrategia sobre a mesma
#: janela.
#:
#: O efeito minimo declarado e `excesso_sobre_b3_cents`. Padronizar um efeito
#: de DIFERENCA pela volatilidade do MERCADO estima o objeto errado: a variancia
#: da diferenca depende de quanto as duas estrategias se movem JUNTAS, e nao de
#: quanto o mercado se move.
#:
#: ### Medido, em cenario sintetico com dois cruzamentos sobre a mesma janela
#:
#: | | desvio por barra, em centavos |
#: |---|---|
#: | mercado x base | 221,53 |
#: | diferenca real (grade comum de marcacao) | **121,08** |
#: | razao | 0,547 |
#: | **fator em `n`** (escala com o quadrado) | **0,299** |
#:
#: A amostra exigida esta **superestimada em cerca de 3,3x**. Nao e um erro de
#: casas decimais: muda qual conclusao sobrevive.
#:
#: ### O que sobrevive e o que nao
#:
#: **Sobrevive:** "o desenho nao consegue testar o efeito minimo no in-sample" -
#: mesmo corrigido, o `n` exigido continua muito acima das 21.024 barras.
#:
#: **NAO sobrevive:** "nem o dataset inteiro alcanca". Com o fator medido, o `n`
#: exigido cai para perto de 40.000, abaixo das 70.080 do dataset. Essa frase
#: fica RETIRADA ate a recalibracao.
#:
#: ### O que corrige
#:
#: A serie de excesso incremental sobre uma grade comum de marcacao a mercado,
#: `excesso_t = (equity_agente_t - equity_agente_{t-1}) - (equity_B3_t -
#: equity_B3_{t-1})`. **Medido: ela reconstroi o excesso final EXATAMENTE** - a
#: soma bateu ao centavo com `caixa_agente - caixa_B3`. A construcao e viavel
#: com o que existe (`curva_do_run` sobre a mesma grade de barras).
#:
#: Ela e **correcao bloqueante antes do relatorio definitivo**, e nao divida.
VALIDACAO_DA_VARIANCIA = {
    "pertence_ao_estimador_do_efeito": False,
    "o_que_a_variancia_mede_hoje": (
        "retornos do MERCADO, fechamento a fechamento do dataset - a mesma"
        " serie para qualquer estrategia sobre a mesma janela"
    ),
    "o_que_ela_deveria_medir": (
        "a diferenca por barra entre a equity da candidata e a do B3, numa"
        " grade comum de marcacao a mercado - porque o efeito minimo declarado"
        " e `excesso_sobre_b3_cents`"
    ),
    "fator_medido_em_n": 0.299,
    "amostra_exigida_esta": "SUPERESTIMADA em cerca de 3,3x",
    "o_que_sobrevive": (
        "a conclusao sobre o IN-SAMPLE: mesmo corrigido, o `n` exigido fica"
        " muito acima das 21.024 barras"
    ),
    "o_que_NAO_sobrevive": (
        "a frase 'nem o dataset inteiro alcanca'. Com o fator medido o `n`"
        " exigido cai para perto de 40.000, abaixo das 70.080 do dataset."
        " RETIRADA ate a recalibracao"
    ),
    "correcao": (
        "serie de excesso incremental sobre grade comum de marcacao a mercado."
        " Medido: ela reconstroi o excesso final EXATAMENTE, ao centavo."
        " CORRECAO BLOQUEANTE antes do relatorio definitivo"
    ),
}


def _desvio_por_barra_bps(retornos: list[int]) -> int:
    """A variancia da D48, medida - e arredondada para BAIXO.

    Para baixo porque um desvio menor faz o efeito parecer mais facil de
    detectar: e o arredondamento que erra contra a nossa conclusao, e portanto
    o unico que se pode fazer de graca aqui.

    Devolve zero quando a serie e curta demais. Zero nao e "sem volatilidade":
    e "nao medido", e quem chama recusa em vez de seguir com ele.
    """
    if len(retornos) < 2:
        return 0
    return int(statistics.pstdev(retornos))


def _taxa_de_referencia(
    conn: sqlite3.Connection, comum: dict[str, Any]
) -> dict[str, Any] | None:
    """A hipotese de referencia, e a TAXA que o efeito minimo dela implica.

    Referencia e a **ultima do agente** - a que o Portao B avaliou. Nao e uma
    escolha estetica: a tabela de capacidade compara contra UMA taxa, e usar a
    media de varias produziria um numero que nenhuma hipotese declarou.

    Devolve `None` quando nao ha hipotese do agente. `None` e nao zero: sem
    hipotese nao ha taxa declarada, e uma taxa zero afirmaria um efeito
    esperado de nada.
    """
    from ..hipotese import registro as hipotese_registro

    linha = conn.execute(
        "SELECT id, efeito_minimo, horizonte_barras, sharpe_esperado_milesimos"
        "  FROM hypothesis WHERE agente_origem = ?"
        " ORDER BY id DESC LIMIT 1",
        (hipotese_registro.AGENTE_ORIGEM,),
    ).fetchone()
    if linha is None:
        return None
    try:
        d = dimensionamento.dimensionar(
            efeito_minimo_cents=abs(int(linha["efeito_minimo"])),
            horizonte_barras=int(linha["horizonte_barras"]),
            sharpe_esperado_milesimos=int(linha["sharpe_esperado_milesimos"]),
            **comum,
        )
    except dimensionamento.DimensionamentoImpossivel:
        return None
    return {
        "hypothesis_id": int(linha["id"]),
        "efeito_minimo_cents": d.insumos.efeito_minimo_cents,
        "horizonte_declarado_barras": d.insumos.horizonte_declarado_barras,
        "base_de_normalizacao_cents": d.insumos.base_de_normalizacao_cents,
        "taxa_por_barra_bps_micro": d.taxa_por_barra_bps_micro,
        "n_bruto_necessario": d.n_bruto_necessario,
        "efeito_acumulado_no_cruzamento_cents": (
            d.efeito_acumulado_no_cruzamento_cents
        ),
        "o_efeito_minimo_nao_e_um_total_constante": (
            f"{d.insumos.efeito_minimo_cents} centavos e o efeito TOTAL"
            f" declarado sobre {d.insumos.horizonte_declarado_barras} barras,"
            " e nao um total que valha em qualquer janela. O que a conta de"
            " amostra detecta e a TAXA que os dois implicam:"
            f" {d.taxa_por_barra_bps_micro / 1_000_000:.6f} bps por barra."
            f" Nas {d.n_bruto_necessario} barras exigidas essa mesma taxa"
            f" acumula {d.efeito_acumulado_no_cruzamento_cents} centavos - e e"
            " esse valor, e nao o declarado, que iguala o minimo detectavel"
            " ali. Por isso o minimo detectavel CRESCE com o horizonte e a"
            " taxa exigida CAI: sao duas leituras da mesma coisa, e a que"
            " compara entre janelas e o Sharpe anualizado."
        ),
    }


def montar(conn: sqlite3.Connection, *, potencia_ppm: int) -> dict[str, Any]:
    """A capacidade experimental do desenho vigente, com tudo derivado.

    `potencia_ppm` e **obrigatorio e sem default**, como em `dimensionar`. Um
    relatorio de viabilidade que assumisse a potencia sozinho seria o lugar
    mais provavel para o numero voltar a ser implicito.
    """
    from ..config import service as config_service

    vigente = config_service.versao_atual(conn)
    ds = loader.dataset_vigente(conn)
    if vigente is None or ds is None:
        return {
            "disponivel": False,
            "motivo": (
                "sem config vigente ou sem dataset: a capacidade experimental"
                " depende de familia, FDR e das barras que existem"
            ),
            "conclusao": None,
            # A marca vai em TODOS os caminhos de saida. A primeira versao
            # ficou so no completo, e o teste acusou: um `return` curto que
            # omite a ressalva publica numeros sem ela.
            "numeros_validados": False,
            "validacao_da_variancia": VALIDACAO_DA_VARIANCIA,
        }

    cfg = vigente.config
    conjuntos = {c.finalidade: c for c in split.ler(conn, ds.id)}
    in_sample = conjuntos.get("in_sample")
    if in_sample is None:
        return {
            "disponivel": False,
            "motivo": (
                "dataset sem divisao por finalidade: nao ha in-sample sobre o"
                " qual medir variancia e dependencia"
            ),
            "conclusao": None,
            # A marca vai em TODOS os caminhos de saida. A primeira versao
            # ficou so no completo, e o teste acusou: um `return` curto que
            # omite a ressalva publica numeros sem ela.
            "numeros_validados": False,
            "validacao_da_variancia": VALIDACAO_DA_VARIANCIA,
        }

    # Variancia e dependencia MEDIDAS, e medidas no in-sample - que e o
    # conjunto que uma hipotese usa para ser testada. Medi-las na reserva seria
    # olhar dado selado para dimensionar; medi-las na exploracao descreveria
    # outro periodo.
    retornos = loader.retornos_bps_entre(
        conn, ds.id, in_sample.from_ms, in_sample.to_ms_exclusive - 1
    )
    desvio_bps = _desvio_por_barra_bps(retornos)
    rho_ppm = poder.autocorrelacao_lag1_ppm(retornos)
    if desvio_bps <= 0:
        return {
            "disponivel": False,
            "motivo": (
                f"variancia nao medivel no in-sample ({len(retornos)} retornos):"
                " sem ela o dimensionamento seria ficcao"
            ),
            "conclusao": None,
            # A marca vai em TODOS os caminhos de saida. A primeira versao
            # ficou so no completo, e o teste acusou: um `return` curto que
            # omite a ressalva publica numeros sem ela.
            "numeros_validados": False,
            "validacao_da_variancia": VALIDACAO_DA_VARIANCIA,
        }

    capital = cfg.seed_capital_usd_cents
    comum = dict(
        dependencia_rho_ppm=rho_ppm,
        variancia_desvio_por_barra_bps=desvio_bps,
        base_de_normalizacao_cents=capital,
        duracao_barra_ms=ds.interval_ms,
        potencia_ppm=potencia_ppm,
        familia_m=cfg.familia_max_hipoteses,
        fdr_alfa_bps=cfg.fdr_alvo_bps,
        procedimento=cfg.fdr_procedimento,
    )

    # A TAXA de referencia, e ela e o insumo que faltava no relatorio.
    #
    # `efeito_minimo` e um TOTAL declarado sobre um horizonte. A conta de
    # amostra detecta a TAXA que os dois implicam - e por isso o mesmo valor em
    # centavos significa coisas diferentes em janelas diferentes.
    #
    # Sem publicar a taxa, a tabela de capacidade era ilegivel: o menor efeito
    # detectavel CRESCE com o horizonte (e um total sobre uma janela que cresceu
    # mais rapido do que a sensibilidade melhorou), e lido sozinho isso parece
    # dizer que mais dado piora a situacao.
    referencia = _taxa_de_referencia(conn, comum)
    taxa_micro = referencia["taxa_por_barra_bps_micro"] if referencia else None

    # Os horizontes, na ordem em que crescem - e cada um DECLARANDO se e dado
    # que a hipotese atual pode usar ou cenario prospectivo de capacidade.
    #
    # **A distincao e da secao 8.5.1, e ela nao e formalidade.** Somar
    # walk-forward ao in-sample para "ter mais amostra" e consumir o conjunto
    # que existe para confirmar fora da amostra; somar o dataset inteiro inclui
    # a reserva selada (uso unico por hipotese) e a exploracao que o agente JA
    # observou (D34). Nenhum dos dois e anexavel.
    escalas: list[tuple[str, int, bool, str]] = [
        (
            "in_sample",
            in_sample.bars,
            True,
            "e o conjunto da propria hipotese (secao 8.5.1): 30% do dataset,"
            " contiguo e cronologico, e o unico que ela pode usar para estimar",
        ),
        (
            "in_sample_mais_walk_forward",
            sum(
                conjuntos[f].bars
                for f in ("in_sample", "walk_forward")
                if f in conjuntos
            ),
            False,
            "CENARIO PROSPECTIVO. O walk-forward continua selado para a"
            " finalidade dele - confirmar FORA da amostra, em tres janelas"
            " (secao 14.4, criterio 5). Anexa-lo ao in-sample para ganhar"
            " amostra gastaria justamente o conjunto que valida o resultado, e"
            " a hipotese atual NAO pode faze-lo",
        ),
        (
            "dataset_inteiro",
            ds.bars,
            False,
            "CENARIO PROSPECTIVO, e o mais enganoso dos tres. Inclui a reserva"
            " selada, cujo uso e UNICO por hipotese e imposto por"
            " `UNIQUE (hypothesis_id)` (secao 8.5.1), e inclui a exploracao que"
            " o agente JA observou (D34) - reusa-la como teste devolveria a"
            " sobreposicao amostral que caiu de 100% para zero. A hipotese"
            " atual NAO pode usar nem uma das duas partes",
        ),
    ]

    horizontes = [
        {
            "horizonte": nome,
            "barras": barras,
            "cenario": "disponivel" if reutilizavel else "prospectivo",
            "reutilizavel_pela_hipotese_atual": reutilizavel,
            "por_que": por_que,
            **dimensionamento.capacidade(
                barras_disponiveis=barras,
                taxa_declarada_bps_micro=taxa_micro,
                **comum,
            ).como_dict(),
        }
        for nome, barras, reutilizavel, por_que in escalas
        if barras > 0
    ]

    # E o CRUZAMENTO: o horizonte em que a taxa declarada passaria a ser
    # detectavel. Ele fecha a leitura da tabela - sem ele, as tres linhas acima
    # mostram uma diferenca que encolhe e nunca dizem onde ela chega a zero.
    #
    # Nao existe conjunto nenhum com esse tamanho, e a linha diz isso.
    if referencia is not None:
        cruzamento = referencia["n_bruto_necessario"]
        horizontes.append(
            {
                "horizonte": "n_necessario_para_a_taxa_declarada",
                "barras": cruzamento,
                "cenario": "inexistente",
                "reutilizavel_pela_hipotese_atual": False,
                "por_que": (
                    "NAO E UM CONJUNTO: e o horizonte em que a taxa declarada"
                    " passaria a ser detectavel, e nenhum conjunto deste"
                    f" dataset tem {cruzamento} barras. A diferenca cruza zero"
                    " aqui por construcao - `n_bruto_necessario` e definido"
                    " como o `n` em que a taxa detectavel iguala a declarada"
                ),
                **dimensionamento.capacidade(
                    barras_disponiveis=cruzamento,
                    taxa_declarada_bps_micro=taxa_micro,
                    **comum,
                ).como_dict(),
            }
        )

    # E as hipoteses ja registradas, cada uma contra a regua nova. Elas NAO sao
    # redimensionadas no banco (a D48 nao e retroativa): isto e leitura, e o
    # campo `regua_aplicada` de cada linha continua dizendo sob qual ela nasceu.
    hipoteses = _hipoteses_contra_a_regua(
        conn,
        capital_cents=capital,
        desvio_bps=desvio_bps,
        rho_ppm=rho_ppm,
        potencia_ppm=potencia_ppm,
        familia_m=cfg.familia_max_hipoteses,
        fdr_alfa_bps=cfg.fdr_alvo_bps,
        procedimento=cfg.fdr_procedimento,
        duracao_barra_ms=ds.interval_ms,
        disponivel_barras=ds.bars,
    )

    alfa_ppm = dimensionamento.alfa_primeira_rejeicao_ppm(
        procedimento=cfg.fdr_procedimento,
        m=cfg.familia_max_hipoteses,
        alfa_bps=cfg.fdr_alvo_bps,
    )
    return {
        "disponivel": True,
        "motivo": None,
        "config_version_id": vigente.id,
        "insumos_medidos": {
            "dataset_id": ds.id,
            "in_sample_barras": in_sample.bars,
            "retornos_medidos": len(retornos),
            "variancia_desvio_por_barra_bps": desvio_bps,
            "dependencia_rho_ppm": rho_ppm,
            "fator_dependencia_ppm": poder.fator_ppm_de_rho(rho_ppm),
            "base_de_normalizacao_cents": capital,
            "o_que_a_base_e": (
                "BASE DE NORMALIZACAO, e nao exposicao real. Ela converte"
                " centavos em fracao, e e o `seed_capital_usd_cents` da"
                " config - fixo, declarado, igual em todos os horizontes."
                " A exposicao VERDADEIRA varia barra a barra: a regra fica"
                " fora do mercado parte do tempo, e quando esta dentro"
                " aplica `fracao_bps` sobre o caixa do momento. A base"
                " CANCELA no Sharpe anualizado (multiplica media e desvio"
                " igualmente) e so aparece na coluna de dolares."
            ),
            "duracao_barra_ms": ds.interval_ms,
        },
        "insumos_declarados": {
            "procedimento": cfg.fdr_procedimento,
            "familia_m": cfg.familia_max_hipoteses,
            "fdr_alvo_bps": cfg.fdr_alvo_bps,
            "potencia_ppm": potencia_ppm,
            "alfa_primeira_rejeicao_ppm": alfa_ppm,
            "t_exigido_micro": dimensionamento.t_exigido_micro(
                alfa_ppm=alfa_ppm, potencia_ppm=potencia_ppm
            ),
            "t_secao_8_3_micro": poder.T_ALVO * 1_000_000,
        },
        "taxa_de_referencia": referencia,
        "horizontes": horizontes,
        "conferencia_dimensional": _CONFERENCIA_DIMENSIONAL,
        "cenarios_prospectivos_nao_sao_dado_reutilizavel": (
            _PROSPECTIVO_NAO_E_REUTILIZAVEL
        ),
        "hipoteses": hipoteses,
        "decisao_de_capacidade": {
            "escolhida": dimensionamento.DECISAO_DE_CAPACIDADE_EXPERIMENTAL,
            "saidas_possiveis": list(dimensionamento.SAIDAS_DE_CAPACIDADE),
            "bloqueia": (
                "hipotese nova sob a regua da D48 - bloqueante ABSOLUTO"
            ),
            "nao_bloqueia": (
                "a construcao do relatorio da 0C, nem a leitura deste"
                " relatorio: relatar o que se mediu nao decide nada"
            ),
        },
        "validacao_da_variancia": VALIDACAO_DA_VARIANCIA,
        "numeros_validados": False,
        "conclusao": CONCLUSAO,
        "conclusao_sustentada": _sustenta(horizontes, hipoteses),
        "opcoes_prospectivas": OPCOES_PROSPECTIVAS,
        "fora_de_cogitacao": FORA_DE_COGITACAO,
        "nenhuma_opcao_escolhida": (
            "Nenhuma das opcoes acima foi escolhida. Escolher agora seria"
            " escolher olhando resultado ja produzido, e a decisao e"
            " prospectiva por construcao (secao 8.2)."
        ),
    }


def _sustenta(horizontes: list[dict], hipoteses: list[dict]) -> bool:
    """A conclusao e verdadeira NESTE banco, ou nao e.

    Derivada, e nao digitada, pelo mesmo motivo que `fecha` do relatorio da 0A:
    se um dia o dataset crescer o bastante, este campo vira `False` sozinho e a
    frase deixa de ser sustentada - em vez de continuar impressa descrevendo um
    mundo que acabou.
    """
    if not hipoteses:
        return False
    return all(h["veredito"] == dimensionamento.VEREDITO_NAO_TESTAVEL
               for h in hipoteses)


def _hipoteses_contra_a_regua(
    conn: sqlite3.Connection,
    *,
    capital_cents: int,
    desvio_bps: int,
    rho_ppm: int,
    potencia_ppm: int,
    familia_m: int,
    fdr_alfa_bps: int,
    procedimento: str,
    duracao_barra_ms: int,
    disponivel_barras: int,
) -> list[dict[str, Any]]:
    """Cada hipotese registrada, relida contra a regua da D48.

    **Leitura, nunca escrita.** A D48 nao e retroativa: nada aqui altera
    `n_minimo`, `efeito_minimo` nem `testavel` de linha nenhuma - e a tabela
    recusaria o `UPDATE` de qualquer forma, por gatilho desde a migracao 9.
    O que este bloco produz e a resposta a "e se a regua nova valesse?", que e
    exatamente a pergunta que uma decisao prospectiva precisa ter respondida.
    """
    saida: list[dict[str, Any]] = []
    for linha in conn.execute(
        "SELECT id, agente_origem, efeito_minimo, n_minimo, horizonte_barras,"
        "       sharpe_esperado_milesimos, testavel, regua_dimensionamento"
        "  FROM hypothesis ORDER BY id"
    ):
        try:
            d = dimensionamento.dimensionar(
                efeito_minimo_cents=abs(int(linha["efeito_minimo"])),
                base_de_normalizacao_cents=capital_cents,
                variancia_desvio_por_barra_bps=desvio_bps,
                dependencia_rho_ppm=rho_ppm,
                horizonte_barras=int(linha["horizonte_barras"]),
                disponivel_barras=disponivel_barras,
                potencia_ppm=potencia_ppm,
                familia_m=familia_m,
                fdr_alfa_bps=fdr_alfa_bps,
                procedimento=procedimento,
                sharpe_esperado_milesimos=int(
                    linha["sharpe_esperado_milesimos"]
                ),
                duracao_barra_ms=duracao_barra_ms,
            )
        except dimensionamento.DimensionamentoImpossivel as erro:
            saida.append(
                {
                    "hypothesis_id": int(linha["id"]),
                    "agente_origem": linha["agente_origem"],
                    "veredito": None,
                    "motivo": str(erro),
                }
            )
            continue
        saida.append(
            {
                "hypothesis_id": int(linha["id"]),
                "agente_origem": linha["agente_origem"],
                "regua_com_que_nasceu": linha["regua_dimensionamento"],
                "n_minimo_gravado": int(linha["n_minimo"]),
                "testavel_como_esta_gravado": bool(linha["testavel"]),
                "veredito": d.veredito,
                "n_bruto_necessario": d.n_bruto_necessario,
                "n_bruto_disponivel": d.n_bruto_disponivel,
                "deficit_barras": d.deficit_barras,
                "n_minimo_efetivo_pelo_sharpe": d.n_minimo_efetivo_pelo_sharpe,
                "motivo": d.motivo,
            }
        )
    return saida
