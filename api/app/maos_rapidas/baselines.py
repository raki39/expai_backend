"""Os tres baselines da secao 14.3, sobre a mesma janela e o mesmo simulador.

| | O que mede |
|---|---|
| **B1 aleatorio** | quanto do resultado e sorte |
| **B2 buy and hold** | se o agente adiciona algo sobre exposicao passiva |
| **B3 heuristica fixa** | se o LLM adiciona algo sobre uma regra trivial |

**Paridade (criterio 6).** Os tres usam o mesmo dimensionamento, as mesmas
taxas e o mesmo nucleo de precificacao - literalmente as mesmas funcoes de
`simulador.execucao`, e nao copias. Se cada um tivesse a sua, a paridade
dependeria de as copias continuarem iguais.

**Por que B1 nao passa pelo ledger.** A secao 14.3 exige no minimo mil
repeticoes, e mil historias completas seriam da ordem de milhoes de
lancamentos imutaveis para produzir tres numeros: p5, p50 e p95. As
repeticoes rodam pelo nucleo puro e cada uma vira uma linha de
`baseline_result`. Uma repeticao representativa TAMBEM roda o caminho
persistido inteiro, e ha teste exigindo que os dois cheguem ao mesmo centavo -
sem isso, "mesmo simulador" seria afirmacao e nao fato.

**Quantas operacoes faz cada repeticao de B1 (D19).** As mesmas que o B3 fez
na mesma janela. A secao 8.4.1.3 diz que o prejuizo do aleatorio e
proporcional ao NUMERO DE OPERACOES; se B1 girasse menos, a diferenca entre
ele e B3 mediria arrasto de custo em vez de timing, e qualquer estrategia
ganharia de B1 simplesmente operando menos.
"""

from __future__ import annotations

import hashlib
import logging
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from ..config.schema import ExperimentConfig
from ..dataset.loader import BarraCarregada
from ..regra import registro
from ..regra.schema import (
    CondicoesValidade,
    CruzamentoMedias,
    Regra,
)
from ..simulador import execucao as simulador
from . import executor

log = logging.getLogger(__name__)

MIN_REPETICOES = 1_000  # piso da secao 14.3


@dataclass(frozen=True)
class ResultadoSimulado:
    """Uma repeticao rodada em memoria, pelo nucleo puro."""

    operacoes: int
    equity_final_cents: int
    fee: int
    spread: int
    slippage: int
    penalty: int

    @property
    def custo_total(self) -> int:
        return self.fee + self.spread + self.slippage + self.penalty


def derivar_semente(base: int, indice: int) -> int:
    """Semente da repeticao, derivada deterministicamente da do run.

    SHA-256 em vez de aritmetica simples: `base + i` produz sementes vizinhas,
    e geradores com sementes vizinhas correlacionam no comeco da sequencia -
    o que estreitaria a distribuicao de B1 e faria o p95 parecer mais dificil
    de bater do que e.
    """
    digest = hashlib.sha256(f"{base}:{indice}".encode()).digest()
    # 63 bits, e nao 64: o INTEGER do SQLite e assinado, e um valor acima de
    # 2^63-1 estoura na gravacao. A semente precisa caber onde vai ser
    # guardada, senao a distribuicao nao e reproduzivel a partir do banco.
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def condicoes(config: ExperimentConfig) -> CondicoesValidade:
    return CondicoesValidade(
        venue=config.market_venue,
        symbol=config.market_symbol,
        timeframe=config.timeframe,
        fidelity_level=config.fidelity_level,
    )


# ---------------------------------------------------------------------------
# Nucleo em memoria: a mesma aritmetica do caminho persistido
# ---------------------------------------------------------------------------


def simular_pares(
    barras: Sequence[BarraCarregada],
    pares: Sequence[tuple[int, int]],
    *,
    caixa_inicial_cents: int,
    config: ExperimentConfig,
    fracao_bps: int,
) -> ResultadoSimulado:
    """Roda idas e voltas em memoria. `pares` sao indices de DECISAO.

    A execucao acontece em `indice + latency_bars`, exatamente como no
    caminho persistido - a latencia nao e detalhe da persistencia, e do
    desenho do simulador.
    """
    caixa = caixa_inicial_cents
    fracao = Decimal(fracao_bps) / Decimal(10_000)
    fee = spread = slippage = penalty = 0
    operacoes = 0
    lat = config.latency_bars

    for entrada, saida in pares:
        barra_e = barras[entrada + lat]
        barra_s = barras[saida + lat]

        ref_c = simulador.preco_referencia(barra_e, "compra", config)
        exec_c = simulador.preco_executado(ref_c, "compra", config)
        orcamento = int(Decimal(caixa) * fracao)
        qty = simulador.dimensionar(orcamento, exec_c, config)
        if qty <= 0:
            continue

        nocional_c, custos_c = simulador.custear(qty, ref_c, exec_c, "compra", config)
        caixa -= nocional_c + custos_c.de_preco + custos_c.fee

        ref_v = simulador.preco_referencia(barra_s, "venda", config)
        exec_v = simulador.preco_executado(ref_v, "venda", config)
        nocional_v, custos_v = simulador.custear(qty, ref_v, exec_v, "venda", config)
        caixa += nocional_v - custos_v.de_preco - custos_v.fee

        fee += custos_c.fee + custos_v.fee
        spread += custos_c.spread + custos_v.spread
        slippage += custos_c.slippage + custos_v.slippage
        penalty += custos_c.penalty + custos_v.penalty
        operacoes += 1

    return ResultadoSimulado(
        operacoes=operacoes,
        equity_final_cents=caixa,
        fee=fee,
        spread=spread,
        slippage=slippage,
        penalty=penalty,
    )


def sortear_pares(
    semente: int, ultima_decidivel: int, primeira: int, operacoes: int
) -> list[tuple[int, int]]:
    """Momentos aleatorios de entrada e saida, sem sobreposicao.

    Sem sobreposicao porque D1 fixou long/flat: nao ha como estar comprado
    duas vezes. Sortear e depois descartar sobreposicao enviesaria para
    posicoes curtas, entao os pontos sao sorteados de uma vez e ordenados.
    """
    rng = random.Random(semente)
    disponiveis = ultima_decidivel - primeira + 1
    necessarios = operacoes * 2
    if necessarios > disponiveis:
        raise ValueError(
            f"{operacoes} operacoes exigem {necessarios} pontos, e ha "
            f"{disponiveis} barras decidiveis"
        )
    pontos = sorted(rng.sample(range(primeira, ultima_decidivel + 1), necessarios))
    return [(pontos[i], pontos[i + 1]) for i in range(0, necessarios, 2)]


# ---------------------------------------------------------------------------
# B3 - heuristica fixa, congelada
# ---------------------------------------------------------------------------


def regra_b3(config: ExperimentConfig) -> Regra:
    """Cruzamento SMA, parametros vindos da configuracao versionada (D4)."""
    return Regra(
        params=CruzamentoMedias(rapida=config.b3_fast, lenta=config.b3_slow),
        position_fraction_bps=10_000,
        stop_loss_bps=None,
        condicoes_validade=condicoes(config),
    )


def rodar_b3(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    dataset_id: int,
    config: ExperimentConfig,
    barras: Sequence[BarraCarregada] | None = None,
) -> executor.ResultadoRegra:
    """Roda B3 pelo caminho persistido e CONGELA a regra.

    Congelar aqui, e nao depois: o criterio 5 exige que os parametros estejam
    fixados antes do primeiro run do agente, e o trigger do banco passa a
    recusar qualquer alteracao. Tunar B3 depois de ver o resultado do agente
    destroi o grupo de controle - e a partir do congelamento isso e impossivel,
    nao apenas desaconselhado.
    """
    regra = regra_b3(config)
    rule_id = registro.registrar(conn, regra)
    registro.congelar(conn, rule_id)
    return executor.rodar(
        conn, run_id=run_id, dataset_id=dataset_id, regra=regra,
        rule_id=rule_id, config=config, barras=barras,
    )


# ---------------------------------------------------------------------------
# B2 - buy and hold
# ---------------------------------------------------------------------------


def rodar_b2(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    dataset_id: int,
    config: ExperimentConfig,
    barras: Sequence[BarraCarregada] | None = None,
) -> dict:
    """Compra na primeira barra decidivel, vende na ultima. Uma ida e volta.

    Conferivel a mao (criterio 3): e exatamente comprar no primeiro preco da
    janela e vender no ultimo, menos um unico par de custos de execucao.
    """
    barras = list(barras) if barras is not None else executor.carregar_janela(conn, dataset_id)
    ultima_decidivel = len(barras) - 1 - config.latency_bars
    if ultima_decidivel < 1:
        raise ValueError("janela curta demais para buy and hold")

    rule_id = registro.registrar_baseline(
        conn, "buy_and_hold", {"descricao": "compra no inicio e mantem"},
        condicoes(config),
    )

    entrada = simulador.comprar(
        conn, run_id=run_id, dataset_id=dataset_id,
        decision_bar_ms=barras[0].open_time_ms, config=config,
        fracao_do_caixa=Decimal("1.0"), rule_id=rule_id,
    )
    saida = simulador.vender(
        conn, run_id=run_id, dataset_id=dataset_id,
        decision_bar_ms=barras[ultima_decidivel].open_time_ms,
        config=config, rule_id=rule_id,
    )
    return {
        "rule_id": rule_id,
        "entrada": entrada.como_dict(),
        "saida": saida.como_dict(),
        "operacoes": 1,
        "digest": executor.digest_do_run(conn, run_id),
    }


# ---------------------------------------------------------------------------
# B1 - aleatorio, como DISTRIBUICAO
# ---------------------------------------------------------------------------


def percentil(valores: Sequence[int], p: int) -> int:
    """Percentil pelo metodo do vizinho mais proximo, sobre lista ordenada.

    Sem interpolacao: interpolar inventa um valor que nenhuma repeticao
    produziu, e o p95 e usado como limiar de comparacao contra o agente.
    """
    if not valores:
        raise ValueError("sem valores")
    ordenados = sorted(valores)
    indice = min(len(ordenados) - 1, max(0, round(p / 100 * len(ordenados)) - 1))
    return ordenados[indice]


def rodar_b1(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    dataset_id: int,
    config: ExperimentConfig,
    operacoes_alvo: int,
    semente: int,
    repeticoes: int | None = None,
    barras: Sequence[BarraCarregada] | None = None,
    persistir: bool = True,
) -> dict:
    """Distribuicao de B1. Um numero unico nao satisfaz a secao 14.3."""
    repeticoes = repeticoes if repeticoes is not None else max(
        MIN_REPETICOES, config.b1_repetitions
    )
    if repeticoes < MIN_REPETICOES:
        raise ValueError(
            f"a secao 14.3 exige no minimo {MIN_REPETICOES} repeticoes"
        )
    if operacoes_alvo <= 0:
        raise ValueError("B1 precisa casar com um numero positivo de operacoes")

    barras = list(barras) if barras is not None else executor.carregar_janela(conn, dataset_id)
    ultima_decidivel = len(barras) - 1 - config.latency_bars

    rule_id = registro.registrar_baseline(
        conn,
        "aleatorio",
        {
            "operacoes_alvo": operacoes_alvo,
            "repeticoes": repeticoes,
            "semente_base": semente,
            "casamento": "numero de operacoes do B3 (D19)",
        },
        condicoes(config),
    )

    equities: list[int] = []
    linhas = []
    for i in range(repeticoes):
        semente_i = derivar_semente(semente, i)
        pares = sortear_pares(semente_i, ultima_decidivel, 0, operacoes_alvo)
        r = simular_pares(
            barras, pares,
            caixa_inicial_cents=config.seed_capital_usd_cents,
            config=config,
            fracao_bps=10_000,
        )
        equities.append(r.equity_final_cents)
        linhas.append(
            (run_id, "B1", i, semente_i, r.operacoes, r.equity_final_cents,
             r.fee, r.spread, r.slippage, r.penalty, rule_id,
             config.fidelity_level,
             datetime.now(timezone.utc).isoformat(timespec="seconds"))
        )

    if persistir:
        conn.executemany(
            "INSERT INTO baseline_result (run_id, baseline, repeticao, seed,"
            " operacoes, equity_final_cents, fee_cents, spread_cents,"
            " slippage_cents, penalty_cents, rule_id, fidelity_level,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            linhas,
        )

    resumo = {
        "rule_id": rule_id,
        "repeticoes": repeticoes,
        "operacoes_alvo": operacoes_alvo,
        "semente_base": semente,
        "capital_semente_cents": config.seed_capital_usd_cents,
        # Distribuicao, nunca um numero so (criterio 4).
        "p5": percentil(equities, 5),
        "p50": percentil(equities, 50),
        "p95": percentil(equities, 95),
        "minimo": min(equities),
        "maximo": max(equities),
        "fidelity_level": config.fidelity_level,
        "condicoes_validade": simulador.condicoes_de_validade(config),
    }
    log.info("baseline.b1", extra={k: v for k, v in resumo.items()
                                  if k != "condicoes_validade"})
    return resumo


def rodar_b1_representativa(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    dataset_id: int,
    config: ExperimentConfig,
    pares: Sequence[tuple[int, int]],
    rule_id: int,
    barras: Sequence[BarraCarregada],
) -> dict:
    """Uma repeticao de B1 pelo caminho PERSISTIDO, no ledger.

    Existe por dois motivos que se somam:

    1. **Prova a paridade.** Ha teste exigindo que esta repeticao e a mesma
       repeticao rodada em memoria cheguem ao mesmo centavo. Sem isso, "os
       baselines usam o mesmo simulador" seria afirmacao, nao fato.

    2. **Torna o digest sensivel a semente** (criterio 2). B3 e determinista e
       seu digest nao depende de semente nenhuma - o que esta certo. E aqui
       que a semente vira lancamento, e portanto digest.
    """
    for entrada, saida in pares:
        try:
            simulador.comprar(
                conn, run_id=run_id, dataset_id=dataset_id,
                decision_bar_ms=barras[entrada].open_time_ms, config=config,
                fracao_do_caixa=Decimal("1.0"), rule_id=rule_id,
            )
        except simulador.CaixaInsuficiente:
            continue
        simulador.vender(
            conn, run_id=run_id, dataset_id=dataset_id,
            decision_bar_ms=barras[saida].open_time_ms, config=config,
            rule_id=rule_id,
        )
    return {
        "operacoes": len(pares),
        "digest": executor.digest_do_run(conn, run_id),
        "equity_final_cents": simulador.caixa_cents(conn, run_id),
    }


def rodar_comparacao(
    conn: sqlite3.Connection,
    *,
    dataset_id: int,
    config: ExperimentConfig,
    config_version_id: int,
    semente: int,
    repeticoes: int | None = None,
) -> dict:
    """B2, B3 e B1 sobre a mesma janela, mesmo simulador, mesmo custo.

    **Cada baseline no seu proprio run.** Rodar os tres na mesma carteira
    faria o segundo comecar com o que o primeiro deixou, e a comparacao
    perderia o sentido. Sao historias economicas independentes - e e
    exatamente para isso que o saldo por run existe.

    Nenhum LLM e envolvido em ponto algum disto. Se o encanamento nao fecha
    sem o modelo, o problema nao e o modelo.
    """
    from ..ledger.livro import abrir_run, encerrar_run

    barras = executor.carregar_janela(conn, dataset_id)
    semente_cents = config.seed_capital_usd_cents
    resultado: dict = {
        "semente": semente,
        "dataset_id": dataset_id,
        "config_version_id": config_version_id,
        "barras": len(barras),
        "capital_semente_cents": semente_cents,
    }

    # ---- B2 -----------------------------------------------------------
    run_b2, _ = abrir_run(
        conn, config_version_id=config_version_id,
        seed_capital_usd_cents=semente_cents, agent_id="baseline-B2",
    )
    b2 = rodar_b2(conn, run_id=run_b2, dataset_id=dataset_id, config=config,
                  barras=barras)
    b2["run_id"] = run_b2
    b2["equity_final_cents"] = simulador.caixa_cents(conn, run_b2)
    encerrar_run(conn, run_b2, "concluido")
    resultado["B2"] = b2

    # ---- B3 -----------------------------------------------------------
    run_b3, _ = abrir_run(
        conn, config_version_id=config_version_id,
        seed_capital_usd_cents=semente_cents, agent_id="baseline-B3",
    )
    b3 = rodar_b3(conn, run_id=run_b3, dataset_id=dataset_id, config=config,
                  barras=barras)
    encerrar_run(conn, run_b3, "concluido")
    resultado["B3"] = {
        **b3.como_dict(),
        "equity_final_cents": simulador.caixa_cents(conn, run_b3),
    }

    # ---- B1: casa o giro com o do B3 (D19) ----------------------------
    operacoes_alvo = max(1, b3.operacoes)
    run_b1, _ = abrir_run(
        conn, config_version_id=config_version_id,
        seed_capital_usd_cents=semente_cents, agent_id="baseline-B1-rep",
    )
    distribuicao = rodar_b1(
        conn, run_id=run_b1, dataset_id=dataset_id, config=config,
        operacoes_alvo=operacoes_alvo, semente=semente,
        repeticoes=repeticoes, barras=barras,
    )
    representativa = rodar_b1_representativa(
        conn, run_id=run_b1, dataset_id=dataset_id, config=config,
        pares=sortear_pares(
            derivar_semente(semente, 0),
            len(barras) - 1 - config.latency_bars, 0, operacoes_alvo,
        ),
        rule_id=distribuicao["rule_id"], barras=barras,
    )
    encerrar_run(conn, run_b1, "concluido")
    resultado["B1"] = {
        **distribuicao,
        "run_id": run_b1,
        "representativa": representativa,
    }

    log.info(
        "baselines.comparacao",
        extra={
            "semente": semente,
            "b2_equity": b2["equity_final_cents"],
            "b3_equity": resultado["B3"]["equity_final_cents"],
            "b3_operacoes": b3.operacoes,
            "b1_p50": distribuicao["p50"],
        },
    )
    return resultado


def resumo_comparacao(conn: sqlite3.Connection) -> dict:
    """Le de volta a ultima comparacao. Nada e guardado em duplicata.

    B2 e B3 sao reconstruidos dos proprios runs e B1 das linhas de
    `baseline_result` - guardar um resumo a parte criaria uma segunda fonte
    de verdade sobre resultado, que diverge da primeira no dia em que alguem
    esquecer de atualiza-la (regra 16).
    """
    saida: dict = {"existe": False}
    # ORDER BY id crescente com dict: o ultimo de cada marcador vence, que e
    # o que se quer - reexecutar a comparacao mostra a nova, nao a antiga.
    runs = {
        l["agent_id"]: dict(l)
        for l in conn.execute(
            "SELECT id, agent_id, config_version_id, state FROM run"
            " WHERE agent_id LIKE 'baseline-%' ORDER BY id"
        )
    }
    if not runs:
        return saida

    saida["existe"] = True
    for marcador, chave in (
        ("baseline-B2", "B2"),
        ("baseline-B3", "B3"),
        ("baseline-B1-rep", "B1_representativa"),
    ):
        r = runs.get(marcador)
        if not r:
            continue
        saida[chave] = {
            "run_id": r["id"],
            # Sob qual configuracao este numero foi produzido. Sem isto o
            # painel mostra um resultado sem dizer de que experimento ele e -
            # e um numero que descreve outra config e um numero que parou de
            # descrever o que diz, que e como este projeto ja se enganou
            # cinco vezes.
            "config_version_id": r["config_version_id"],
            "equity_final_cents": simulador.caixa_cents(conn, r["id"]),
            "execucoes": conn.execute(
                "SELECT COUNT(*) AS n FROM execution WHERE run_id = ?", (r["id"],)
            ).fetchone()["n"],
            "digest": executor.digest_do_run(conn, r["id"]),
        }

    # SO a ultima comparacao. Agregar todas as linhas de B1 do banco misturaria
    # comparacoes distintas, e o p50 passaria a descrever a soma de duas
    # distribuicoes - um numero que nao corresponde a experimento nenhum.
    ultimo_b1 = conn.execute(
        "SELECT MAX(run_id) AS run_id FROM baseline_result WHERE baseline = 'B1'"
    ).fetchone()
    linha = conn.execute(
        "SELECT COUNT(*) AS n, MIN(equity_final_cents) AS minimo,"
        " MAX(equity_final_cents) AS maximo, MAX(operacoes) AS operacoes"
        " FROM baseline_result WHERE baseline = 'B1' AND run_id = ?",
        (ultimo_b1["run_id"],),
    ).fetchone()
    if linha and linha["n"]:
        equities = [
            int(l["equity_final_cents"])
            for l in conn.execute(
                "SELECT equity_final_cents FROM baseline_result"
                " WHERE baseline = 'B1' AND run_id = ?",
                (ultimo_b1["run_id"],),
            )
        ]
        saida["B1"] = {
            "repeticoes": int(linha["n"]),
            "operacoes_alvo": int(linha["operacoes"] or 0),
            "p5": percentil(equities, 5),
            "p50": percentil(equities, 50),
            "p95": percentil(equities, 95),
            "minimo": int(linha["minimo"]),
            "maximo": int(linha["maximo"]),
        }

    # Do run do B3, e nao da config vigente: o resumo descreve o que FOI
    # rodado. Se a config mudar depois, o resultado antigo continua contando
    # as condicoes dele.
    if "B3" in saida:
        saida["condicoes_validade"] = simulador.condicoes_do_run(
            conn, saida["B3"]["run_id"]
        )
    # A comparacao ainda descreve a configuracao vigente?
    #
    # `config_version` muda por alteracao MATERIAL, e alteracao material
    # invalida comparacao que a atravesse (secao 10.2.3). Um painel que mostra
    # numeros de uma versao antiga ao lado da config atual mostra um resultado
    # que parou de descrever o experimento - exatamente o padrao que este
    # projeto ja registrou cinco vezes. Derivado, e nao guardado: nao ha campo
    # para ficar desatualizado.
    vigente = conn.execute(
        "SELECT MAX(id) AS id FROM config_version"
    ).fetchone()["id"]
    versoes = {
        saida[c]["config_version_id"]
        for c in ("B2", "B3", "B1_representativa")
        if c in saida
    }
    saida["config_version_vigente"] = vigente
    saida["config_versions_da_comparacao"] = sorted(versoes)
    saida["sob_a_config_vigente"] = versoes == {vigente} if versoes else None

    saida["aviso"] = (
        "Comparacao produzida SEM nenhum LLM envolvido. Fase 0A: nenhuma "
        "conclusao estatistica, nenhum conhecimento promovido."
    )
    return saida
