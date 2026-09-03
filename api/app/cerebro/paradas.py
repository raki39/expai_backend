"""Por que o cerebro parou. **Uma lista fechada, e uma fonte para ela.**

O grafo sempre soube parar; ele nao sabia dizer por que. `_parar` mandava o
motivo para `log.info` e para o corpo do POST, e ali ele morria: o GET nao o
tinha, o export nao o tinha, o painel nao o tinha. Diagnosticar um run exigia
abrir o log da plataforma - que e o mesmo que dizer que nao da para
diagnosticar.

## Por que a categoria e separada do motivo

O motivo e texto para uma pessoa ler. A **categoria** e o que o programa
decide em cima, e sao decisoes diferentes:

- `teto_atingido` e a unica em que as maos rapidas seguem executando a regra
  padrao. Isso e a secao 3.6, regra 2, ao pe da letra: "Ao atingir o teto, ele
  continua operando com as maos rapidas, mas para de raciocinar ate o proximo
  ciclo."
- **Todas as outras sao falha tecnica, e nelas nada executa** (D35). A secao
  3.6.2 fala do TETO: ali o agente decidiu nao gastar. Numa falha de provedor
  ele nao conseguiu decidir - e atribuir a ele o resultado de uma regra que
  ninguem escolheu foi o que fez o run 27 parecer um agente entre o p50 e o
  p95 quando o que rodou era o B3.

Tratar as duas igual foi o defeito. A lista aqui e a mesma da migracao 13, e
ha teste comparando as duas - porque duas listas fechadas iguais em arquivos
diferentes e a forma classica de uma delas parar de descrever.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# A lista fechada. Espelhada no gatilho `categoria_de_parada_fechada`.
# ---------------------------------------------------------------------------

#: O agente gastou o que podia gastar. Nao e falha (secao 3.6, regra 2).
TETO_ATINGIDO = "teto_atingido"

#: O provedor recusou a NOSSA requisicao (400/422). O defeito esta no que
#: enviamos - schema de saida malformado, parametro que o modelo nao aceita.
PEDIDO_RECUSADO = "pedido_recusado"

#: A RESPOSTA do modelo nao bate com o contrato. O defeito esta no que voltou.
ERRO_SCHEMA = "erro_schema"

#: O limite de saida acabou antes de sair texto. O pensamento conta nele.
MAX_TOKENS = "max_tokens"

#: A chamada falhou - transitoria com as tentativas esgotadas, ou permanente.
ERRO_PROVEDOR = "erro_provedor"

#: Sem credencial ou sem adaptador para o provedor resolvido pela config.
PROVEDOR_INDISPONIVEL = "provedor_indisponivel"

#: O tier pedido nao existe na configuracao vigente.
TIER_NAO_CONFIGURADO = "tier_nao_configurado"

#: Excecao do proprio no, fora de qualquer chamada de modelo.
ERRO_INTERNO = "erro_interno"

CATEGORIAS: frozenset[str] = frozenset(
    {
        TETO_ATINGIDO,
        PEDIDO_RECUSADO,
        ERRO_SCHEMA,
        MAX_TOKENS,
        ERRO_PROVEDOR,
        PROVEDOR_INDISPONIVEL,
        TIER_NAO_CONFIGURADO,
        ERRO_INTERNO,
    }
)

#: A unica categoria em que as maos rapidas seguem (secao 3.6, regra 2).
#:
#: Escrito como CONJUNTO, e nao como `== TETO_ATINGIDO` espalhado pelo codigo,
#: porque a pergunta "isto autoriza executar sem decisao do cerebro?" tem de
#: ter um lugar so. Se algum dia outra categoria entrar aqui, entra com ADR.
EXECUTA_MESMO_ASSIM: frozenset[str] = frozenset({TETO_ATINGIDO})


def executa_regra_padrao(categoria: str | None) -> bool:
    """As maos rapidas rodam a regra padrao apos esta parada? (D35)

    `None` significa que nao houve parada - o cerebro falou, e quem decide o
    que executa e a proposta dele.
    """
    return categoria is None or categoria in EXECUTA_MESMO_ASSIM


# ---------------------------------------------------------------------------
# Classificacao da excecao crua do SDK
# ---------------------------------------------------------------------------

#: Codigos em que tentar de novo faz sentido. 408 e timeout do lado deles,
#: 409 e conflito de concorrencia, 429 e limite de taxa, 5xx e falha deles.
#: 529 e o "overloaded" da Anthropic, que cai no >= 500.
_TRANSITORIOS = frozenset({408, 409, 429})

#: Recusa da requisicao: tentar de novo com o mesmo corpo da o mesmo 400.
_RECUSA_DO_PEDIDO = frozenset({400, 422})

#: Credencial ou permissao. Nao e transitorio e nao e schema.
_CREDENCIAL = frozenset({401, 403})


def classificar(erro: BaseException) -> tuple[str, bool]:
    """Devolve `(categoria, transitorio)` para uma excecao crua de SDK.

    Le `status_code` por `getattr`, e nao por `isinstance`, de proposito: os
    dois SDKs expoem o campo em `APIStatusError`, e importar o tipo de um deles
    aqui poria nome de provedor no codigo comum - o que a regra 4 proibe e o
    que faria este modulo deixar de servir ao segundo adaptador, que a secao
    3.9 exige viavel.
    """
    status = getattr(erro, "status_code", None)
    nome = type(erro).__name__

    if isinstance(status, int):
        if status in _RECUSA_DO_PEDIDO:
            return PEDIDO_RECUSADO, False
        if status in _CREDENCIAL:
            return PROVEDOR_INDISPONIVEL, False
        if status in _TRANSITORIOS or status >= 500:
            return ERRO_PROVEDOR, True
        return ERRO_PROVEDOR, False

    # Sem status: nao chegou a haver resposta HTTP. Timeout e falha de conexao
    # sao o caso classico de tentar de novo; o resto nao se sabe, e "nao se
    # sabe" nao autoriza gastar dinheiro de novo.
    if "Timeout" in nome or "Connection" in nome:
        return ERRO_PROVEDOR, True
    return ERRO_PROVEDOR, False


# ---------------------------------------------------------------------------
# Atribuicao
# ---------------------------------------------------------------------------

def atribuicao(
    *, veio_do_cerebro: bool, categoria: str | None, executou: bool
) -> dict:
    """Este resultado pode ser chamado de "resultado do agente"?

    Existe como funcao, e num modulo que nem a api nem o painel podem
    contornar, porque a alternativa ja falhou: `regra_veio_do_cerebro` era
    calculado no ciclo, ia no corpo do POST e **sumia no GET**. O painel
    mostrava patrimonio, faixa contra o acaso e excesso sobre o B1 sem nada
    dizendo que nenhuma regra tinha vindo do cerebro.

    Nao e detalhe de apresentacao. `faixa: "entre_p50_e_p95"` e uma afirmacao
    sobre a competencia do agente; sobre um run em que ele nao decidiu nada,
    e uma afirmacao falsa.
    """
    if veio_do_cerebro:
        return {
            "atribuivel_ao_agente": True,
            "o_que_executou": "a regra proposta pelo cerebro",
            "por_que": "houve decisao cognitiva registrada, com regra e hash",
        }
    if categoria == TETO_ATINGIDO and executou:
        return {
            "atribuivel_ao_agente": False,
            "o_que_executou": (
                "a regra padrao - o mesmo cruzamento do B3, derivado da config"
            ),
            "por_que": (
                "o teto do run foi atingido e o cerebro parou de raciocinar."
                " As maos rapidas seguem por exigencia da secao 3.6, regra 2,"
                " mas o resultado e do B3 e nao do agente (D23)"
            ),
        }
    if executou:
        # **Eu escrevi aqui que nao havia caminho que chegasse.** Havia: um run
        # que executou, sem parada e sem regra do cerebro - que e exatamente o
        # que um run de B4 e. Ele apareceu em producao com o texto
        # "parada em None, com execucao autorizada", que nao explica nada a
        # quem le.
        #
        # A causa raiz era outra (a rota do agente mostrava run de B4), mas o
        # texto tem de servir de todo jeito: um ramo alcancavel com mensagem
        # de ramo impossivel e pior que o ramo nao existir.
        if categoria is None:
            return {
                "atribuivel_ao_agente": False,
                "o_que_executou": "a regra padrao",
                "por_que": (
                    "houve execucao e nenhuma regra veio do cerebro, e tambem"
                    " nao houve parada. E o formato de um run que nao e do"
                    " agente - de B4, por exemplo. Se isto aparecer na tela do"
                    " agente, quem esta errado e a consulta que escolheu o run"
                ),
            }
        return {
            "atribuivel_ao_agente": False,
            "o_que_executou": "a regra padrao",
            "por_que": (
                f"parada em {categoria!r}, que autoriza executar sem decisao"
                " do cerebro. Ver `EXECUTA_MESMO_ASSIM`"
            ),
        }
    return {
        "atribuivel_ao_agente": False,
        "o_que_executou": "nada",
        "por_que": (
            f"o cerebro parou por {categoria!r}, que e falha tecnica e nao"
            " decisao. Nada foi executado, porque atribuir ao agente uma"
            " regra que ele nao escolheu foi o defeito que a D35 corrigiu"
        ),
    }
