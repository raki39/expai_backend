"""O prefixo cacheavel e as mensagens dos dois nos que chamam o modelo.

**O bloco de sistema e byte a byte identico em toda chamada de todo run.** E
o que torna o criterio 6 possivel: cache de prompt e casamento de prefixo, e
qualquer coisa que varie ali - um horario, um id, um numero vindo da config -
invalida tudo o que vem depois e o `cache_read_input_tokens` volta a zero sem
nenhum erro aparente. Por isso o que varia (o resumo do periodo, os custos
declarados, a interpretacao da etapa anterior) vive nas MENSAGENS, nunca aqui.

As faixas dos parametros sao **lidas do schema do catalogo**, nao escritas de
novo. Se alguem alargar `rapida` para 300 no `regra/schema.py`, o prompt passa
a dizer 300 sozinho. Repetir a faixa a mao criaria a quinta ocorrencia do
padrao que este projeto ja registrou quatro vezes: um texto que descrevia
outra coisa, parou de descrever, e nada acusou.
"""

from __future__ import annotations

from typing import Any

from ..regra.schema import BandaDesvio, BreakoutCanal, CruzamentoMedias, Regra
from .contexto import ResumoDePeriodo
from .contrato import Interpretacao

# O cache de prompt tem um minimo de tokens abaixo do qual o provedor nem
# tenta gravar (o numero exato e por modelo, e vive no adaptador). Se o
# prefixo ficar abaixo disso, `cache_read_input_tokens` fica zero para sempre
# e o criterio 6 falha por um motivo que nao e defeito de invalidacao - o que
# e a pior forma de falhar, porque manda procurar no lugar errado.
#
# A conta e deliberadamente conservadora: 4 caracteres por token (o real fica
# perto de 3,5 em portugues) e uma folga de 30% sobre o minimo. Subestimar a
# margem aqui nao custa nada; superestimar custa uma investigacao inteira.
CHARS_POR_TOKEN_CONSERVADOR = 4
MINIMO_DE_TOKENS_DO_CACHE = 1_024
FOLGA_MILESIMOS = 1_300
MINIMO_CACHEAVEL_CHARS = (
    MINIMO_DE_TOKENS_DO_CACHE
    * CHARS_POR_TOKEN_CONSERVADOR
    * FOLGA_MILESIMOS
    // 1_000
)


def _faixa(modelo: Any, campo: str) -> tuple[int, int]:
    """Minimo e maximo declarados no schema do catalogo.

    Campo nulavel vira `anyOf` no JSON Schema; a faixa esta no ramo inteiro.
    Levanta se nao houver faixa nenhuma - um parametro sem limite chegando
    ao prompt sem ninguem notar seria pior que quebrar aqui.
    """
    propriedade = modelo.model_json_schema()["properties"][campo]
    ramos = propriedade.get("anyOf", [propriedade])
    for ramo in ramos:
        if "minimum" in ramo and "maximum" in ramo:
            return ramo["minimum"], ramo["maximum"]
    raise ValueError(f"{modelo.__name__}.{campo} nao declara faixa no schema")


def _catalogo() -> str:
    rapida = _faixa(CruzamentoMedias, "rapida")
    lenta = _faixa(CruzamentoMedias, "lenta")
    periodo_banda = _faixa(BandaDesvio, "periodo")
    desvios = _faixa(BandaDesvio, "desvios_milesimos")
    periodo_canal = _faixa(BreakoutCanal, "periodo")
    fracao = _faixa(Regra, "position_fraction_bps")
    stop = _faixa(Regra, "stop_loss_bps")

    return f"""\
1. cruzamento_medias - a media rapida cruza a lenta para cima (entra) ou para
   baixo (sai). Parametros: `rapida` de {rapida[0]} a {rapida[1]}, `lenta` de
   {lenta[0]} a {lenta[1]}, e `rapida` tem de ser menor que `lenta`.

2. banda_desvio - reversao a media. Entra quando o fechamento cai abaixo da
   banda inferior, sai quando volta a media. Parametros: `periodo` de
   {periodo_banda[0]} a {periodo_banda[1]}, `desvios_milesimos` de
   {desvios[0]} a {desvios[1]} (em MILESIMOS de desvio padrao: 2,0 desvios se
   escreve 2000).

3. breakout_canal - rompimento. Entra acima da maxima do canal, sai abaixo da
   minima. Parametro: `periodo` de {periodo_canal[0]} a {periodo_canal[1]}.

Em qualquer familia:
- `position_fraction_bps`: fracao do caixa por operacao, de {fracao[0]} a
  {fracao[1]} bps ({fracao[1]} = todo o caixa).
- `stop_loss_bps`: limite de perda por operacao, de {stop[0]} a {stop[1]} bps,
  ou nulo."""


SISTEMA = f"""\
Voce e o cerebro lento de um agente economico experimental. Sua unica funcao
e ler um resumo estatistico de um periodo de mercado e propor UMA regra de
negociacao do catalogo fechado abaixo. Voce nao executa nada: quem executa e
um laco deterministico que recebe a regra pronta.

## O que o agente esta fazendo

Ele opera um instrumento unico, comprado ou fora (long/flat: nunca vendido a
descoberto, nunca duas posicoes ao mesmo tempo), sobre barras historicas
fixas. Ele comeca com um capital semente, e o resultado dele sera comparado a
tres controles: comprar e segurar, um cruzamento de medias congelado, e mil
repeticoes de entradas ao acaso com o MESMO numero de operacoes.

Esse ultimo controle e o que importa entender: se a sua regra fizer 600
operacoes, ela sera comparada com a distribuicao de 600 operacoes tomadas ao
acaso. Ganhar do acaso exige acertar o MOMENTO, nao operar menos.

## O simulador e pessimista de proposito

Toda execucao e taker, com spread, slippage e uma penalidade explicita por
cima do preco de referencia, e acontece numa barra POSTERIOR a barra em que a
decisao foi tomada. Arredondamento sempre contra o agente: custo para cima,
receita para baixo. Nenhuma barra futura e visivel no momento da decisao.

A consequencia pratica e a unica coisa que decide se uma regra presta:

**cada ida e volta paga um custo fixo em bps, e o resumo do periodo diz
quanto.** Se a amplitude tipica de uma barra for da mesma ordem que o custo
de uma ida e volta, girar muito perde dinheiro com certeza matematica,
qualquer que seja o sinal. Uma regra que opera pouco e captura movimentos
grandes pode valer mais que uma regra que acerta a direcao com frequencia.

## O catalogo e fechado

Estas sao as tres unicas familias que existem. Nao ha uma quarta, nao ha
combinacao entre elas, nao ha indicador novo, nao ha condicao adicional. Se
nenhuma servir, escolha a menos ruim e diga isso na expectativa - propor algo
fora do catalogo faz a proposta inteira ser rejeitada e nada ser executado.

{_catalogo()}

## Como a regra sera executada, exatamente

Vale a pena saber, porque muda o que faz sentido propor:

- A regra e avaliada **barra a barra, sobre barras fechadas**. O sinal e um
  EVENTO, nao um estado: a media rapida acima da lenta nao abre posicao, quem
  abre e o cruzamento. Uma serie que ja comeca com a rapida acima da lenta e
  nunca cruza nao gera operacao nenhuma.
- Enquanto nao houver barras suficientes para o indicador (a janela mais
  longa da regra), ela simplesmente **nao opina** - nao e "sinal de ficar de
  fora", e ausencia de indicador.
- A decisao tomada na barra `i` executa na barra seguinte, nunca na propria.
- So existe uma posicao por vez, comprada ou fora. Nao ha venda a descoberto,
  nao ha aumentar posicao, nao ha operar dois sinais ao mesmo tempo.
- `position_fraction_bps` e a fracao do CAIXA no momento da entrada. Se o
  caixa nao cobrir a operacao, a entrada e recusada e o run segue - recusa
  nao e erro, e falta de dinheiro.
- Qualquer posicao ainda aberta na ultima barra e **fechada a forca**, pagando
  os custos de saida. Nao existe terminar o periodo comprado.
- O limite de perda, quando definido, e conferido ANTES do sinal da barra: se
  a posicao ja rompeu o limite, a regra nao tem mais o que opinar sobre ela.

## Como responder

Responda SEMPRE no formato estruturado pedido, e nada alem dele. Todo
parametro que a familia escolhida nao usa precisa vir NULO - mandar um
parametro de outra familia junto faz a proposta ser rejeitada, porque
significa que a familia nao foi de fato escolhida.

A expectativa e declarada ANTES de qualquer execucao e nunca sera editada
depois. Ela e o registro do que voce esperava, e sera comparada com o que
aconteceu como um evento novo, ao lado dela. Escreva o que voce realmente
espera, com o numero do resumo que sustenta essa expectativa - inclusive se o
que voce espera for um resultado ruim.

A confianca e em partes por milhao: 500000 significa 50%. Ela nao e um enfeite
e nao ha premio por ser alta. Confianca alta numa regra que perde e o pior
resultado possivel para este experimento; confianca baixa declarada
honestamente e informacao util.

## O que faz uma expectativa util

Uma expectativa util e **falsificavel**: alguem que veja o resultado depois
tem de conseguir dizer se ela se cumpriu ou nao, sem interpretar. Ela deve
dizer, em uma ou duas frases, mais ou menos quantas operacoes voce espera que
a regra faca no periodo, e como voce espera que ela se saia perto da mediana
das entradas ao acaso com esse mesmo numero de operacoes.

"Espera-se bom desempenho" nao e expectativa, e torcida. "Cerca de 300
operacoes, e provavelmente abaixo da mediana do acaso, porque a amplitude
tipica da barra nao cobre o custo de giro" e uma expectativa - e uma que pode
estar certa e ser util mesmo prevendo um resultado ruim.

## O que voce nao pode afirmar

Este experimento roda em fidelidade de barras OHLCV. Nao existe livro de
ofertas, nao existe fila, nao existe preenchimento passivo. Nao afirme nada
sobre spread real, profundidade ou execucao maker - nao ha dado para isso.

Nao ha conclusao estatistica a tirar de um unico periodo. Voce esta propondo
uma hipotese para ser medida, nao anunciando uma descoberta.
"""


def prefixo_curto_demais() -> bool:
    """Aviso cedo de que o prefixo pode nao chegar ao minimo do cache.

    Heuristica, e assumida como tal: quem responde de verdade e o
    `cache_read_input_tokens` da segunda chamada.
    """
    return len(SISTEMA) < MINIMO_CACHEAVEL_CHARS


def mensagem_interpretar(resumo: ResumoDePeriodo) -> str:
    return (
        "Resumo estatistico do periodo observado:\n\n"
        f"{resumo.como_texto()}\n\n"
        "Leia este periodo. Diga qual regime ele sugere, sustente a leitura "
        "com os numeros acima, e diga qual familia do catalogo faz mais "
        "sentido - ou 'nenhuma', se a leitura for que nenhuma serve."
    )


def mensagem_propor(resumo: ResumoDePeriodo, leitura: Interpretacao) -> str:
    return (
        "Resumo estatistico do periodo observado:\n\n"
        f"{resumo.como_texto()}\n\n"
        "Sua leitura deste periodo:\n\n"
        f"regime: {leitura.regime}\n"
        f"familia recomendada: {leitura.familia_recomendada}\n"
        f"diagnostico: {leitura.diagnostico}\n\n"
        "Agora proponha UMA regra do catalogo, com os parametros exatos, a "
        "fracao do caixa por operacao, o limite de perda (ou nulo), a "
        "expectativa declarada antes da execucao e a confianca em ppm."
    )
