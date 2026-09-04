"""§14.4.1: aprovar no Portão B dispara **auditoria**, não comemoração.

> "Em fidelidade 1–2, com um agente e apenas evidência retrospectiva, a
> probabilidade a priori de encontrar edge real em mercado líquido é baixa. A
> probabilidade de um bug produzir o mesmo sinal é maior. Portanto, **passar no
> Portão B é tratado como suspeita de defeito até prova em contrário**." —
> §14.4.1

O documento lista quatro obrigações, e o critério 5 do incremento 14 exige que
elas sejam **código executável, não texto**. São elas, na ordem em que §14.4.1
as escreve:

1. reexecutar com a semente de aleatoriedade alterada e com o período
   reservado trocado;
2. procurar vazamento temporal ativamente, incluindo revisão manual das **cinco
   operações mais lucrativas** — resultado concentrado em poucas operações é o
   padrão típico de bug, não de edge;
3. verificar se o resultado sobrevive dobrando as premissas de custo, spread e
   slippage;
4. confirmar que a estratégia vencedora não depende de preço, volume ou horário
   que o modo pessimista deveria ter proibido.

## O que este módulo NÃO faz, e por quê

**A revisão manual continua manual.** O item 2 diz "revisão manual", e um
módulo que devolvesse "revisado: ok" trocaria a revisão pela alegação de tê-la
feito. O que ele faz é **preparar a revisão**: lista as cinco operações mais
lucrativas com barra, preço de referência, preço executado e quanto cada uma
pesou no resultado, mais a concentração — que é o número que diz se vale
desconfiar.

**Nada aqui promove nem reprova.** A auditoria produz evidência para uma pessoa
decidir. §14.4.1 termina com "só depois disso o resultado entra em quarentena
forward", e quarentena é 0C.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from ..cerebro import avaliacao
from ..config.schema import ExperimentConfig
from ..hipotese import registro as hipotese_registro
from ..ledger import livro
from ..maos_rapidas import baselines, executor
from ..regra import registro as registro_de_regra
from ..simulador import execucao as simulador
from ..validador import forward

log = logging.getLogger(__name__)

#: Quantas operações a revisão manual olha. Cinco é do documento, literal —
#: não é um número que escolhemos.
QUANTAS_OPERACOES = 5

#: O dono dos runs de auditoria. Nunca se confunde com o do agente, e o filtro
#: que garante isso é positivo (`ciclo.ultimo_run_do_agente`).
AGENT_ID = "auditoria"


class SemRun(Exception):
    """Não há o que auditar."""


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 2. As cinco operações mais lucrativas, e a concentração
# ---------------------------------------------------------------------------


def cinco_mais_lucrativas(
    conn: sqlite3.Connection, run_id: int
) -> dict:
    """Prepara a revisão manual do item 2. **Não a substitui.**

    "Resultado concentrado em poucas operações é o padrão típico de bug, não de
    edge" — então o número que interessa é a **fração do ganho bruto que veio
    das cinco maiores**. Uma estratégia cujo resultado inteiro sai de cinco
    entradas num período de meses é suspeita antes de ser boa.

    O ganho de cada ida e volta sai do ledger: `sim.resultado.realizado` recebe
    o resultado de cada venda, positivo quando é prejuízo (coerente com
    despesa). Aqui o sinal é invertido para que "lucro" seja positivo, e isso
    está dito porque um sinal trocado em silêncio é como se lê um resultado ao
    contrário.
    """
    linhas = [
        {
            "transaction_id": int(l["tx"]),
            "execution_bar_ms": int(l["bar"]),
            "lucro_cents": -int(l["valor"]),
            "price_ref": int(l["price_ref"]),
            "price_exec": int(l["price_exec"]),
            "quantity_sats": int(l["qty"]),
        }
        for l in conn.execute(
            "SELECT t.id AS tx, e.execution_bar_ms AS bar,"
            "       le.amount_minor AS valor, e.price_ref AS price_ref,"
            "       e.price_exec AS price_exec, e.quantity_sats AS qty"
            "  FROM ledger_entry le"
            "  JOIN ledger_transaction t ON t.id = le.transaction_id"
            "  JOIN account a ON a.id = le.account_id"
            "  JOIN execution e ON e.ledger_transaction_id = t.id"
            " WHERE t.run_id = ? AND a.code = 'sim.resultado.realizado'"
            " ORDER BY le.amount_minor ASC",
            (run_id,),
        )
    ]
    ganhos = [l for l in linhas if l["lucro_cents"] > 0]
    total_ganho = sum(l["lucro_cents"] for l in ganhos)
    top = ganhos[:QUANTAS_OPERACOES]
    return {
        "operacoes": top,
        "quantas_lucrativas": len(ganhos),
        "ganho_bruto_total_cents": total_ganho,
        "ganho_das_cinco_cents": sum(l["lucro_cents"] for l in top),
        "concentracao_ppm": (
            sum(l["lucro_cents"] for l in top) * 1_000_000 // total_ganho
            if total_ganho
            else None
        ),
        "por_que_importa": (
            "resultado concentrado em poucas operacoes e o padrao tipico de"
            " bug, nao de edge (§14.4.1)"
        ),
        "a_revisao_continua_manual": (
            "§14.4.1 pede REVISAO MANUAL destas operacoes. Este bloco a"
            " prepara; um campo 'revisado: ok' trocaria a revisao pela"
            " alegacao de te-la feito"
        ),
    }


# ---------------------------------------------------------------------------
# 3. Dobrar custo, spread e slippage
# ---------------------------------------------------------------------------


def _config_dobrada(config: ExperimentConfig) -> ExperimentConfig:
    """As três premissas de §14.4.1, dobradas. Nada mais é tocado."""
    return config.model_copy(
        update={
            "taker_fee_bps": config.taker_fee_bps * 2,
            "spread_bps": config.spread_bps * 2,
            "slippage_bps": config.slippage_bps * 2,
        }
    )


def sobrevive_ao_custo_dobrado(
    conn: sqlite3.Connection,
    *,
    hypothesis_id: int,
    dataset_id: int,
    config: ExperimentConfig,
    config_version_id: int,
) -> dict:
    """Reexecuta a regra com custo, spread e slippage dobrados.

    **Num run próprio, e marcado como auditoria.** O resultado não é do agente
    e não pode aparecer como se fosse: ele roda sob premissas que a
    `config_version` do experimento não declara, e por isso não é comparável
    com nada além de si mesmo.
    """
    hip = hipotese_registro.por_id(conn, hypothesis_id)
    if hip is None:
        raise SemRun(f"hipotese {hypothesis_id} nao existe")
    _, regra = forward._regra_da_hipotese(conn, hip, config)
    dobrada = _config_dobrada(config)

    barras = executor.carregar_janela(
        conn, dataset_id, finalidade=executor.FINALIDADE_DE_EXECUCAO
    )
    run_id, _ = livro.abrir_run(
        conn,
        config_version_id=config_version_id,
        seed_capital_usd_cents=config.seed_capital_usd_cents,
        agent_id=AGENT_ID,
    )
    rule_id = registro_de_regra.registrar(conn, regra)
    resultado = executor.rodar(
        conn, run_id=run_id, dataset_id=dataset_id, regra=regra,
        rule_id=rule_id, config=dobrada, barras=barras,
    )
    livro.encerrar_run(conn, run_id, "concluido")

    patrimonio = simulador.caixa_cents(conn, run_id)
    original = simulador.caixa_cents(conn, int(hip["run_id"]))
    return {
        "run_id": run_id,
        "premissas_dobradas": {
            "taker_fee_bps": [str(config.taker_fee_bps), str(dobrada.taker_fee_bps)],
            "spread_bps": [str(config.spread_bps), str(dobrada.spread_bps)],
            "slippage_bps": [str(config.slippage_bps), str(dobrada.slippage_bps)],
        },
        "patrimonio_original_cents": original,
        "patrimonio_dobrado_cents": patrimonio,
        "idas_e_voltas": resultado.operacoes,
        "sobrevive": patrimonio > config.seed_capital_usd_cents,
        "por_que_importa": (
            "se o resultado desaparece ao dobrar premissas que ja sao"
            " pessimistas, ele estava vivendo da margem entre o modelo de"
            " custo e o mundo (§14.4.1)"
        ),
    }


# ---------------------------------------------------------------------------
# 1. Semente alterada — e o que ela de fato muda
# ---------------------------------------------------------------------------


def com_semente_alterada(
    conn: sqlite3.Connection,
    *,
    hypothesis_id: int,
    dataset_id: int,
    config: ExperimentConfig,
    config_version_id: int,
    semente: int,
) -> dict:
    """Reexecuta o **controle** com outra semente, e não a regra.

    §14.4.1 pede "reexecutar o experimento com a semente de aleatoriedade
    alterada". Escrever isso como "rodar a regra de novo com outra semente"
    produziria exatamente o mesmo resultado e um bloco de auditoria que sempre
    diz "sobreviveu" — porque **a regra é determinística**: mesmas barras,
    mesmos sinais, mesmas execuções, mesmo digest.

    O que a semente move é o **B1 casado**: ele sorteia os pares de entrada e
    saída. Então a pergunta que a semente responde é a que interessa — *o
    resultado continua no mesmo lugar de uma distribuição do acaso sorteada de
    outro jeito?*

    Uma auditoria que reexecutasse a parte determinística estaria conferindo
    que `f(x) = f(x)`.
    """
    hip = hipotese_registro.por_id(conn, hypothesis_id)
    if hip is None:
        raise SemRun(f"hipotese {hypothesis_id} nao existe")
    run_id = int(hip["run_id"])
    patrimonio = simulador.caixa_cents(conn, run_id)
    giro = executor.idas_e_voltas(conn, run_id)
    if giro <= 0:
        return {
            "executada": False,
            "por_que": (
                "o run nao teve ida e volta nenhuma; nao ha giro com que casar"
                " o controle, e sortear zero pares nao produz distribuicao"
            ),
        }

    _, regra = forward._regra_da_hipotese(conn, hip, config)
    alvo, _ = livro.abrir_run(
        conn,
        config_version_id=config_version_id,
        seed_capital_usd_cents=config.seed_capital_usd_cents,
        agent_id=AGENT_ID,
    )
    livro.encerrar_run(conn, alvo, "concluido")
    novo = baselines.b1_casado_com(
        conn,
        dataset_id=dataset_id,
        config=config,
        config_version_id=config_version_id,
        operacoes_alvo=giro,
        fracao_bps=regra.position_fraction_bps,
        semente=semente,
        casa_run_id=alvo,
    )
    original = baselines.b1_do_run(conn, run_id)

    def _faixa(dist: dict | None) -> str | None:
        if dist is None:
            return None
        return avaliacao.faixa_contra_o_acaso(patrimonio, dist)

    return {
        "executada": True,
        "semente_original": config.default_seed,
        "semente_alternativa": semente,
        "o_que_a_semente_move": (
            "o B1 casado, que sorteia os pares de entrada e saida. A REGRA e"
            " deterministica: reexecuta-la com outra semente daria o mesmo"
            " digest, e o bloco diria 'sobreviveu' sempre"
        ),
        "faixa_original": _faixa(original),
        "faixa_com_outra_semente": _faixa(novo),
        "p50_original_cents": (original or {}).get("p50"),
        "p50_alternativo_cents": novo["p50"],
        "patrimonio_cents": patrimonio,
        "mesma_leitura": (
            None
            if original is None
            else _faixa(original) == _faixa(novo)
        ),
    }


# ---------------------------------------------------------------------------
# 4. A vencedora depende de algo que o modo pessimista deveria ter proibido?
# ---------------------------------------------------------------------------


def nao_depende_do_proibido(
    conn: sqlite3.Connection, run_id: int, config: ExperimentConfig
) -> dict:
    """Confere, por consulta, que nada no run vive de fora do modo pessimista.

    Três perguntas, e as três sobre o que ficou **gravado**:

    - alguma execução foi preenchida melhor que a referência adversa? (o
      preenchimento maker que a fidelidade 1 não pode afirmar);
    - alguma execução aconteceu na barra da decisão? (a latência é estrutural);
    - alguma execução declarou fidelidade acima da configurada? (o nível viaja
      junto do número desde o incremento 3, e afirmar mais do que o dado tem é
      o que §8.4.1 proíbe).
    """
    generosas = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM execution WHERE run_id = ?"
            "   AND ((side = 'compra' AND price_exec < price_ref)"
            "     OR (side = 'venda'  AND price_exec > price_ref))",
            (run_id,),
        ).fetchone()["n"]
    )
    sem_latencia = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM execution"
            " WHERE run_id = ? AND execution_bar_ms <= decision_bar_ms",
            (run_id,),
        ).fetchone()["n"]
    )
    fidelidade_alta = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM execution"
            " WHERE run_id = ? AND fidelity_level > ?",
            (run_id, config.fidelity_level),
        ).fetchone()["n"]
    )
    conferencias = {
        "nenhum_preenchimento_generoso": generosas == 0,
        "latencia_respeitada_em_todas": sem_latencia == 0,
        "fidelidade_nunca_acima_da_declarada": fidelidade_alta == 0,
    }
    return {
        "conferencias": conferencias,
        "execucoes_generosas": generosas,
        "execucoes_sem_latencia": sem_latencia,
        "execucoes_com_fidelidade_acima": fidelidade_alta,
        "fidelity_level_declarado": config.fidelity_level,
        "ok": all(conferencias.values()),
        "limite": (
            "isto confere o que o SIMULADOR gravou. Volume e horario nao"
            " entram porque a fidelidade 1 nao modela nem um nem outro - e"
            " §8.4.1 proibe afirmar fidelidade de book, entao uma conferencia"
            " sobre eles afirmaria ter medido o que o dado nao tem"
        ),
    }


# ---------------------------------------------------------------------------
# O roteiro inteiro
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pedido:
    hypothesis_id: int
    dataset_id: int
    semente_alternativa: int


def montar(
    conn: sqlite3.Connection,
    *,
    hypothesis_id: int,
    dataset_id: int | None,
    config: ExperimentConfig,
    config_version_id: int,
    executar: bool = False,
    semente_alternativa: int | None = None,
) -> dict:
    """O roteiro de §14.4.1. `executar=False` só descreve e mede o que já existe.

    A separação existe porque dois dos quatro itens **escrevem**: reexecutar
    com semente trocada e reexecutar com custo dobrado abrem runs. Uma rota de
    leitura que os disparasse a cada carregamento do painel encheria o registro
    append-only de runs que ninguém pediu.
    """
    hip = hipotese_registro.por_id(conn, hypothesis_id)
    if hip is None:
        raise SemRun(f"hipotese {hypothesis_id} nao existe")
    run_id = int(hip["run_id"])

    saida: dict = {
        "gerado_em": _agora(),
        "hypothesis_id": hypothesis_id,
        "run_id": run_id,
        "postura": (
            "passar no Portao B e tratado como SUSPEITA DE DEFEITO ate prova"
            " em contrario: a probabilidade de um bug produzir o sinal e maior"
            " que a de haver edge real em fidelidade 1-2 (§14.4.1)"
        ),
        # Item 2 e item 4 so LEEM - podem rodar sempre.
        "revisao_das_cinco_mais_lucrativas": cinco_mais_lucrativas(conn, run_id),
        "nao_depende_do_proibido": nao_depende_do_proibido(conn, run_id, config),
    }

    if not executar or dataset_id is None:
        saida["reexecucao"] = {
            "executada": False,
            "por_que": (
                "reexecutar com semente trocada e com custo dobrado ABRE RUNS,"
                " e o registro e append-only: uma rota de leitura que"
                " disparasse isso encheria o banco de runs que ninguem pediu."
                " Peca a execucao explicitamente"
            ),
        }
        return saida

    semente = (
        semente_alternativa
        if semente_alternativa is not None
        else config.default_seed + 1
    )
    saida["reexecucao"] = {
        "executada": True,
        # Item 1: semente alterada. O que ela move e o CONTROLE, e nao a
        # regra - ver `com_semente_alterada`.
        "com_semente_alterada": com_semente_alterada(
            conn,
            hypothesis_id=hypothesis_id,
            dataset_id=dataset_id,
            config=config,
            config_version_id=config_version_id,
            semente=semente,
        ),
        # A reserva trocada NAO entra, e o motivo esta escrito abaixo.
        "periodo_reservado_trocado": False,
        "por_que_a_reserva_nao_e_trocada": (
            "trocar o periodo reservado consumiria o holdout, que tem USO"
            " UNICO por hipotese (§8.5.1). A auditoria aconteceria as custas"
            " do teste final que ela existe para proteger - e um holdout"
            " consumido nao se recupera. Fica declarado, e nao feito em"
            " silencio: e divergencia do roteiro de §14.4.1, levantada"
        ),
        # Item 3: premissas dobradas.
        "custo_dobrado": sobrevive_ao_custo_dobrado(
            conn,
            hypothesis_id=hypothesis_id,
            dataset_id=dataset_id,
            config=config,
            config_version_id=config_version_id,
        ),
    }
    return saida
