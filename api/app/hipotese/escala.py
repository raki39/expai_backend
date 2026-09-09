"""Duas perguntas diferentes sobre uma cláusula, e elas não podem se misturar.

> *"A ausência de alavancagem e venda a descoberto não limita
> `patrimonio_final_cents` a 3 × semente. Uma posição long pode ultrapassar
> esse valor se o ativo valorizar mais de 3×, e operações sucessivas podem
> compor ganhos. Portanto, `[0, 3× semente]` só pode ser chamado de domínio
> observável se existir uma trava real do simulador ou uma derivação sobre o
> caminho de preços que prove esse máximo."* — o usuário, 2026-09-09

**Ele está certo, e a primeira versão deste módulo estava errada.** Ela
publicava `[0, 3 × semente]` como *domínio observável* e dizia que a cláusula
da hipótese 41 *"dispara SEMPRE"*. Nada prova isso: se o ativo triplicar, o
patrimônio passa de 3× sem alavancagem nenhuma, e ganhos sucessivos compõem.
**Eu publiquei como impossibilidade o que é política** — a forma exata do
padrão que este projeto conta, cometida no módulo escrito para pegá-lo.

Agora são dois conceitos, e o relatório diz qual está falando:

| | o que é | o que autoriza afirmar |
|---|---|---|
| `dominio_estrutural` | limite **imposto** pelo simulador ou pela aritmética | *"esta cláusula nunca dispara"* / *"dispara sempre"* |
| `faixa_economica_admissivel` | **política** pré-declarada, para recusar cláusula irrelevante à escala | *"está fora da faixa que declaramos admitir"* — e nada além disso |

A diferença não é cosmética. Dizer *"dispara sempre"* é afirmação sobre o
mundo; dizer *"fora da faixa admissível"* é afirmação sobre a nossa régua. A
primeira precisa de prova. A segunda precisa de justificativa e de ter sido
declarada antes.

---

## Os domínios ESTRUTURAIS, com a prova de cada um

### `idas_e_voltas` ∈ `[0, ceil(barras / 2)]` — **provado**

`executor.idas_e_voltas` conta **compras** (`side = 'compra'`), e não metade das
execuções — a D1 fixou long/flat, então cada compra abre exatamente uma ida e
volta. O limite sai de duas propriedades do laço de `executor.rodar`:

1. **No máximo uma execução por barra de decisão.** O laço é
   `for i in range(ultima_decidivel + 1)`, uma iteração por barra. Dentro dela,
   a venda por stop faz `continue`, e o resto é `if ENTRAR ... elif SAIR` —
   ramos exclusivos. Nenhum caminho executa duas vezes na mesma iteração;
2. **compras e vendas alternam estritamente.** `comprar` só roda com
   `not aberta` e põe `aberta = True`; só `vender` devolve `False`. Então entre
   duas compras existe pelo menos uma barra com venda.

De (1) e (2): em `B` barras decidíveis cabem no máximo `ceil(B / 2)` compras —
a sequência mais densa é `C, V, C, V, …`, que começa e pode terminar em compra.

**`ceil` e não `floor`**: em 5 barras cabem 3 compras (`C V C V C`), e a
primeira versão deste módulo usava `B // 2` = 2. Ela **recusaria uma cláusula
legítima** em horizonte ímpar.

E `B ≤ horizonte`, porque o laço para em `ultima_decidivel`, que desconta a
janela mínima da regra e a latência. Usar o horizonte inteiro dá um limite
**mais frouxo** que o verdadeiro, e essa é a direção certa: um domínio
super-estimado só recusa o que está com certeza fora.

### `patrimonio_final_cents` ∈ `[0, +∞)` — **metade provada, metade não**

O piso é estrutural: long/flat sem alavancagem e sem venda a descoberto, o
caixa nunca fica negativo, e o executor **fecha a posição no fim** — então o
valor final é caixa, e caixa não é negativo.

**O teto não existe.** Ele depende do caminho de preços, que não é nosso, e
qualquer limite derivado da janela observada seria (a) dado do in-sample
entrando numa conferência de pré-registro e (b) inválido para qualquer outra
janela. Fica `None`, e `None` aqui significa *"não há limite a afirmar"*.

### `excesso_sobre_*_cents` ∈ `(-∞, +∞)` — **nada provado**

Excesso é diferença entre dois patrimônios, os dois sem teto. O piso também
não é zero: perder para o baseline é o resultado normal.

---

## A FAIXA ECONÔMICA, e por que 3×

Política, declarada **antes** de qualquer resultado, e permissiva de propósito:
ela não julga se a hipótese é boa, só se a cláusula fala da escala deste
experimento. O número tem o mesmo tipo de justificativa que
`SHARPE_MAX_MILESIMOS = 5000`, que existe porque acima dele *"não há hipótese
honesta em mercado líquido, há erro de unidade"*:

- o capital semente é **US$ 1.000** e o horizonte in-sample é **0,6 ano**;
- §8.3 já põe Sharpe 3,0 como o topo do que se testa em meses;
- uma hipótese que declare importar-se com **triplicar** o capital nesse
  horizonte não está descrevendo um edge de mercado líquido;
- e o que se quer pegar é **erro de ordem de grandeza** — o caso real foi 9,5×
  a semente, que é um `× 10` de unidade.

Ela é frouxa por escolha. Se um dia recusar uma hipótese que alguém quis
declarar de verdade, o número está errado e muda por decisão registrada — não
por um `if` que apareceu.
"""

from __future__ import annotations

from .schema import (
    METRICAS_FACTUAIS,
    ClausulaFalseamento,
    PreRegistroBruto,
)

#: A faixa econômica admissível, em bps da semente. **Política, não limite.**
#: Ver a justificativa no docstring do módulo.
FAIXA_ECONOMICA_BPS = 30_000

#: Por que este número, junto do número — para que ninguém o leia como se
#: fosse derivado de alguma coisa.
POR_QUE_A_FAIXA = (
    "POLITICA pre-declarada, e nao limite estrutural: 3x o capital semente."
    " Semente de US$ 1.000 e in-sample de 0,6 ano; a secao 8.3 ja poe Sharpe"
    " 3,0 como o topo do que se testa em meses, e uma hipotese que declare"
    " importar-se com TRIPLICAR o capital nesse horizonte nao descreve edge de"
    " mercado liquido. Frouxa de proposito: o que ela pega e erro de ORDEM DE"
    " GRANDEZA - o caso real foi 9,5x a semente, um `x 10` de unidade. Acima"
    " dela nada e impossivel; e apenas irrelevante para a escala deste"
    " experimento"
)


class ClausulaNaoInformativa(ValueError):
    """A cláusula não consegue disparar, ou não consegue deixar de disparar.

    Afirmação sobre o **mundo**, e ela só é levantada com o domínio estrutural
    na mão — nunca a partir da faixa de política.
    """


class ForaDaEscalaEconomica(ValueError):
    """A cláusula é legível, e fala de uma escala que não é a do experimento.

    Afirmação sobre a **nossa régua**. Não diz que o valor é impossível: diz
    que declaramos, antes de olhar resultado, não admitir cláusula ali.
    """


def metricas_monetarias() -> frozenset[str]:
    """As que a regra 5 obriga a inteiro de centavos.

    Derivado de `METRICAS_FACTUAIS` para que acrescentar uma métrica não exija
    lembrar de duas listas — o defeito de `condicoes_da_config`, que existiu
    duas vezes idêntico.
    """
    from .schema import METRICAS

    return frozenset(METRICAS) - METRICAS_FACTUAIS


def dominio_estrutural(
    metrica: str, *, horizonte_barras: int
) -> tuple[int | None, int | None]:
    """O que o simulador IMPÕE. `None` em qualquer lado = sem limite a afirmar.

    Não recebe a semente de propósito: nenhum limite estrutural depende dela.
    Foi assumir que dependia que produziu o defeito de 2026-09-09.
    """
    if metrica == "idas_e_voltas":
        # ceil(B / 2). A prova está no docstring do módulo.
        return (0, max(0, (horizonte_barras + 1) // 2))
    if metrica == "patrimonio_final_cents":
        # Piso estrutural; teto inexistente (depende do caminho de precos).
        return (0, None)
    # Excesso: diferenca de dois patrimonios sem teto, e perder e normal.
    return (None, None)


def faixa_economica(
    metrica: str, *, semente_cents: int
) -> tuple[int | None, int | None]:
    """A POLÍTICA. `None` = a política não fala desta métrica.

    `idas_e_voltas` fica de fora: o limite dela já é estrutural e exato, e uma
    política por cima seria uma segunda régua sobre a mesma coisa.
    """
    if metrica == "idas_e_voltas":
        return (None, None)
    teto = semente_cents * FAIXA_ECONOMICA_BPS // 10_000
    if metrica == "patrimonio_final_cents":
        return (0, teto)
    return (-teto, teto)


def _fora_do_estrutural(
    c: ClausulaFalseamento, *, minimo: int | None, maximo: int | None
) -> str:
    """A cláusula não consegue ser as duas coisas? Só com limite PROVADO.

    Devolve o motivo, ou `""`. Um lado `None` nunca produz veredito: sem limite
    provado não há afirmação a fazer.
    """
    if c.comparador == "menor_que":
        if minimo is not None and c.valor <= minimo:
            return (
                f"`{c.como_texto()}` NUNCA dispara: o minimo estrutural e"
                f" {minimo}, e nada pode ficar abaixo de {c.valor}"
            )
        if maximo is not None and c.valor > maximo:
            return (
                f"`{c.como_texto()}` dispara SEMPRE: o maximo estrutural e"
                f" {maximo}, e tudo fica abaixo de {c.valor}. Uma clausula que"
                " nao pode deixar de disparar nao refuta - ela decora"
            )
        return ""
    if maximo is not None and c.valor >= maximo:
        return (
            f"`{c.como_texto()}` NUNCA dispara: o maximo estrutural e"
            f" {maximo}, e nada pode passar de {c.valor}"
        )
    if minimo is not None and c.valor < minimo:
        return (
            f"`{c.como_texto()}` dispara SEMPRE: o minimo estrutural e"
            f" {minimo}, e tudo passa de {c.valor}"
        )
    return ""


def _fora_da_faixa(
    c: ClausulaFalseamento, *, minimo: int | None, maximo: int | None
) -> str:
    """O limiar está dentro da faixa de política? Nada é afirmado além disso."""
    if minimo is not None and c.valor < minimo:
        return (
            f"`{c.como_texto()}` fica abaixo da faixa economica admissivel"
            f" ({minimo}). {POR_QUE_A_FAIXA}"
        )
    if maximo is not None and c.valor > maximo:
        return (
            f"`{c.como_texto()}` fica acima da faixa economica admissivel"
            f" ({maximo}). {POR_QUE_A_FAIXA}"
        )
    return ""


def _unidade(c: ClausulaFalseamento) -> str:
    """A métrica e a unidade do limiar combinam? Não depende de estado."""
    monetaria = c.metrica not in METRICAS_FACTUAIS
    if monetaria and c.valor_bps_da_semente is None:
        return (
            f"a clausula sobre '{c.metrica}' declara limiar monetario absoluto"
            f" ({c.valor}) sem referencia de escala. Metrica monetaria e"
            " declarada em `valor_bps_da_semente`, e o sistema converte: um"
            " numero em centavos sozinho nao diz se e um decimo da semente ou"
            " dez vezes ela"
        )
    if not monetaria and c.valor_bps_da_semente is not None:
        return (
            f"a clausula sobre '{c.metrica}' e uma CONTAGEM e nao tem semente a"
            " que se referir; `valor_bps_da_semente` so vale para metrica"
            " monetaria"
        )
    if c.valor < 0 and c.metrica == "patrimonio_final_cents":
        return (
            "patrimonio nao fica negativo: long/flat sem alavancagem e sem"
            " venda a descoberto, e o executor fecha a posicao no fim - um"
            " limiar negativo descreve um estado que o simulador nao produz"
        )
    return ""


def conferir(
    bruto: PreRegistroBruto, *, semente_cents: int, horizonte_barras: int
) -> None:
    """TODAS as cláusulas, primárias e secundárias. Roda no REGISTRO.

    Levanta **duas exceções diferentes**, e a distinção é o ponto do módulo:
    `ClausulaNaoInformativa` é afirmação sobre o mundo e exige limite provado;
    `ForaDaEscalaEconomica` é afirmação sobre a nossa régua.

    Aqui, e não no `model_validator`, por **não-retroatividade**: o modelo é
    reconstruído a partir do JSON gravado, e o pré-registro é imutável (§8.2) —
    exigir no modelo torna todo veredito antigo impossível de reler. Custou
    três rotas em 500 antes de eu aplicar a lição que a migração 29 já tinha.
    """
    for c in bruto.condicoes_falseamento:
        erro = _unidade(c)
        if erro:
            raise ClausulaNaoInformativa(erro)

        e_min, e_max = dominio_estrutural(
            c.metrica, horizonte_barras=horizonte_barras
        )
        erro = _fora_do_estrutural(c, minimo=e_min, maximo=e_max)
        if erro:
            raise ClausulaNaoInformativa(
                f"{erro} (dominio ESTRUTURAL de `{c.metrica}`:"
                f" [{e_min}, {e_max}], imposto pelo simulador)"
            )

        f_min, f_max = faixa_economica(c.metrica, semente_cents=semente_cents)
        erro = _fora_da_faixa(c, minimo=f_min, maximo=f_max)
        if erro:
            raise ForaDaEscalaEconomica(erro)


def diagnosticar(
    clausulas, *, semente_cents: int, horizonte_barras: int
) -> list[dict]:
    """O mesmo julgamento em forma de RELATÓRIO, para linhas já gravadas.

    O pré-registro é imutável, então uma cláusula antiga fora de escala não
    pode ser corrigida. O que se pode é **publicar** o que ela é — e cada linha
    diz de qual das duas perguntas está falando, porque `nao_informativa` e
    `fora_da_escala_economica` autorizam frases diferentes.
    """
    fora = []
    for c in clausulas:
        e_min, e_max = dominio_estrutural(
            c.metrica, horizonte_barras=horizonte_barras
        )
        f_min, f_max = faixa_economica(c.metrica, semente_cents=semente_cents)

        motivo_estrutural = _fora_do_estrutural(c, minimo=e_min, maximo=e_max)
        motivo_faixa = _fora_da_faixa(c, minimo=f_min, maximo=f_max)
        if not motivo_estrutural and not motivo_faixa:
            continue

        fora.append(
            {
                "clausula": c.como_texto(),
                "metrica": c.metrica,
                # QUAL das duas coisas ela e. Nunca as duas ao mesmo tempo na
                # mesma frase: uma afirma sobre o mundo, a outra sobre a regua.
                "classificacao": (
                    "nao_informativa"
                    if motivo_estrutural
                    else "fora_da_escala_economica"
                ),
                "por_que": motivo_estrutural or motivo_faixa,
                "dominio_estrutural": [e_min, e_max],
                "faixa_economica_admissivel": [f_min, f_max],
                "o_que_isso_NAO_afirma": (
                    None
                    if motivo_estrutural
                    else (
                        "NAO afirma que o valor e impossivel nem que a clausula"
                        " dispara sempre. O teto estrutural de"
                        f" `{c.metrica}` nao existe: ele depende do caminho de"
                        " precos, e uma posicao long pode ultrapassar qualquer"
                        " multiplo da semente se o ativo valorizar. O que se"
                        " afirma e que o limiar esta fora da faixa que este"
                        " experimento declarou admitir"
                    )
                ),
                "nao_fortalece_veredito": True,
            }
        )
    return fora
