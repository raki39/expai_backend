"""O veredito sobre a hipotese: derivado, nunca escrito (R33, R51).

A 0A fechou com `veredito_da_expectativa = None` e o motivo por extenso ao
lado: a expectativa era texto livre, e julgar se ela se cumpriu exigiria nova
inferencia. Aquele `None` era a resposta certa para o campo que existia.

Com o pre-registro da secao 8.2 o campo e outro. `efeito_minimo` e
`condicoes_falseamento` foram declarados antes de qualquer execucao,
estruturados, e o veredito e uma comparacao entre eles e o que ficou gravado.

## Tres valores, e o `None` continua existindo

Secao 14.4 e explicita em que sao **tres resultados, nao dois**:

A ORDEM em que os ramos sao avaliados e o desenho, e nao detalhe:

| # | Veredito | Condicao |
|---|---|---|
| 1 | `None` | alguma condicao declarada nao pode ser conferida neste run |
| 2 | `refutada` | disparou uma clausula sobre metrica **factual** - fato nao depende de amostra |
| 3 | `inconclusiva` | `n_efetivo` nao alcancou `n_minimo` |
| 4 | `refutada` | disparou uma clausula **estatistica**, ja com amostra suficiente |
| 5 | `sustentada` | nada disparou, amostra suficiente, efeito >= minimo |
| 6 | `refutada` | nada disparou, amostra suficiente, efeito abaixo do minimo |

**O passo 3 vir antes do 4 e o que mantem a R51 viva.** A clausula obrigatoria
sobre a metrica primaria e sempre "efeito < minimo" (o schema exige). Se ela
fosse avaliada antes da amostra, toda hipotese de efeito baixo sairia
`refutada` e `inconclusiva` seria um ramo INALCANCAVEL - a distincao existiria
no codigo e nao no comportamento. Foi assim que este defeito apareceu: um
teste do proprio ramo nao conseguia alcanca-lo.

E `None` segue disponivel, porque continua havendo o caso em que nao se sabe:
uma metrica citada pelo pre-registro que nao pode ser observada neste run.
`None` nao e `False` - a 0A ja registrou isso no relatorio, e a regra vale
igual aqui.

**Nao alcancar amostra nao e falha.** Secao 14.4: "Tratar os dois como a mesma
coisa faria o projeto descartar abordagens por impaciencia, que e o erro
simetrico ao de promover ruido."

## Por que uma clausula nao conferida derruba o veredito inteiro

Se uma das condicoes de falseamento incide sobre metrica que este run nao pode
observar, o veredito e `None` - nunca `sustentada`.

Dizer "sustentada" tendo deixado de conferir uma condicao de refutacao
declarada seria afirmar mais do que se apurou, e por um caminho que nao
apareceria em lugar nenhum: o veredito sairia positivo e a clausula
silenciosamente ignorada. E a forma exata do defeito que este projeto ja
encontrou sete vezes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .schema import METRICAS_FACTUAIS, ClausulaFalseamento, PreRegistroBruto

VEREDITOS = ("sustentada", "refutada", "inconclusiva")


@dataclass(frozen=True)
class Realizado:
    """O que este run observou, por metrica. `None` e 'nao sei', com motivo."""

    valores: dict[str, int] = field(default_factory=dict)
    indisponiveis: dict[str, str] = field(default_factory=dict)

    def de(self, metrica: str) -> int | None:
        return self.valores.get(metrica)

    def por_que_falta(self, metrica: str) -> str:
        return self.indisponiveis.get(metrica, "metrica nao observada")


def observar(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    patrimonio_cents: int,
    idas_e_voltas: int,
    b1_casado: dict | None,
    baselines_do_recorte: dict[str, int] | None = None,
) -> Realizado:
    """Monta o observado deste run, dizendo o que faltou e por que.

    B2 e B3 vivem em runs proprios (D19 e o desenho da comparacao da 0A), e
    so entram aqui quando foram produzidos sob a MESMA `config_version` deste
    run. Comparar contra um baseline de outra config e comparar atravessando
    uma mudanca material, que a secao 10.2.3 invalida - e faze-lo em silencio
    seria pior que nao comparar.

    `baselines_do_recorte` existe para o **walk-forward** (§14.4, criterio
    B5), onde "o B2 e o B3 deste periodo" nao sao os da comparacao: cada
    janela de teste tem os seus, rodados sobre o mesmo recorte. Passa-los aqui
    mantem UMA definicao do observado - a alternativa era um segundo `observar`
    dentro do validador, e duas construcoes do mesmo objeto divergem.

    Quando ele vem, a busca global nao acontece: o chamador ja disse quais
    runs valem, e procurar por cima seria ignorar o recorte em silencio.
    """
    valores: dict[str, int] = {
        "patrimonio_final_cents": patrimonio_cents,
        "idas_e_voltas": idas_e_voltas,
    }
    indisponiveis: dict[str, str] = {}

    if b1_casado is not None:
        valores["excesso_sobre_b1_p50_cents"] = (
            patrimonio_cents - int(b1_casado["p50"])
        )
    else:
        indisponiveis["excesso_sobre_b1_p50_cents"] = (
            "o B1 casado nao rodou neste run; sem a distribuicao do acaso com"
            " o mesmo giro nao ha contra o que medir excesso (D19)"
        )

    minha_config = conn.execute(
        "SELECT config_version_id FROM run WHERE id = ?", (run_id,)
    ).fetchone()
    minha = int(minha_config["config_version_id"]) if minha_config else None

    for metrica, marcador, nome in (
        ("excesso_sobre_b2_cents", "baseline-B2", "B2"),
        ("excesso_sobre_b3_cents", "baseline-B3", "B3"),
    ):
        if baselines_do_recorte is not None:
            do_recorte = baselines_do_recorte.get(nome)
            if do_recorte is None:
                indisponiveis[metrica] = (
                    f"{nome} nao foi rodado sobre este recorte; o baseline da"
                    " comparacao e de outro periodo, e usa-lo aqui compararia"
                    " a janela de teste contra o desempenho de outra janela"
                )
                continue
            from ..simulador import execucao as simulador

            valores[metrica] = patrimonio_cents - simulador.caixa_cents(
                conn, int(do_recorte)
            )
            continue
        linha = conn.execute(
            "SELECT id, config_version_id FROM run"
            " WHERE agent_id = ? ORDER BY id DESC LIMIT 1",
            (marcador,),
        ).fetchone()
        if linha is None:
            indisponiveis[metrica] = (
                f"{nome} nao foi executado; a comparacao completa nao rodou"
            )
            continue
        if minha is not None and int(linha["config_version_id"]) != minha:
            indisponiveis[metrica] = (
                f"{nome} foi produzido sob config_version"
                f" {int(linha['config_version_id'])} e este run esta na"
                f" {minha}; comparar atravessaria uma mudanca material"
                " (secao 10.2.3)"
            )
            continue
        # Import tardio: `veredito` nao pode arrastar as maos rapidas para
        # dentro de si em tempo de importacao - a fronteira de §3.2 e
        # verificada por AST e nao distingue intencao.
        from ..simulador import execucao as simulador

        valores[metrica] = patrimonio_cents - simulador.caixa_cents(
            conn, int(linha["id"])
        )

    return Realizado(valores=valores, indisponiveis=indisponiveis)


@dataclass(frozen=True)
class ClausulaConferida:
    clausula: ClausulaFalseamento
    observado: int | None
    disparou: bool | None
    por_que_nao_conferida: str | None = None

    def como_dict(self) -> dict:
        return {
            "condicao": self.clausula.como_texto(),
            "metrica": self.clausula.metrica,
            "comparador": self.clausula.comparador,
            "valor": self.clausula.valor,
            "observado": self.observado,
            "disparou": self.disparou,
            "por_que_nao_conferida": self.por_que_nao_conferida,
        }


@dataclass(frozen=True)
class Veredito:
    veredito: str | None
    motivo: str
    clausulas: list[ClausulaConferida]
    efeito_observado: int | None
    efeito_minimo: int
    n_efetivo: int
    n_minimo: int
    amostra_suficiente: bool

    def como_dict(self) -> dict:
        return {
            "veredito": self.veredito,
            "motivo": self.motivo,
            "condicoes_falseamento": [c.como_dict() for c in self.clausulas],
            "efeito": {
                "observado": self.efeito_observado,
                "minimo_declarado": self.efeito_minimo,
                "alcancou": (
                    None
                    if self.efeito_observado is None
                    else self.efeito_observado >= self.efeito_minimo
                ),
            },
            "amostra": {
                "n_efetivo": self.n_efetivo,
                "n_minimo": self.n_minimo,
                "suficiente": self.amostra_suficiente,
            },
        }


def emitir(
    pre: PreRegistroBruto,
    realizado: Realizado,
    *,
    n_efetivo: int,
    n_minimo: int,
) -> Veredito:
    """Deriva o veredito. Nenhum ramo aqui escreve uma conclusao a mao."""
    clausulas: list[ClausulaConferida] = []
    nao_conferidas: list[str] = []
    for c in pre.condicoes_falseamento:
        observado = realizado.de(c.metrica)
        if observado is None:
            motivo = realizado.por_que_falta(c.metrica)
            nao_conferidas.append(f"{c.como_texto()} ({motivo})")
            clausulas.append(
                ClausulaConferida(
                    clausula=c,
                    observado=None,
                    disparou=None,
                    por_que_nao_conferida=motivo,
                )
            )
            continue
        clausulas.append(
            ClausulaConferida(
                clausula=c, observado=observado, disparou=c.disparou(observado)
            )
        )

    efeito = realizado.de(pre.metrica_primaria)
    amostra_ok = n_efetivo >= n_minimo

    base = dict(
        clausulas=clausulas,
        efeito_observado=efeito,
        efeito_minimo=pre.efeito_minimo,
        n_efetivo=n_efetivo,
        n_minimo=n_minimo,
        amostra_suficiente=amostra_ok,
    )

    # 1. Nao sei. Uma condicao de refutacao que ficou sem conferir impede
    #    qualquer veredito - inclusive o negativo, porque "refutada por outra
    #    clausula" tambem afirmaria ter conferido o conjunto.
    if nao_conferidas:
        return Veredito(
            veredito=None,
            motivo=(
                "condicao de falseamento declarada que este run nao pode"
                " conferir: " + "; ".join(nao_conferidas)
            ),
            **base,
        )
    if efeito is None:
        return Veredito(
            veredito=None,
            motivo=(
                f"a metrica primaria '{pre.metrica_primaria}' nao foi"
                f" observada: {realizado.por_que_falta(pre.metrica_primaria)}"
            ),
            **base,
        )

    def _refutada(disparadas: list[ClausulaConferida], porque: str) -> Veredito:
        return Veredito(
            veredito="refutada",
            motivo=(
                porque
                + ": "
                + "; ".join(
                    f"{c.clausula.como_texto()} (observado {c.observado})"
                    for c in disparadas
                )
            ),
            **base,
        )

    # 2. Refutada por FATO. Contagem nao e estimativa: "operou 5.000 vezes"
    #    e verdade com uma execucao ou com mil, e nao ha amostra que a torne
    #    mais ou menos verdadeira. Por isso este ramo vem antes da amostra.
    factuais = [
        c for c in clausulas
        if c.disparou and c.clausula.metrica in METRICAS_FACTUAIS
    ]
    if factuais:
        return _refutada(
            factuais,
            "condicao de falseamento sobre um FATO observado foi satisfeita,"
            " e fato nao depende de amostra",
        )

    # 3. Inconclusiva. Secao 14.4: falta de amostra nao e falha, e trata-la
    #    como falha e descartar abordagem por impaciencia.
    #
    #    Vem ANTES das clausulas estatisticas de proposito. A clausula
    #    obrigatoria sobre a metrica primaria e sempre "efeito < minimo": se
    #    ela disparasse aqui, toda hipotese com efeito baixo sairia
    #    `refutada` e este ramo nunca seria alcancado. Seria a R51 presente no
    #    codigo e ausente no comportamento.
    if not amostra_ok:
        return Veredito(
            veredito="inconclusiva",
            motivo=(
                f"n_efetivo de {n_efetivo} nao alcancou o n_minimo de"
                f" {n_minimo} declarado no pre-registro; nem promove nem"
                " descarta, e nao pode ser citada como evidencia de sucesso"
            ),
            **base,
        )

    # 4. Refutada por clausula ESTATISTICA, ja com amostra suficiente.
    dispararam = [c for c in clausulas if c.disparou]
    if dispararam:
        return _refutada(
            dispararam,
            "condicao de falseamento declarada no pre-registro foi observada,"
            " com amostra suficiente para afirmar",
        )

    # 5. Nada disparou e a amostra chegou: decide o efeito.
    if efeito >= pre.efeito_minimo:
        return Veredito(
            veredito="sustentada",
            motivo=(
                f"nenhuma condicao de falseamento disparou, e o efeito"
                f" observado ({efeito}) alcancou o minimo declarado"
                f" ({pre.efeito_minimo}) com amostra suficiente"
            ),
            **base,
        )
    return Veredito(
        veredito="refutada",
        motivo=(
            f"efeito observado ({efeito}) ficou abaixo do minimo que a"
            f" propria hipotese declarou importar ({pre.efeito_minimo}), com"
            " amostra suficiente para afirmar"
        ),
        **base,
    )
