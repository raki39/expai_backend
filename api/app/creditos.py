"""Créditos de teste: tentativa estatística é recurso escasso (R42, R43, §8.6.1).

> "Se o agente paga por computação mas testa hipóteses de graça, ele tem
> incentivo para gerar hipóteses em massa e ficar com as que passarem por
> acaso. Isso destrói o orçamento de falsas descobertas de toda a
> especialidade, e o dano recai sobre agentes que não cometeram o abuso.
>
> Portanto: tentativa estatística é recurso escasso, consome créditos e sai do
> orçamento do agente." — §8.6.1

## Os pesos são do documento, e o banco os impõe

| Tipo de teste | Créditos | Justificativa de §8.6.1 |
|---|---|---|
| in-sample de hipótese pré-registrada | **1** | consumo base |
| reteste da mesma hipótese com parâmetro alterado | **3** | "varredura de parâmetro é a principal fonte de sobreajuste" |
| out-of-sample | **5** | "consome dados reservados, que são finitos e não renováveis" |
| entrada em quarentena | **10** | "ocupa capacidade de observação em tempo real" |

Não são configuráveis. O gatilho `credito_usa_o_peso_do_documento` recusa
cobrar 1 por um out-of-sample — que consumiria o dado mais escasso do sistema
ao preço do teste mais barato.

## Isto NÃO é o ledger

A regra 7 fixa **dois** livros, real em BRL e simulado em USD. Crédito não é
nenhum dos dois: §8.6.1 diz que os pesos "são pesos administrativos iniciais,
**não custos econômicos demonstrados**", e que na Fase 0 "servem apenas para
criar escassez". Um terceiro livro tornaria "somar o ledger" uma operação sem
significado.

O que se herda do ledger é a **disciplina**: apenas por acréscimo, saldo
derivado de view, correção só por registro novo.

## E não determina o FDR

§8.6.1 é explícita: são "dois mecanismos distintos, que não devem ser
confundidos (...) cobrar por hipótese cria incentivo econômico correto, mas
**não determina matematicamente o FDR**. Os dois precisam existir em paralelo;
nenhum substitui o outro."

Por isso `app/estatistica` não importa este módulo e não menciona crédito. Há
teste varrendo os dois lados.

## Reteste é reconhecido pelo conteúdo, não pela memória de ninguém

A diferença entre 1 e 3 créditos é a diferença entre "hipótese nova" e "a mesma
hipótese com outro parâmetro". `hypothesis.content_hash` já existe desde o
incremento 8 exatamente para isso: duas gravações da mesma afirmação têm o
mesmo hash, e um parâmetro trocado muda o hash.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)

BRACOS = ("agente", "b4", "a1a", "a1b")

#: De qual BRACO uma hipotese e, a partir da `agente_origem` dela.
#:
#: As chaves sao as constantes `AGENTE_ORIGEM*` de `hipotese.registro`,
#: escritas aqui como literais para nao importar `app/hipotese` neste modulo -
#: e ha teste comparando as duas listas, porque duas copias de um mapa fechado
#: divergem.
#:
#: Os controles tem braco proprio porque §14.4 os manda injetar "pelo mesmo
#: caminho das reais", e o mesmo caminho COBRA. Cobra-los do agente drenaria o
#: orcamento do que a fase mede - o defeito que o incremento 12 encontrou -, e
#: isenta-los exigiria um ramo no validador que reconhece controle, o que
#: quebraria "mesmo caminho" justamente onde ele e a garantia.
ORIGEM_PARA_BRACO: dict[str, str] = {
    "transacao@0b": "agente",
    "b4@0b": "b4",
    "a1a@0b": "a1a",
    "a1b@0b": "a1b",
}


def braco_da_hipotese(conn: sqlite3.Connection, hypothesis_id: int) -> str:
    """O braco que paga por testar esta hipotese. **Lido do banco.**

    Existe porque `promocao._avaliar` cobrava `braco="agente"` FIXO, para
    qualquer hipotese. Enquanto houve um braco so, o defeito era invisivel;
    com B4, testar uma hipotese do controle drenaria o orcamento do agente -
    e §14.3 exige "mesmo orcamento de creditos de teste" nos dois, o que so
    significa algo se os dois forem contados separado.

    Derivado da linha da hipotese, e nunca parametro do chamador: um `braco`
    passado de fora poderia cobrar do bolso errado, que e a mesma porta lateral
    que o incremento 10 fechou na maquina de estados ao exigir que a transicao
    parta do estado LIDO DO BANCO, e nao do que o chamador declara.
    """
    linha = conn.execute(
        "SELECT agente_origem FROM hypothesis WHERE id = ?", (hypothesis_id,)
    ).fetchone()
    if linha is None:
        raise ValueError(f"hipotese {hypothesis_id} nao existe")
    origem = linha["agente_origem"]
    braco = ORIGEM_PARA_BRACO.get(origem)
    if braco is None:
        # Nao cai no braco do agente por padrao. Uma origem nova sem braco
        # atribuido cobraria de alguem, e cobrar do bolso errado em silencio e
        # pior que recusar - o orcamento e o denominador da comparacao da fase.
        raise ValueError(
            f"origem {origem!r} nao tem braco de creditos atribuido; a"
            " comparacao de §14.3 e por credito gasto POR BRACO, e um"
            " lancamento no braco errado a corrompe"
        )
    return braco

# Tabela da §8.6.1. Os mesmos números vivem num CASE do gatilho
# `credito_usa_o_peso_do_documento`: duas cópias, e é deliberado - se
# divergirem, a inserção é recusada e o defeito aparece na primeira cobrança,
# em vez de virar preço errado gravado em silêncio.
PESOS: dict[str, int] = {
    "in_sample": 1,
    "reteste_parametro": 3,
    "out_of_sample": 5,
    "quarentena": 10,
}


class SemCredito(Exception):
    """O orçamento do braço acabou. §8.6.1 quer que acabar signifique parar."""


class OrcamentoAusente(Exception):
    """Testar sem orçamento seria testar de graça."""


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Saldo:
    braco: str
    config_version_id: int
    orcamento: int
    consumido: int
    restante: int

    def como_dict(self) -> dict:
        return {
            "braco": self.braco,
            "config_version_id": self.config_version_id,
            "orcamento": self.orcamento,
            "consumido": self.consumido,
            "restante": self.restante,
        }


def conceder(
    conn: sqlite3.Connection,
    *,
    braco: str,
    config_version_id: int,
    creditos: int,
) -> None:
    """Fixa o orçamento do braço nesta config. Idempotente.

    D30: 60 por braço, **idênticos** para o agente e para o B4 — §14.3 exige
    "mesmo orçamento de créditos de teste", e é o que torna a comparação "por
    crédito gasto" possível. Um braço com orçamento maior mediria orçamento,
    não qualidade de hipótese.
    """
    if braco not in BRACOS:
        raise ValueError(f"braço desconhecido: {braco!r}")
    if conn.execute(
        "SELECT 1 FROM test_credit_budget WHERE braco = ?"
        " AND config_version_id = ?",
        (braco, config_version_id),
    ).fetchone():
        return
    conn.execute(
        "INSERT INTO test_credit_budget (braco, config_version_id, creditos,"
        " created_at) VALUES (?,?,?,?)",
        (braco, config_version_id, creditos, _agora()),
    )
    log.info(
        "creditos.concedidos",
        extra={
            "braco": braco,
            "config_version_id": config_version_id,
            "creditos": creditos,
        },
    )


def tipo_do_teste(
    conn: sqlite3.Connection, hypothesis_id: int, *, etapa: str
) -> str:
    """1 crédito ou 3? Decidido pelo CONTEÚDO da hipótese.

    §8.6.1 cobra o triplo pelo "reteste da mesma hipótese com parâmetro
    alterado". Reconhecer isso exige comparar o que a hipótese diz, e é o que
    `content_hash` faz desde o incremento 8.

    Uma hipótese cujo hash já apareceu antes é reteste — mesmo que quem a
    propôs não soubesse disso, o que é justamente o caso que a cobrança
    precisa pegar.
    """
    if etapa != "in_sample":
        return etapa
    linha = conn.execute(
        "SELECT content_hash FROM hypothesis WHERE id = ?", (hypothesis_id,)
    ).fetchone()
    if linha is None:
        raise ValueError(f"hipótese {hypothesis_id} não existe")
    anteriores = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM hypothesis"
            " WHERE content_hash = ? AND id < ?",
            (linha["content_hash"], hypothesis_id),
        ).fetchone()["n"]
    )
    return "reteste_parametro" if anteriores else "in_sample"


def cobrar(
    conn: sqlite3.Connection,
    *,
    braco: str,
    config_version_id: int,
    hypothesis_id: int,
    tipo: str,
    cpu_micros: int,
    barras_reservadas: int,
    familia_max: int,
) -> int:
    """Cobra o teste. Devolve os créditos debitados.

    Levanta `SemCredito` quando o orçamento não cobre — e a recusa vem do
    **banco**, não de uma conferência aqui. Se a checagem morasse no Python,
    um defeito neste arquivo mascararia a ausência da regra no schema, que é
    o mesmo desenho das partidas dobradas do incremento 2.

    Os quatro números de calibração de §8.6.1 (R43) são gravados junto, e
    **medidos**, não estimados depois: estimar no fim exigiria supor quantos
    testes de cada tipo houve e quanto cada um custou, que é exatamente o que
    a calibração existe para descobrir.
    """
    if tipo not in PESOS:
        raise ValueError(f"tipo de teste desconhecido: {tipo!r}")
    creditos = PESOS[tipo]

    # Terceiro número de R43: quanto deste teste pesa no orçamento estatístico
    # da especialidade. Uma tentativa numa família de 48 consome 1/48 da
    # multiplicidade, e é isso que encarece o limiar de BY para todos.
    impacto_bps = 10_000 // max(1, familia_max)

    try:
        conn.execute(
            "INSERT INTO test_credit_entry (braco, config_version_id,"
            " hypothesis_id, tipo, creditos, occurred_at, impacto_fdr_bps,"
            " cpu_micros, barras_reservadas) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                braco,
                config_version_id,
                hypothesis_id,
                tipo,
                creditos,
                _agora(),
                impacto_bps,
                cpu_micros,
                barras_reservadas,
            ),
        )
    except sqlite3.IntegrityError as erro:
        texto = str(erro)
        if "orcamento" in texto and "nao ha" in texto:
            raise OrcamentoAusente(texto) from erro
        if "insuficientes" in texto:
            atual = saldo(conn, braco=braco, config_version_id=config_version_id)
            raise SemCredito(
                f"orçamento esgotado: o braço '{braco}' precisa de"
                f" {creditos} créditos para um teste '{tipo}' e tem"
                f" {atual.restante if atual else 0}. §8.6.1 existe para que"
                " orçamento esgotado signifique parar de testar"
            ) from erro
        raise

    log.info(
        "creditos.cobrados",
        extra={
            "braco": braco,
            "hypothesis_id": hypothesis_id,
            "tipo": tipo,
            "creditos": creditos,
            "cpu_micros": cpu_micros,
        },
    )
    return creditos


def saldo(
    conn: sqlite3.Connection, *, braco: str, config_version_id: int
) -> Saldo | None:
    linha = conn.execute(
        "SELECT braco, config_version_id, orcamento, consumido, restante"
        "  FROM test_credit_balance WHERE braco = ? AND config_version_id = ?",
        (braco, config_version_id),
    ).fetchone()
    if linha is None:
        return None
    return Saldo(
        braco=linha["braco"],
        config_version_id=int(linha["config_version_id"]),
        orcamento=int(linha["orcamento"]),
        consumido=int(linha["consumido"]),
        restante=int(linha["restante"]),
    )


def calibracao(conn: sqlite3.Connection) -> dict:
    """Os quatro números de §8.6.1, agregados por tipo de teste.

    > "O que deve ser medido durante a fase, para calibrá-los depois: consumo
    > de créditos por agente e por tipo de teste; impacto de cada teste no
    > orçamento estatístico da especialidade; custo computacional real de cada
    > tipo de teste; custo de oportunidade do dado reservado consumido."

    Isto **não recalibra nada**. §8.6.1 diz que "só com esses dados é possível
    saber qual precificação realmente reduz teste oportunista" — e mudar os
    pesos 1/3/5/10 durante a fase seria mudar o preço depois de ver o consumo.
    O que a 0B faz é medir.
    """
    return {
        "por_tipo": [
            {
                "tipo": l["tipo"],
                "testes": int(l["testes"]),
                "creditos": int(l["creditos"]),
                "cpu_micros_total": int(l["cpu"]),
                "cpu_micros_medio": int(l["cpu"]) // max(1, int(l["testes"])),
                "barras_reservadas": int(l["barras"]),
                "impacto_fdr_bps": int(l["impacto"]),
            }
            for l in conn.execute(
                "SELECT tipo, COUNT(*) AS testes, SUM(creditos) AS creditos,"
                " SUM(cpu_micros) AS cpu, SUM(barras_reservadas) AS barras,"
                " SUM(impacto_fdr_bps) AS impacto"
                " FROM test_credit_entry GROUP BY tipo ORDER BY tipo"
            )
        ],
        "por_braco": [
            s.como_dict()
            for s in (
                saldo(
                    conn,
                    braco=l["braco"],
                    config_version_id=int(l["config_version_id"]),
                )
                for l in conn.execute(
                    "SELECT braco, config_version_id FROM test_credit_budget"
                    " ORDER BY braco, config_version_id"
                )
            )
            if s is not None
        ],
        "pesos_do_documento": dict(PESOS),
        "nota": (
            "os pesos 1/3/5/10 são de §8.6.1 e não são recalibrados durante a"
            " fase: mudar preço depois de ver o consumo é escolher a régua"
            " depois do resultado"
        ),
    }


def testes_da_hipotese(conn: sqlite3.Connection, hypothesis_id: int) -> list[dict]:
    """Quando esta hipotese foi testada, e a que custo. **O registro do fato.**

    Existe porque um parecer `inconclusiva` nao move a hipotese - §14.4: nem
    promove nem descarta - e portanto nao deixa transicao. O estado dela fica
    em `hipotese_registrada`, que e exatamente o mesmo estado de uma hipotese
    **nunca testada**.

    Em producao isso apareceu como a hipotese 1: avaliada no in-sample,
    veredito `inconclusiva`, credito cobrado - e nenhuma rota conseguia
    distingui-la de uma que ninguem tinha olhado. §8.6 e explicito: "Toda
    hipotese testada e registrada, inclusive as que falharam", e a R51 existe
    para separar **rejeitado** de **inconclusivo**. Sem esta consulta, o
    terceiro caso - "nunca testado" - se confundia com o segundo.

    O lancamento de credito ja era o registro; o que faltava era conseguir
    le-lo por hipotese.
    """
    return [
        dict(l)
        for l in conn.execute(
            "SELECT tipo, creditos, occurred_at, braco, config_version_id,"
            "       cpu_micros, barras_reservadas"
            "  FROM test_credit_entry WHERE hypothesis_id = ?"
            " ORDER BY id",
            (hypothesis_id,),
        )
    ]
