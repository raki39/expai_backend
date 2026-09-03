"""Lancamentos, carteira derivada e as conferencias do livro.

Convencao de sinal, a mesma da migracao 3:

    valor_minor > 0   a conta RECEBE
    valor_minor < 0   a conta ENTREGA

"Soma de debitos igual a soma de creditos" vira, nesta forma, "a soma dos
lancamentos de um livro e zero" - que e o que o banco consegue conferir
sozinho, no fechamento da transacao.

**Quem impede o desequilibrio e o trigger, nao este modulo.** Aqui nao ha
validacao de partidas dobradas de proposito: se houvesse, um defeito neste
codigo poderia mascarar a ausencia da regra no banco, e a garantia passaria a
depender de o caminho certo ter sido usado. O teste correspondente insere
desequilibrado DIRETO no SQL e espera a recusa.

Nao existe coluna de saldo. O saldo e sempre derivado dos lancamentos (regra
16: nunca duas fontes de verdade sobre dinheiro).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, NamedTuple, Sequence

from ..store import bloco_atomico
from . import contas

log = logging.getLogger(__name__)

MICRO = 1_000_000


class Lancamento(NamedTuple):
    """Uma linha da transacao. `valor_minor` e assinado."""

    conta: str
    valor_minor: int
    memo: str = ""


class ContaDesconhecida(Exception):
    pass


class TransacaoInvalida(Exception):
    pass


def agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fx_micro(taxa: Decimal) -> int:
    """5,40 BRL/USD -> 5400000. Sem passar por float.

    A taxa vira dinheiro assim que multiplica um valor; guarda-la em ponto
    flutuante contaminaria o valor convertido (regra 5).
    """
    escalado = Decimal(taxa) * MICRO
    if escalado != escalado.to_integral_value():
        raise ValueError(
            f"taxa {taxa} tem mais de 6 casas decimais; arredondar aqui seria "
            "decidir em silencio o valor de um lancamento"
        )
    return int(escalado)


def usd_para_brl(usd_minor: int, fx_rate_micro: int) -> int:
    """Converte centavos de USD em centavos de BRL.

    Arredonda **para cima** quando o valor representa custo. E a escolha
    pessimista, coerente com a secao 8.4.1: na duvida, o experimento paga
    mais, nunca menos. Arredondar para baixo criaria um centavo de vantagem
    sistematica que nao existe no mundo.
    """
    if usd_minor < 0:
        raise ValueError("usar valor absoluto; o sinal e do lancamento")
    produto = usd_minor * fx_rate_micro
    return -(-produto // MICRO)  # divisao com teto, sem float


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------


def registrar(
    conn: sqlite3.Connection,
    *,
    kind: str,
    lancamentos: Sequence[Lancamento],
    occurred_at: str | None = None,
    run_id: int | None = None,
    fx_rate_micro: int | None = None,
    fx_rate_date: str | None = None,
    reverses: int | None = None,
    agent_event_id: int | None = None,
    memo: str = "",
) -> int:
    """Abre uma transacao, lanca e fecha. Devolve o id.

    O fechamento e o momento em que o banco confere as partidas dobradas. Se
    nao fecharem, o `UPDATE` falha e o SAVEPOINT desfaz tudo - nunca fica
    meia transacao gravada.
    """
    if not lancamentos:
        raise TransacaoInvalida("transacao sem lancamento")

    mapa = contas.id_por_codigo(conn)
    desconhecidas = {l.conta for l in lancamentos} - set(mapa)
    if desconhecidas:
        raise ContaDesconhecida(f"conta(s) inexistente(s): {sorted(desconhecidas)}")

    quando = occurred_at or agora()

    with bloco_atomico(conn, "registrar"):
        cur = conn.execute(
            "INSERT INTO ledger_transaction (kind, occurred_at, run_id,"
            " fx_rate_micro, fx_rate_date, agent_event_id,"
            " reverses_transaction_id, memo)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (kind, quando, run_id, fx_rate_micro, fx_rate_date,
             agent_event_id, reverses, memo),
        )
        tx_id = int(cur.lastrowid)

        conn.executemany(
            "INSERT INTO ledger_entry (transaction_id, account_id, amount_minor,"
            " memo) VALUES (?,?,?,?)",
            [(tx_id, mapa[l.conta], l.valor_minor, l.memo) for l in lancamentos],
        )

        # Fechar dispara a conferencia. Nao ha como fechar desequilibrada.
        conn.execute(
            "UPDATE ledger_transaction SET posted_at = ? WHERE id = ?",
            (agora(), tx_id),
        )

    log.info(
        "ledger.transacao",
        extra={"transaction_id": tx_id, "kind": kind, "lancamentos": len(lancamentos)},
    )
    return tx_id


def estornar(conn: sqlite3.Connection, transaction_id: int, *, memo: str = "") -> int:
    """Cria o estorno: mesmos lancamentos com sinal trocado.

    Correcao e SEMPRE estorno (regra 6). Os dois permanecem visiveis no
    historico - o erro nao desaparece, ele fica registrado ao lado da
    correcao. Historia que pode ser apagada nao e historia.
    """
    original = conn.execute(
        "SELECT id, kind, run_id, fx_rate_micro, fx_rate_date, posted_at"
        " FROM ledger_transaction WHERE id = ?",
        (transaction_id,),
    ).fetchone()
    if original is None:
        raise TransacaoInvalida(f"transacao {transaction_id} nao existe")
    if original["posted_at"] is None:
        raise TransacaoInvalida(
            f"transacao {transaction_id} esta aberta; nao ha o que estornar"
        )

    linhas = conn.execute(
        "SELECT a.code AS code, e.amount_minor AS valor"
        " FROM ledger_entry e JOIN account a ON a.id = e.account_id"
        " WHERE e.transaction_id = ?",
        (transaction_id,),
    ).fetchall()

    return registrar(
        conn,
        kind="estorno",
        lancamentos=[
            Lancamento(l["code"], -int(l["valor"]), f"estorno de {transaction_id}")
            for l in linhas
        ],
        run_id=original["run_id"],
        fx_rate_micro=original["fx_rate_micro"],
        fx_rate_date=original["fx_rate_date"],
        reverses=transaction_id,
        memo=memo or f"estorno da transacao {transaction_id}",
    )


# ---------------------------------------------------------------------------
# Abertura do run e capital semente
# ---------------------------------------------------------------------------


#: O `agent_id` do run DO AGENTE. Nomeado porque agora ha mais de um dono de
#: run que emite `agent_event`: o braco B4 tambem emite (evento nao cognitivo,
#: provedor nulo), e sem um identificador para filtrar, "o ultimo run com
#: evento" passou a incluir os runs do controle.
AGENT_ID_PADRAO = "agent-0001"


def abrir_run(
    conn: sqlite3.Connection,
    *,
    config_version_id: int,
    seed_capital_usd_cents: int,
    agent_id: str = AGENT_ID_PADRAO,
    casa_run_id: int | None = None,
) -> tuple[int, int]:
    """Cria o run e credita o capital semente COMO LANCAMENTO.

    Criterio 7: o capital nao e valor inicial magico numa coluna. Ele entra
    pela mesma porta que todo o resto do dinheiro, com contrapartida em
    patrimonio - e por isso aparece no historico e fecha as partidas dobradas
    como qualquer outro evento.

    `casa_run_id` marca este run como o CONTROLE casado de outro (migracao
    14). Ele nasce com o run e nao pode mudar depois - um controle que
    trocasse de alvo depois de medido seria a regua trocada depois do
    resultado. O default `None` e o caso normal: quase todo run nao controla
    ninguem.
    """
    if seed_capital_usd_cents <= 0:
        raise TransacaoInvalida("capital semente precisa ser positivo")

    with bloco_atomico(conn, "abrir_run"):
        cur = conn.execute(
            "INSERT INTO run (agent_id, state, config_version_id, created_at,"
            " updated_at, casa_run_id) VALUES (?, 'executando', ?, ?, ?, ?)",
            (agent_id, config_version_id, agora(), agora(), casa_run_id),
        )
        run_id = int(cur.lastrowid)
        tx_id = registrar(
            conn,
            kind="abertura",
            run_id=run_id,
            lancamentos=[
                Lancamento(contas.CAIXA_SIM, seed_capital_usd_cents, "capital semente"),
                Lancamento(contas.SEMENTE, -seed_capital_usd_cents, "capital semente"),
            ],
            memo="abertura do run",
        )

    log.info(
        "run.aberto",
        extra={"run_id": run_id, "seed_usd_cents": seed_capital_usd_cents},
    )
    return run_id, tx_id


def encerrar_run(conn: sqlite3.Connection, run_id: int, estado: str) -> None:
    if estado not in ("concluido", "interrompido", "abortado"):
        raise TransacaoInvalida(f"estado de encerramento invalido: {estado}")
    conn.execute(
        "UPDATE run SET state = ?, updated_at = ? WHERE id = ?",
        (estado, agora(), run_id),
    )
    log.info("run.encerrado", extra={"run_id": run_id, "state": estado})


# ---------------------------------------------------------------------------
# Custo de reflexao: o evento cognitivo e o dinheiro, amarrados
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Uso:
    """O que o provedor informou. `None` significa NAO INFORMADO.

    Nunca zero para campo ausente: "nao sei" e "foi zero" sao afirmacoes
    diferentes, e trata-las como iguais corrompe o custo por decisao
    (secao 5.2). Os dois provedores reportam cache de formas distintas, e e
    justamente ai que a confusao aconteceria.

    `tokens_in` e SEMPRE "entrada cobrada ao preco cheio", ja descontado o
    que veio do cache. Os provedores discordam nisto - a Anthropic entrega
    `input_tokens` ja sem os lidos do cache, a OpenAI entrega `prompt_tokens`
    COM eles dentro - e normalizar no adaptador e o unico lugar onde a
    diferenca pode ser tratada com conhecimento de causa. O payload cru fica
    em `bruto`, entao nada do que o provedor disse se perde.

    Os dois numeros de cache sao separados porque tem precos diferentes:
    escrever custa 1,25x a entrada, ler custa 0,1x. Um campo so para os dois
    obrigaria a estimar.
    """

    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_cache_read: int | None = None
    tokens_cache_write: int | None = None
    bruto: dict[str, Any] | None = None


def registrar_custo_reflexao(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    node: str,
    kind: str,
    custo_usd_minor: int,
    fx_rate_micro: int,
    fx_rate_date: str,
    uso: Uso | None = None,
    tier: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    expectation: str | None = None,
    confidence_ppm: int | None = None,
    parent_event_id: int | None = None,
    inputs_digest: str | None = None,
    outputs_digest: str | None = None,
    price_table_version: str | None = None,
    custo_usd_micro: int | None = None,
    dinheiro_real: bool = True,
    evaluation_json: str | None = None,
    stop_category: str | None = None,
    stop_reason: str | None = None,
) -> tuple[int, int | None]:
    """Grava o evento cognitivo e o dinheiro que ele custou, amarrados.

    Nos DOIS livros, na mesma transacao (criterio 6):

        simulado (USD)   carteira  -custo      tesouraria  +custo
        real     (BRL)   caixa     -custo_brl  despesa     +custo_brl

    Cada livro fecha em zero por si. A ponte entre eles e `fx_rate_micro` +
    `fx_rate_date`, gravados na propria transacao - nunca uma conversao feita
    na hora de ler, que faria variacao cambial virar desempenho (secao 4.2).

    **`dinheiro_real=False`** grava so o par do livro simulado. E o caso da
    resposta servida do cache local: o agente pagou pelo proprio pensamento -
    isso e fato do experimento e nao pode depender de o nosso cache estar
    quente, senao reexecutar um run melhoraria o resultado dele - mas nenhum
    real saiu da conta, e afirmar que saiu seria inventar despesa. Cada livro
    fecha em zero por si, entao omitir o par do livro real nao desequilibra
    coisa nenhuma.

    Ordem da gravacao, que existe por causa da referencia mutua (criterio 9):
    a transacao nasce aberta, o evento e criado apontando para ela, a
    transacao recebe o id do evento enquanto ainda esta aberta, e so entao
    fecha. Depois de fechada nao ha `UPDATE` possivel - nem para isso.
    """
    if custo_usd_minor < 0:
        raise TransacaoInvalida("custo nao pode ser negativo")
    if custo_usd_micro is not None and -(-custo_usd_micro // 10_000) != custo_usd_minor:
        # O lancamento tem de ser o TETO em centavos do custo exato. Se os
        # dois puderem divergir, o campo exato deixa de descrever o
        # lancamento e vira a quinta ocorrencia do padrao de sempre.
        raise TransacaoInvalida(
            f"custo em centavos ({custo_usd_minor}) nao e o teto do custo"
            f" exato ({custo_usd_micro} micros)"
        )

    uso = uso or Uso()
    custo_brl_minor = usd_para_brl(custo_usd_minor, fx_rate_micro)
    mapa = contas.id_por_codigo(conn)
    quando = agora()

    with bloco_atomico(conn, "reflexao"):
        # Custo zero nao cria transacao nenhuma. Houve decisao, nao houve
        # dinheiro - e uma transacao contabil vazia seria um evento economico
        # que nao existiu, pendurada para sempre em aberto porque o banco se
        # recusa (com razao) a fechar transacao sem lancamento.
        tx_id: int | None = None
        if custo_usd_minor > 0:
            cur = conn.execute(
                "INSERT INTO ledger_transaction (kind, occurred_at, run_id,"
                " fx_rate_micro, fx_rate_date, memo)"
                " VALUES ('reflexao',?,?,?,?,?)",
                (quando, run_id, fx_rate_micro, fx_rate_date,
                 f"custo de reflexao: {node}"),
            )
            tx_id = int(cur.lastrowid)

        cur = conn.execute(
            "INSERT INTO agent_event (run_id, parent_event_id, occurred_at, node,"
            " kind, tier, provider, model, tokens_in, tokens_out,"
            " tokens_cache_read, tokens_cache_write, price_table_version,"
            " usage_bruto_json, cost_usd_minor, cost_usd_micro, expectation,"
            " confidence_ppm, inputs_digest, outputs_digest,"
            " ledger_transaction_id, evaluation_json,"
            " stop_category, stop_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, parent_event_id, quando, node, kind, tier, provider, model,
                uso.tokens_in, uso.tokens_out,
                uso.tokens_cache_read, uso.tokens_cache_write, price_table_version,
                json.dumps(uso.bruto, ensure_ascii=False) if uso.bruto else None,
                custo_usd_minor, custo_usd_micro, expectation, confidence_ppm,
                inputs_digest, outputs_digest, tx_id, evaluation_json,
                stop_category, stop_reason,
            ),
        )
        event_id = int(cur.lastrowid)

        if tx_id is not None:
            # Enquanto aberta, a transacao ainda aceita este vinculo. Depois
            # de fechada nao ha `UPDATE` possivel - nem para isso.
            conn.execute(
                "UPDATE ledger_transaction SET agent_event_id = ? WHERE id = ?",
                (event_id, tx_id),
            )
            lancamentos = [
                (tx_id, mapa[contas.CAIXA_SIM], -custo_usd_minor, "reflexao"),
                (tx_id, mapa[contas.TESOURARIA_SIM], custo_usd_minor, "reflexao"),
            ]
            if dinheiro_real:
                lancamentos += [
                    (tx_id, mapa[contas.CAIXA_REAL], -custo_brl_minor, "inferencia"),
                    (tx_id, mapa[contas.DESPESA_INFERENCIA], custo_brl_minor,
                     "inferencia"),
                ]
            conn.executemany(
                "INSERT INTO ledger_entry (transaction_id, account_id,"
                " amount_minor, memo) VALUES (?,?,?,?)",
                lancamentos,
            )
            # Fechar dispara a conferencia de partidas dobradas.
            conn.execute(
                "UPDATE ledger_transaction SET posted_at = ? WHERE id = ?",
                (agora(), tx_id),
            )
        else:
            log.info("ledger.reflexao_sem_custo", extra={"event_id": event_id})

    log.info(
        "ledger.custo_reflexao",
        extra={
            "event_id": event_id,
            "transaction_id": tx_id,  # None quando nao houve custo
            "custo_usd_minor": custo_usd_minor,
            "custo_brl_minor": custo_brl_minor,
            "cache_read_informado": uso.tokens_cache_read is not None,
            "cache_write_informado": uso.tokens_cache_write is not None,
        },
    )
    return event_id, tx_id


# ---------------------------------------------------------------------------
# Carteira derivada e conferencias
# ---------------------------------------------------------------------------


def saldos(
    conn: sqlite3.Connection,
    book: str | None = None,
    *,
    run_id: int | None = None,
) -> list[dict]:
    """Saldo por conta, sempre derivado dos lancamentos.

    Com `run_id`, devolve a historia economica DAQUELE run. Sem ele, o livro
    inteiro. Os dois numeros sao legitimos e respondem perguntas diferentes -
    confundi-los faz um run herdar a carteira do anterior, que foi exatamente
    o defeito que o simulador revelou.
    """
    if run_id is not None:
        linhas = conn.execute(
            "SELECT * FROM account_balance_run WHERE run_id = ?"
            + (" AND book = ?" if book else "")
            + " ORDER BY code",
            (run_id, book) if book else (run_id,),
        ).fetchall()
        # A view so traz conta que teve movimento. Completa com as zeradas,
        # para que a carteira de um run novo nao tenha buraco.
        vistas = {l["code"] for l in linhas}
        faltantes = [
            dict(l) | {"run_id": run_id, "balance_minor": 0, "entries": 0}
            for l in conn.execute("SELECT * FROM account_balance ORDER BY code")
            if l["code"] not in vistas and (not book or l["book"] == book)
        ]
        return sorted(
            [dict(l) for l in linhas] + faltantes, key=lambda s: s["code"]
        )

    sql = "SELECT * FROM account_balance"
    parametros: tuple = ()
    if book is not None:
        sql += " WHERE book = ?"
        parametros = (book,)
    return [dict(l) for l in conn.execute(sql + " ORDER BY code", parametros)]


def saldo_da_conta(
    conn: sqlite3.Connection, code: str, *, run_id: int | None = None
) -> int:
    """Saldo de uma conta. Ponto unico de leitura, para nao haver duas formas."""
    if run_id is not None:
        linha = conn.execute(
            "SELECT balance_minor FROM account_balance_run"
            " WHERE run_id = ? AND code = ?",
            (run_id, code),
        ).fetchone()
    else:
        linha = conn.execute(
            "SELECT balance_minor FROM account_balance WHERE code = ?", (code,)
        ).fetchone()
    return int(linha["balance_minor"]) if linha else 0


def carteira(conn: sqlite3.Connection, *, run_id: int | None = None) -> dict:
    """Resumo economico dos dois livros. Com `run_id`, so daquele run."""
    por_conta = {s["code"]: s for s in saldos(conn, run_id=run_id)}
    return {
        "simulado_usd": {
            "caixa_minor": por_conta[contas.CAIXA_SIM]["balance_minor"],
            "posicao_btc_minor": por_conta[contas.POSICAO_BTC]["balance_minor"],
            "tesouraria_minor": por_conta[contas.TESOURARIA_SIM]["balance_minor"],
            # As QUATRO contas de custo. `sim.despesa.spread` foi criada
            # depois das outras tres e ficou de fora daqui, entao o painel
            # vinha subnotificando o custo de execucao em silencio.
            "custo_execucao_minor": sum(
                por_conta[c]["balance_minor"]
                for c in (
                    contas.DESPESA_TAXA,
                    contas.DESPESA_SPREAD,
                    contas.DESPESA_SLIPPAGE,
                    contas.DESPESA_PENALIDADE,
                )
            ),
        },
        "real_brl": {
            "caixa_minor": por_conta[contas.CAIXA_REAL]["balance_minor"],
            "despesa_inferencia_minor": por_conta[contas.DESPESA_INFERENCIA][
                "balance_minor"
            ],
        },
    }


def conferir_partidas_dobradas(conn: sqlite3.Connection) -> list[dict]:
    """Toda transacao fechada, em todo livro, soma zero? (criterio 1)

    Devolve as violacoes. Lista vazia e o unico resultado aceitavel.
    """
    return [
        dict(l)
        for l in conn.execute(
            """
            SELECT t.id AS transaction_id, a.book AS book,
                   SUM(e.amount_minor) AS soma
            FROM ledger_transaction t
            JOIN ledger_entry e ON e.transaction_id = t.id
            JOIN account a ON a.id = e.account_id
            WHERE t.posted_at IS NOT NULL
            GROUP BY t.id, a.book
            HAVING SUM(e.amount_minor) <> 0
            """
        )
    ]


def reconciliar(conn: sqlite3.Connection) -> list[dict]:
    """Recalcula todo saldo do zero e compara com o exibido (criterio 5).

    A recontagem e feita AQUI, em Python, e nao no mesmo SQL da view - senao
    a conferencia repetiria o eventual erro da view e concordaria com ele.
    Divergencia e erro, nunca ajuste: ajustar silenciosamente e como o
    experimento perde a capacidade de detectar que perdeu dinheiro.
    """
    recalculado: dict[int, int] = {}
    fechadas = {
        int(l["id"])
        for l in conn.execute(
            "SELECT id FROM ledger_transaction WHERE posted_at IS NOT NULL"
        )
    }
    for linha in conn.execute(
        "SELECT transaction_id, account_id, amount_minor FROM ledger_entry"
    ):
        if int(linha["transaction_id"]) in fechadas:
            conta = int(linha["account_id"])
            recalculado[conta] = recalculado.get(conta, 0) + int(linha["amount_minor"])

    divergencias = []
    for linha in conn.execute("SELECT account_id, code, balance_minor FROM account_balance"):
        esperado = recalculado.get(int(linha["account_id"]), 0)
        if int(linha["balance_minor"]) != esperado:
            divergencias.append(
                {
                    "code": linha["code"],
                    "exibido": int(linha["balance_minor"]),
                    "recalculado": esperado,
                }
            )
    return divergencias


def conferir_vinculo_inferencia(conn: sqlite3.Connection) -> dict[str, list[int]]:
    """Os dois registros se referenciam de verdade? (criterio 9)

    Duas perguntas distintas, e as duas precisam ter resposta vazia:
      - ha transacao de reflexao sem evento que a autorize?
      - ha evento com custo sem contrapartida no ledger?
    """
    orfas = [
        int(l["id"])
        for l in conn.execute(
            "SELECT id FROM ledger_transaction"
            " WHERE kind = 'reflexao' AND agent_event_id IS NULL"
        )
    ]
    sem_contrapartida = [
        int(l["id"])
        for l in conn.execute(
            "SELECT id FROM agent_event"
            " WHERE cost_usd_minor > 0 AND ledger_transaction_id IS NULL"
        )
    ]
    # Vinculo quebrado: o evento aponta para uma transacao que nao aponta de
    # volta. Uma seta so nao e vinculo.
    assimetricos = [
        int(l["id"])
        for l in conn.execute(
            "SELECT e.id AS id FROM agent_event e"
            " JOIN ledger_transaction t ON t.id = e.ledger_transaction_id"
            " WHERE t.agent_event_id IS NOT e.id"
        )
    ]
    return {
        "transacoes_sem_evento": orfas,
        "eventos_com_custo_sem_lancamento": sem_contrapartida,
        "vinculos_assimetricos": assimetricos,
    }


def conferir_arredondamento_do_custo(conn: sqlite3.Connection) -> list[dict]:
    """O custo lancado e o teto em centavos do custo exato? (incremento 5)

    Existe porque `cost_usd_micro` e `cost_usd_minor` descrevem a mesma
    quantia em duas precisoes, e valor que descreve outro valor sem ser
    conferido e como este projeto ja se enganou quatro vezes. Aqui a relacao
    e verificavel a qualquer momento, e nao so no instante da gravacao.
    """
    return [
        dict(l)
        for l in conn.execute(
            "SELECT id AS event_id, cost_usd_minor, cost_usd_micro"
            " FROM agent_event"
            " WHERE cost_usd_micro IS NOT NULL"
            "   AND cost_usd_minor <> (cost_usd_micro + 9999) / 10000"
        )
    ]


def gasto_com_reflexao(conn: sqlite3.Connection, run_id: int) -> dict:
    """Quanto o cerebro lento ja custou neste run, e quantas vezes falou.

    Sai do LEDGER, nunca de contador em memoria: o teto que protege o
    orcamento tem de ser lido da mesma fonte que registra o dinheiro, senao
    um processo reiniciado no meio de um run recomeca a contagem do zero e o
    teto deixa de ser teto (secao 12.1).

    Em centavos, e nao em micros, porque e o que esta lancado. Como cada
    chamada arredonda para cima, este numero e sempre maior ou igual ao gasto
    exato - o teto para mais cedo, nunca mais tarde.

    Conta o custo do LIVRO SIMULADO, e por isso inclui as respostas servidas
    do cache local, que nao custaram real nenhum. E de proposito, por duas
    razoes: o teto passa a valer identicamente num run frio e num run quente,
    o que mantem o comportamento reproduzivel; e como resultado ele conta
    algumas chamadas como pagas sem terem sido, o que so faz o limite de
    dinheiro real parar mais cedo. Errar para o lado de gastar menos e o
    unico erro aceitavel num teto (secao 12.1).
    """
    linha = conn.execute(
        "SELECT COUNT(*) AS chamadas,"
        "       COALESCE(SUM(cost_usd_minor), 0) AS gasto_cents,"
        "       COALESCE(SUM(cost_usd_micro), 0) AS gasto_micro"
        " FROM agent_event"
        " WHERE run_id = ? AND provider IS NOT NULL AND cost_usd_minor > 0",
        (run_id,),
    ).fetchone()
    real = conn.execute(
        "SELECT COALESCE(SUM(e.amount_minor), 0) AS brl"
        " FROM ledger_entry e"
        " JOIN ledger_transaction t ON t.id = e.transaction_id"
        " JOIN account a ON a.id = e.account_id"
        " WHERE t.run_id = ? AND t.kind = 'reflexao'"
        "   AND a.code = ?",
        (run_id, contas.DESPESA_INFERENCIA),
    ).fetchone()
    return {
        "chamadas_com_custo": int(linha["chamadas"]),
        "gasto_cents": int(linha["gasto_cents"]),
        "gasto_micro": int(linha["gasto_micro"]),
        # O que de fato saiu da conta, em centavos de BRL. Zero num run
        # inteiramente servido pelo cache - e e essa a prova do criterio 4.
        "gasto_real_brl_cents": int(real["brl"]),
    }


def colunas_em_ponto_flutuante(conn: sqlite3.Connection) -> list[str]:
    """Alguma coluna foi declarada REAL? (criterio 4)

    Le o SCHEMA, e nao uma lista escrita a mao: uma lista manual protege
    contra o que ja foi lembrado, e nao contra a coluna que alguem acrescentar
    amanha. Esta pergunta se responde sozinha para sempre.
    """
    suspeitas: list[str] = []
    tabelas = [
        l["name"]
        for l in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for tabela in tabelas:
        for coluna in conn.execute(f"PRAGMA table_info({tabela})"):
            tipo = (coluna["type"] or "").upper()
            if any(t in tipo for t in ("REAL", "FLOAT", "DOUBLE")):
                suspeitas.append(f"{tabela}.{coluna['name']} {tipo}")
    return suspeitas
