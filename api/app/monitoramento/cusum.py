"""O CUSUM unilateral inferior. PURO, INTEIRO, e sem nenhuma unidade implicita.

ADR 0035. A estatistica e a de sempre:

    d_t = alvo - observado_t - k
    S_t = max(0, S_{t-1} + d_t)

e o que este modulo existe para impor sao as duas coisas que o usuario exigiu
antes de `d_t` ser congelado.

## 1. A UNIDADE, porque a mistura nao aparece como erro

A minha primeira versao da D46 escrevia o alvo como `efeito_minimo / n_minimo`.
O usuario pediu a demonstracao dimensional antes de congelar, e ela condenou a
formula:

| grandeza | unidade |
|---|---|
| `observado_t` | **milicents / barra** |
| `efeito_minimo` | **cents** (unidade da metrica primaria) |
| `horizonte_barras` | **barras** |
| `n_minimo` | **observacoes EFETIVAS** |

`app/hipotese/poder.py` e literal: `n_minimo` sao *"Observacoes EFETIVAS
necessarias para `t > 2`"*. Entao `efeito_minimo / n_minimo` da **cents por
observacao efetiva**, e o CUSUM atualiza **por barra**. Medido sobre a hipotese
41 (50.000 cents, `n_minimo` 19.240, horizonte 21.024 barras): 2,5988 contra
2,3782 cents, **+9,27%**, e US$ 17,78 de deficit inventado em 8.064 barras.

**E o modo de falha nao e o falso alarme.** A calibracao usa o MESMO alvo que a
execucao: um alvo alto produz deriva positiva sob H0, o maximo de `S` cresce
com o horizonte, e o quantil 90 sobe junto. O orcamento de 10% continua
honrado - e o limiar fica tao alto que o monitor nunca dispara. **A calibracao
absorve o erro dimensional e o converte em surdez**, calada.

Por isso o alvo se chama `alvo_milicents_por_barra` e nao `alvo`, e por isso
`n_minimo` **nao aparece neste modulo** - ha guarda de codigo varrendo.

## 2. BARRA AUSENTE NAO E OBSERVACAO ZERO

Zero e uma afirmacao sobre o desvio: com `alvo > 0` e `observado = 0`,
`d_t = alvo - k > 0` e o CUSUM **sobe**. A falta de dado empurraria para o
alarme, que e a pior direcao possivel para uma ausencia.

    barra ausente  ->  S_t = S_{t-1}   (nenhuma atualizacao)

Mesmo argumento do ADR 0026 §3b: retorno que atravessa lacuna nao e retorno de
15 minutos, e o preco nunca e interpolado.

## Aritmetica inteira, e nao e por causa da regra 5

A regra 5 fala de valores monetarios, e `S` e um acumulado de milicents - ela
se aplica. Mas o motivo forte e outro: o limiar entra num digest, e R12 exige
que ele seja identico entre maquinas. Ponto flutuante aqui erraria no ultimo
digito, onde ninguem olha.

**Milicents, e nao cents**, porque o alvo da hipotese 41 e 2,3782 cents por
barra: truncar para 2 seria um erro de 16% no alvo, o que e maior que o erro
dimensional que este modulo existe para evitar.
"""

from __future__ import annotations

from dataclasses import dataclass

# 1 cent = 1.000 milicents. O fator vive aqui, num lugar so.
MILICENTS_POR_CENT = 1_000

# Os niveis, na ordem em que sao cruzados. Fechada: um nivel novo mudaria o
# que os limiares significam, e `transicao_legal` no banco tem os estados
# correspondentes como DADO.
ALERTA = "alerta"
CRITICO = "critico"
NIVEIS: tuple[str, ...] = (ALERTA, CRITICO)


class AlvoNaoDerivavel(Exception):
    """Falta pre-registro para derivar o alvo. NAO ha alvo padrao.

    R79 e literal: *"o esperado vem do pre-registro imutavel, nunca de uma
    media movel do proprio observado"*. Sem pre-registro nao existe alvo, e
    inventar um seria escolher a regua depois de a fase existir - a quinta
    pergunta do teste de escopo responde "sim" a isso.

    E por isso o B3 nao e sujeito de monitoramento na 0C: ele nao tem
    pre-registro por decisao (ADR 0034).
    """


def alvo_por_barra(*, efeito_minimo_cents: int, horizonte_barras: int) -> int:
    """A taxa declarada, em MILICENTS POR BARRA. Os dois insumos pre-registrados.

    Arredondado para CIMA. A direcao e deliberada: alvo maior significa `d_t`
    maior, CUSUM subindo mais rapido, monitor mais sensivel. E nao afrouxa o
    orcamento de falso alarme, porque a calibracao roda ESTA MESMA funcao - o
    limiar sai calibrado com o arredondamento dentro.

    **`n_minimo` nao entra.** Ele responde a pergunta de poder da secao 8.3 -
    quanta amostra e preciso para que `t > 2` seja possivel - e nao tem nada a
    dizer sobre taxa por barra.
    """
    if horizonte_barras <= 0:
        raise AlvoNaoDerivavel(
            "horizonte pre-registrado precisa ser positivo; sem ele nao ha"
            " taxa por barra a derivar"
        )
    n = efeito_minimo_cents * MILICENTS_POR_CENT
    # ceil de divisao inteira, valido para negativo tambem.
    return -(-n // horizonte_barras)


def folga(alvo_milicents_por_barra: int) -> int:
    """`k = |alvo| / 2`, em MILICENTS POR BARRA.

    A folga do CUSUM e, por convencao da estatistica, metade do desvio que se
    quer detectar. Aqui o desvio nao e escolha: e `Delta = alvo`, a perda
    **total** da taxa declarada. Entao `k` e derivado do pre-registro como o
    alvo, e nao fixado por conveniencia - mesmo desenho da purga, que a D28
    mandou derivar do catalogo em vez de fixar em 200.

    Arredondado para BAIXO, tambem na direcao de mais sensibilidade.
    """
    return abs(alvo_milicents_por_barra) // 2


def avancar(s_milicents: int, observado_milicents: int, alvo: int, k: int) -> int:
    """A recorrencia, e ela mora AQUI e em nenhum outro lugar.

        d = alvo - observado - k
        S = max(0, S + d)

    Uma linha de tres operacoes, e ainda assim ela e uma funcao: a calibracao
    do limiar roda esta mesma recorrencia 16 milhoes de vezes, e reescreve-la
    la teria sido a forma exata do defeito que `ultimo_run_do_agente` custou -
    duas copias da mesma conta, uma delas corrigida. Medido: a chamada nao
    custa nada perto do reamostrador.

    O `max(0, .)` e o **piso**, e nao um reset. A distincao e a que o ADR 0035
    fixou: o piso esta dentro do estimador; um reset estaria fora dele.
    """
    d = s_milicents + alvo - observado_milicents - k
    return d if d > 0 else 0


def maximo_de_uma_replica(
    observados: list[int], *, alvo: int, k: int, s_inicial: int = 0
) -> int:
    """`max(S_t)` sobre uma replica INTEIRA, sem construir passo nenhum.

    E o que a calibracao precisa: a distribuicao do maximo atingido ao longo
    de TODO o horizonte (ADR 0035, ajuste 1). Construir 8.064 objetos `Passo`
    por replica, 2.000 vezes, custaria memoria por um numero que ninguem le.

    Sem lacuna de proposito: a serie que calibra e o in-sample congelado, e o
    dataset tem zero lacunas (incremento 1). Barra ausente e caso do FORWARD,
    e quem trata e `acumular`.
    """
    s = s_inicial
    maximo = s
    for obs in observados:
        s = avancar(s, obs, alvo, k)
        if s > maximo:
            maximo = s
    return maximo


@dataclass(frozen=True)
class Passo:
    t_ms: int
    observado_milicents: int | None
    d_milicents: int | None
    s_milicents: int
    atualizou: bool


@dataclass(frozen=True)
class Serie:
    passos: tuple[Passo, ...]
    maximo_milicents: int
    barras_puladas: int
    maior_lacuna: int

    @property
    def avaliadas(self) -> int:
        return len(self.passos)

    @property
    def atualizadas(self) -> int:
        return sum(1 for p in self.passos if p.atualizou)

    @property
    def cobertura_ppm(self) -> int:
        """Fracao das barras avaliadas que tinham dado, em ppm."""
        if not self.passos:
            return 0
        return self.atualizadas * 1_000_000 // len(self.passos)


def acumular(
    observados: list[tuple[int, int | None]],
    *,
    alvo_milicents_por_barra: int,
    folga_milicents_por_barra: int,
    s_inicial_milicents: int = 0,
) -> Serie:
    """Roda o CUSUM sobre `(t_ms, observado)`, em ordem. `None` = barra ausente.

    `s_inicial` existe para continuar uma serie ja acumulada - o monitor roda
    uma vez por barra fechada, e reprocessar o historico inteiro a cada volta
    daria o mesmo numero por um caminho mais caro. Ele **nao** e um reset:
    reset e comecar de zero de novo, e a unica coisa que zera `S` aqui e o
    piso `max(0, .)`, que e parte da definicao da estatistica.
    """
    if s_inicial_milicents < 0:
        raise ValueError("S nao pode comecar negativo: o piso e zero")

    s = s_inicial_milicents
    passos: list[Passo] = []
    maximo = s
    puladas = 0
    lacuna = 0
    maior_lacuna = 0

    for t_ms, obs in observados:
        if obs is None:
            # Nenhuma atualizacao. Nao e zero: zero afirmaria que o desvio
            # daquela barra foi observado e valeu o alvo inteiro.
            puladas += 1
            lacuna += 1
            maior_lacuna = max(maior_lacuna, lacuna)
            passos.append(Passo(t_ms, None, None, s, False))
            continue

        lacuna = 0
        d = alvo_milicents_por_barra - obs - folga_milicents_por_barra
        s = avancar(s, obs, alvo_milicents_por_barra, folga_milicents_por_barra)
        maximo = max(maximo, s)
        passos.append(Passo(t_ms, obs, d, s, True))

    return Serie(
        passos=tuple(passos),
        maximo_milicents=maximo,
        barras_puladas=puladas,
        maior_lacuna=maior_lacuna,
    )


def cruzamentos(serie: Serie, *, alerta: int, critico: int) -> list[tuple[str, Passo]]:
    """O PRIMEIRO passo em que cada nivel foi cruzado, na ordem dos niveis.

    Um salto grande cruza os dois na **mesma barra**, e nesse caso os dois
    saem - alerta primeiro. Cruzar o critico sem ter o alerta registrado seria
    um historico que afirma o impossivel, e o banco tambem recusa (gatilho
    `monitor_critico_exige_alerta`).

    O latch nao esta aqui: ele esta no `UNIQUE (assunto, nivel)`. Esta funcao
    diz onde cruzou; quem grava e quem garante que o primeiro vence.
    """
    if critico < alerta:
        raise ValueError(
            f"limiares nao aninhados: critico {critico} < alerta {alerta}."
            " Sao quantis da mesma distribuicao do maximo, e quantil e"
            " monotono - se estes dois vieram invertidos, a calibracao nao foi"
            " a do ADR 0035"
        )

    achados: list[tuple[str, Passo]] = []
    for nivel, limiar in ((ALERTA, alerta), (CRITICO, critico)):
        for p in serie.passos:
            if p.s_milicents >= limiar:
                achados.append((nivel, p))
                break
    return achados
