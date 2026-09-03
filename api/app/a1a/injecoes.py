"""As injeções: uma função por família de defeito de §14.4.

Cada uma **tenta fazer a coisa errada** e devolve o que aconteceu. Nenhuma
delas conhece o resultado esperado — quem declara o esperado é o catálogo, e
comparar as duas coisas é do relatório. Uma injeção que soubesse o que devia
acontecer poderia produzir o "aconteceu o esperado" sem que nada tivesse
acontecido, que é a forma de teste vazio deste projeto já registrada.

## Por que as injeções escrevem SQL cru

Pelo mesmo motivo que os testes de partidas dobradas do incremento 2 escrevem:
a guarda que se quer exercitar é do **banco**. Uma injeção que passasse pela
função Python seria barrada pela validação em Python, e o controle acabaria
provando que o Python valida — que não é o que §14.4 pede, e não é o que
protege quando alguém escreve um `INSERT` novo em outro lugar.

Onde a guarda é de Python (a finalidade do agente, o enum de métrica), a
injeção chama a função Python: ali a fronteira é o próprio código.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class Tentativa:
    """Uma tentativa dentro de um controle. `barrada` é o fato observado."""

    o_que: str
    barrada: bool
    mecanismo: str | None = None
    detalhe: dict = field(default_factory=dict)

    def como_dict(self) -> dict:
        return {
            "o_que": self.o_que,
            "barrada": self.barrada,
            "mecanismo": self.mecanismo,
            "detalhe": self.detalhe,
        }


def _transacao_do_run(conn: sqlite3.Connection, run_id: int) -> int:
    """Uma transação do run, para a FK de `execution`.

    A do capital semente serve: a injeção é sobre o preço e a latência, e não
    sobre o vínculo com o ledger. Inventar uma transação nova para a injeção
    seria injetar dois defeitos e não saber qual guarda disparou.
    """
    linha = conn.execute(
        "SELECT MIN(id) AS id FROM ledger_transaction WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if linha is None or linha["id"] is None:
        raise ValueError(f"o run {run_id} nao tem transacao no ledger")
    return int(linha["id"])


def _tentar(o_que: str, acao) -> Tentativa:
    """Roda a ação e registra se ela foi recusada, e por quem.

    `Exception` de propósito, e não uma lista de tipos: o controle existe para
    descobrir se ALGUMA guarda barra a injeção, e restringir os tipos aqui
    faria uma guarda nova, de tipo diferente, aparecer como "não barrou".

    O tipo da exceção vai no mecanismo, então a informação não se perde.
    """
    try:
        detalhe = acao() or {}
    except Exception as erro:  # noqa: BLE001 - ver docstring
        return Tentativa(
            o_que=o_que,
            barrada=True,
            mecanismo=f"{type(erro).__name__}: {erro}",
        )
    return Tentativa(o_que=o_que, barrada=False, detalhe=detalhe)


# ---------------------------------------------------------------------------
# 1. Acesso explícito ao futuro
# ---------------------------------------------------------------------------


def acesso_ao_futuro(
    conn: sqlite3.Connection, *, dataset_id: int, run_id: int, decision_ts_ms: int
) -> list[Tentativa]:
    """Ler o que é do validador, e executar na barra em que se decidiu."""
    from ..dataset import loader

    def ler_walk_forward():
        barras = loader.carregar(
            conn,
            dataset_id,
            decision_ts_ms=decision_ts_ms,
            finalidade="walk_forward",
        )
        return {"barras_devolvidas": len(barras)}

    def executar_na_barra_da_decisao():
        # A latência é estrutural: `CHECK (execution_bar_ms > decision_bar_ms)`.
        # Executar na barra da decisão é conhecer a máxima, a mínima e o
        # fechamento dela no instante em que se decide — o vazamento mais
        # difícil de ver, porque a execução parece legítima.
        conn.execute(
            "INSERT INTO execution (run_id, dataset_id, decision_bar_ms,"
            " execution_bar_ms, side, quantity_sats, price_ref, price_exec,"
            " notional_ref_cents, fee_cents, spread_cents, slippage_cents,"
            " penalty_cents, fidelity_level, ledger_transaction_id)"
            " VALUES (?,?,?,?,'compra',1,100,100,1,0,0,0,0,1,?)",
            (
                run_id, dataset_id, decision_ts_ms, decision_ts_ms,
                _transacao_do_run(conn, run_id),
            ),
        )
        return {"gravou": True}

    return [
        _tentar("ler walk-forward pelo caminho do agente", ler_walk_forward),
        _tentar(
            "gravar execucao na mesma barra da decisao",
            executar_na_barra_da_decisao,
        ),
    ]


# ---------------------------------------------------------------------------
# 4. Violação conhecida do embargo
# ---------------------------------------------------------------------------


def violacao_do_embargo(
    conn: sqlite3.Connection, *, dataset_id: int
) -> list[Tentativa]:
    """Duas janelas que atravessam a fronteira treino/teste.

    A grade vem de `janelas.marcos`, e não de um `SELECT ... FROM bar` aqui: a
    guarda do incremento 9 recusa consulta a barra fora do módulo do dataset, e
    ela está certa em recusar — o controle vizinho testa exatamente essa
    fronteira, e furá-la aqui seria o controle violando o que ele mede.
    """
    from ..dataset import janelas

    grade = janelas.marcos(conn, dataset_id, 4)
    if len(grade) < 4:
        return [
            Tentativa(
                o_que="janela invalida de walk-forward",
                barrada=False,
                mecanismo=None,
                detalhe={"nao_injetado": "o dataset nao tem barras suficientes"},
            )
        ]
    a, b, c, d = grade[0], grade[1], grade[2], grade[3]

    def inserir(ordem: int, purga: int, embargo: int, teste_de: int):
        def acao():
            conn.execute(
                "INSERT INTO walk_forward_window (dataset_id, ordem,"
                " treino_de_ms, treino_ate_ms, teste_de_ms, teste_ate_ms,"
                " purga_barras, embargo_barras, purga_origem, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
                (
                    dataset_id, ordem, a, b, teste_de, d,
                    purga, embargo, "controle-a1a",
                ),
            )
            return {"gravou": True}

        return acao

    return [
        # Purga ZERO: o CHECK da tabela e `purga_barras >= 0`, e a conferencia
        # de leitura compara "removidas < purga + embargo" - com os dois em
        # zero ela nao tem o que acusar.
        _tentar(
            "janela com purga declarada ZERO",
            inserir(9_001, purga=0, embargo=0, teste_de=b),
        ),
        # Purga declarada MAIOR que o intervalo de fato removido: a declaracao
        # vira enfeite, e a janela vaza treino para dentro do teste.
        _tentar(
            "janela que declara purga maior que o intervalo que remove",
            inserir(9_002, purga=400, embargo=4, teste_de=c),
        ),
    ]


# ---------------------------------------------------------------------------
# 5. Preço impossível no nível de fidelidade declarado
# ---------------------------------------------------------------------------


def preco_impossivel(
    conn: sqlite3.Connection, *, dataset_id: int, run_id: int, decision_ts_ms: int
) -> list[Tentativa]:
    """Um preenchimento MELHOR que a referência adversa — o maker da fidelidade 1.

    §8.4.1.1 proíbe afirmar fidelidade de book: em fidelidade 1 não há como
    dizer que a ordem teria sido preenchida melhor que o limite adverso. Uma
    execução generosa é a forma exata desse defeito, e é a mais silenciosa —
    ela produz resultado melhor sem que nenhuma linha diga que houve otimismo.
    """

    def generosa(lado: str, price_ref: int, price_exec: int):
        def acao():
            conn.execute(
                "INSERT INTO execution (run_id, dataset_id, decision_bar_ms,"
                " execution_bar_ms, side, quantity_sats, price_ref,"
                " price_exec, notional_ref_cents, fee_cents, spread_cents,"
                " slippage_cents, penalty_cents, fidelity_level,"
                " ledger_transaction_id)"
                " VALUES (?,?,?,?,?,1,?,?,1,0,0,0,0,1,?)",
                (
                    run_id, dataset_id, decision_ts_ms,
                    decision_ts_ms + 900_000, lado, price_ref, price_exec,
                    _transacao_do_run(conn, run_id),
                ),
            )
            return {"gravou": True}

        return acao

    comprar_barato = generosa("compra", 1_000, 900)
    vender_caro = generosa("venda", 1_000, 1_100)

    return [
        _tentar("compra preenchida ABAIXO da referencia adversa", comprar_barato),
        _tentar("venda preenchida ACIMA da referencia adversa", vender_caro),
    ]


# ---------------------------------------------------------------------------
# 6. Adulteração proposital do ledger
# ---------------------------------------------------------------------------


def ledger_adulterado(
    conn: sqlite3.Connection, *, run_id: int
) -> list[Tentativa]:
    """Alterar dinheiro já gravado, e fechar transação desequilibrada.

    O run já nasceu com o capital semente lançado (`abrir_run`), então há o
    que adulterar sem precisar montar nada antes — e é justamente o lançamento
    que todo resultado do run parte.
    """

    def alterar_lancamento():
        linha = conn.execute(
            "SELECT e.id, e.amount_minor FROM ledger_entry e"
            " JOIN ledger_transaction t ON t.id = e.transaction_id"
            " WHERE t.run_id = ? ORDER BY e.id LIMIT 1",
            (run_id,),
        ).fetchone()
        if linha is None:
            return {"nao_injetado": "o run nao tem lancamento"}
        conn.execute(
            "UPDATE ledger_entry SET amount_minor = ? WHERE id = ?",
            (int(linha["amount_minor"]) + 100_000, int(linha["id"])),
        )
        return {"alterou": True}

    def apagar_lancamento():
        linha = conn.execute(
            "SELECT e.id FROM ledger_entry e"
            " JOIN ledger_transaction t ON t.id = e.transaction_id"
            " WHERE t.run_id = ? ORDER BY e.id LIMIT 1",
            (run_id,),
        ).fetchone()
        if linha is None:
            return {"nao_injetado": "o run nao tem lancamento"}
        conn.execute("DELETE FROM ledger_entry WHERE id = ?", (int(linha["id"]),))
        return {"apagou": True}

    def fechar_desequilibrada():
        # Pela MESMA funcao que o resto do sistema usa para lancar. O
        # fechamento e onde o banco confere as partidas dobradas, e e ele que
        # tem de recusar - nao uma validacao em Python, que um caminho de
        # escrita novo nao herdaria.
        from ..ledger import contas
        from ..ledger.livro import Lancamento, registrar

        registrar(
            conn,
            kind="ajuste",
            run_id=run_id,
            lancamentos=[
                Lancamento(
                    contas.CAIXA_SIM, 100_000, "credito sem contrapartida"
                )
            ],
            memo="controle a1a: adulteracao do ledger",
        )
        return {"fechou": True}

    return [
        _tentar("alterar um lancamento ja gravado", alterar_lancamento),
        _tentar("apagar um lancamento ja gravado", apagar_lancamento),
        _tentar(
            "fechar transacao com as partidas desequilibradas",
            fechar_desequilibrada,
        ),
    ]


# ---------------------------------------------------------------------------
# 3a. Métrica primária sem custo
# ---------------------------------------------------------------------------


def metrica_sem_custo() -> list[Tentativa]:
    """Declarar um alvo que o ledger não mede.

    Toda métrica do enum sai do ledger, e o ledger é líquido por construção:
    cada taxa, spread, slippage e penalidade é uma linha própria (critério 3 do
    incremento 3). Não existe métrica bruta a declarar — e é essa ausência que
    impede uma hipótese de ser sobre um lucro que não existiu.
    """
    from ..hipotese.schema import ClausulaFalseamento, PreRegistroBruto

    def declarar():
        PreRegistroBruto(
            enunciado=(
                "CONTROLE NEGATIVO DETERMINISTICO (A1a, secao 14.4). Alvo"
                " declarado sobre lucro BRUTO, antes de taxas, spread,"
                " slippage e penalidade."
            ),
            metrica_primaria="lucro_bruto_cents",
            efeito_minimo=1,
            sharpe_esperado_milesimos=1_000,
            criterio_parada="fim_da_janela",
            condicoes_falseamento=[
                ClausulaFalseamento(
                    metrica="lucro_bruto_cents", comparador="menor_que", valor=1
                )
            ],
        )
        return {"aceitou": True}

    return [_tentar("declarar metrica primaria sem custo", declarar)]
