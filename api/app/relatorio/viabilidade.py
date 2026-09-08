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
        }

    capital = cfg.seed_capital_usd_cents
    comum = dict(
        dependencia_rho_ppm=rho_ppm,
        variancia_desvio_por_barra_bps=desvio_bps,
        capital_exposto_cents=capital,
        duracao_barra_ms=ds.interval_ms,
        potencia_ppm=potencia_ppm,
        familia_m=cfg.familia_max_hipoteses,
        fdr_alfa_bps=cfg.fdr_alvo_bps,
        procedimento=cfg.fdr_procedimento,
    )

    # Os horizontes que existem, na ordem em que crescem. `in_sample` e o que
    # a secao 8.5.1 da a uma hipotese; os outros estao aqui para responder
    # "e se tivessemos mais?" sem que ninguem precise consumir nada para saber.
    escalas: list[tuple[str, int]] = [
        ("in_sample", in_sample.bars),
        (
            "in_sample_mais_walk_forward",
            sum(
                conjuntos[f].bars
                for f in ("in_sample", "walk_forward")
                if f in conjuntos
            ),
        ),
        ("dataset_inteiro", ds.bars),
    ]
    horizontes = [
        {
            "horizonte": nome,
            "barras": barras,
            **dimensionamento.capacidade(
                barras_disponiveis=barras, **comum
            ).como_dict(),
        }
        for nome, barras in escalas
        if barras > 0
    ]

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
            "capital_exposto_cents": capital,
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
        "horizontes": horizontes,
        "hipoteses": hipoteses,
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
                capital_exposto_cents=capital_cents,
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
