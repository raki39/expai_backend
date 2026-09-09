"""O DOMINIO de cada metrica, e se uma clausula consegue ser as duas coisas.

> *"Cada clausula precisa ter metrica valida, unidade compativel e
> possibilidade real de ser satisfeita e nao satisfeita dentro do dominio
> permitido."* — o usuario, 2026-09-09

## O defeito que isto conserta, medido em producao

A hipotese 41 declarou `patrimonio_final_cents < 950000` com semente de
**100.000** centavos. Nove vezes e meia a semente: a clausula **dispara em
qualquer run possivel** e nunca poderia deixar de disparar. Ela entrou no
veredito com `disparou: true`, indistinguivel de uma refutacao de verdade.

Nada acusou, e a razao e dupla:

1. `_falseamento_serve_para_algo` so conferia a clausula sobre a metrica
   **primaria**. Esta era secundaria;
2. o agente declara limiar monetario em centavos **sem nunca receber a
   escala** — `prompts.py` dizia "ele comeca com um capital semente", a
   palavra e nao o numero.

E o mesmo padrao apareceu no proprio A1a: ele declarava
`idas_e_voltas > 20000` sobre um horizonte de 21.024 barras, onde o maximo
aritmetico e ~10.512. **A clausula do controle tambem nunca podia disparar.**

## Como o dominio e derivado

| metrica | dominio | de onde sai |
|---|---|---|
| `patrimonio_final_cents` | `[0, TETO x semente]` | nao ha alavancagem nem venda a descoberto no catalogo: o patrimonio nao fica negativo |
| `excesso_sobre_*_cents` | `[-TETO x semente, +TETO x semente]` | excesso e diferenca entre dois patrimonios do mesmo dominio |
| `idas_e_voltas` | `[0, horizonte // 2]` | uma ida e uma volta gastam no minimo duas barras |

`TETO_MULTIPLO_DA_SEMENTE_BPS` e uma **constante declarada com motivo**, do
mesmo tipo que `SHARPE_MAX_MILESIMOS`: acima dela nao ha hipotese honesta, ha
erro de unidade. Ela e deliberadamente **permissiva** — pega erro de ordem de
grandeza, e nao otimismo. Escolhida antes de qualquer resultado, e nao para
fazer um numero caber.
"""

from __future__ import annotations

from .schema import (
    METRICAS_FACTUAIS,
    ClausulaFalseamento,
    PreRegistroBruto,
)

#: 3x a semente. §8.3 ja poe Sharpe 3,0 como o topo do que se testa em meses;
#: triplicar o capital dentro do in-sample esta alem de qualquer declaracao
#: honesta, e um limiar acima disso so pode ser erro de unidade. Permissiva de
#: proposito: ela nao julga se a hipotese e boa, so se a clausula e legivel.
TETO_MULTIPLO_DA_SEMENTE_BPS = 30_000

#: Metricas monetarias: as que a regra 5 obriga a inteiro de centavos. O
#: complemento de `METRICAS_FACTUAIS`, e derivado dele para que acrescentar uma
#: metrica nova nao exija lembrar de duas listas - o defeito de
#: `condicoes_da_config`, que existiu duas vezes identico.
def metricas_monetarias() -> frozenset[str]:
    from .schema import METRICAS

    return frozenset(METRICAS) - METRICAS_FACTUAIS


class ClausulaNaoInformativa(Exception):
    """A clausula nao consegue ser verdadeira e falsa no dominio.

    Nao e "a hipotese e ruim": e "esta linha nao carrega informacao nenhuma", e
    publicar um `disparou` dela ao lado de um disparo real e o que faz uma
    refutacao decorativa parecer evidencia.
    """


def dominio(
    metrica: str, *, semente_cents: int, horizonte_barras: int
) -> tuple[int, int]:
    """O intervalo fechado que a metrica pode assumir. Derivado, nunca digitado."""
    teto = semente_cents * TETO_MULTIPLO_DA_SEMENTE_BPS // 10_000
    if metrica == "idas_e_voltas":
        return (0, max(0, horizonte_barras // 2))
    if metrica == "patrimonio_final_cents":
        return (0, teto)
    return (-teto, teto)


def _informativa(
    c: ClausulaFalseamento, *, minimo: int, maximo: int
) -> tuple[bool, str]:
    """A clausula consegue disparar E consegue nao disparar?

    Com `menor_que V`: dispara se existe observavel `< V` (ou seja `V > minimo`)
    e deixa de disparar se existe observavel `>= V` (ou seja `V <= maximo`).
    Com `maior_que V`, a simetrica.
    """
    if c.comparador == "menor_que":
        if c.valor <= minimo:
            return False, (
                f"`{c.como_texto()}` nunca dispara: o minimo observavel e"
                f" {minimo}, e nada pode ficar abaixo de {c.valor}"
            )
        if c.valor > maximo:
            return False, (
                f"`{c.como_texto()}` dispara SEMPRE: o maximo observavel e"
                f" {maximo}, e tudo fica abaixo de {c.valor}. Uma clausula que"
                " nao pode deixar de disparar nao refuta - ela decora"
            )
        return True, ""
    if c.valor >= maximo:
        return False, (
            f"`{c.como_texto()}` nunca dispara: o maximo observavel e"
            f" {maximo}, e nada pode passar de {c.valor}"
        )
    if c.valor < minimo:
        return False, (
            f"`{c.como_texto()}` dispara SEMPRE: o minimo observavel e"
            f" {minimo}, e tudo passa de {c.valor}"
        )
    return True, ""


def conferir(
    bruto: PreRegistroBruto, *, semente_cents: int, horizonte_barras: int
) -> None:
    """TODAS as clausulas, primarias e secundarias. Levanta na primeira ruim.

    Chamada no registro, e nao no modelo: o dominio depende da semente e do
    horizonte, que sao da config e da janela - o `PreRegistroBruto` nao os
    conhece, e passa-los para dentro dele faria o schema que vai ao provedor
    depender de estado do banco.
    """
    for c in bruto.condicoes_falseamento:
        minimo, maximo = dominio(
            c.metrica,
            semente_cents=semente_cents,
            horizonte_barras=horizonte_barras,
        )
        ok, por_que = _informativa(c, minimo=minimo, maximo=maximo)
        if not ok:
            raise ClausulaNaoInformativa(
                por_que
                + f" (dominio de `{c.metrica}`: [{minimo}, {maximo}], derivado"
                f" de semente {semente_cents} e horizonte {horizonte_barras})"
            )


def diagnosticar(
    clausulas, *, semente_cents: int, horizonte_barras: int
) -> list[dict]:
    """O mesmo julgamento, em forma de RELATORIO — para linhas ja gravadas.

    O pre-registro e imutavel (§8.2), entao uma hipotese antiga com clausula
    nao informativa nao pode ser corrigida. O que se pode e **publicar** que
    ela nao informa, para que o `disparou` dela pare de parecer evidencia.
    """
    fora = []
    for c in clausulas:
        minimo, maximo = dominio(
            c.metrica,
            semente_cents=semente_cents,
            horizonte_barras=horizonte_barras,
        )
        ok, por_que = _informativa(c, minimo=minimo, maximo=maximo)
        if not ok:
            fora.append(
                {
                    "clausula": c.como_texto(),
                    "metrica": c.metrica,
                    "por_que": por_que,
                    "dominio": [minimo, maximo],
                    "nao_fortalece_veredito": True,
                }
            )
    return fora
