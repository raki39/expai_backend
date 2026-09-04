"""Walk-forward: a regra candidata testada fora da amostra (§14.4, B5).

> "O resultado se mantém em teste **walk-forward** fora da amostra, em pelo
> menos 3 janelas independentes." — §14.4, critério B5

## Quem executa isto é o VALIDADOR, e a diferença é estrutural

O agente nunca alcança o walk-forward: `loader.carregar` tem `acesso =
'agente'` como literal no SQL, e `executor.carregar_janela` só lê `in_sample`.
As barras daqui vêm de `selado.walk_forward`, que é o caminho do validador — e
é por isso que este módulo mora em `app/validador` e não em `app/maos_rapidas`.

O que ele **não** faz é decidir a regra. A regra é a que a hipótese registrou,
lida de `hypothesis.rule_id`: o walk-forward testa a candidata, e escolher
qualquer coisa aqui seria ajustar a hipótese ao período de teste, que é o
sobreajuste que a separação existe para impedir.

## O baseline de cada janela é da JANELA

"Supera o B3" numa janela de teste significa superar o B3 **daquela janela**.
Usar o B3 da comparação — que rodou sobre o in-sample — compararia o desempenho
de um período contra o de outro, e a diferença entre os dois períodos entraria
no número como se fosse mérito da regra.

Por isso cada janela roda os baselines de que a métrica declarada precisa, e
**só esses**. Se a hipótese declarou `excesso_sobre_b3_cents`, roda B3; se
declarou uma métrica sobre B1, roda o B1 casado com o giro daquela janela. O
pré-registro decide o que precisa ser medido — não nós, e não depois.

## "Se mantém" é medido com a régua da própria hipótese

§14.4 diz "o resultado se mantém" e não define o que isso é. A definição que
não inventa régua nova: **a métrica primária declarada alcança o
`efeito_minimo` declarado**, em cada janela. É o mesmo par que o veredito
in-sample usa, e ele é imutável desde o pré-registro (§8.2).

Qualquer outra definição — "positivo", "bate o B2", "mesmo sinal" — seria uma
régua escolhida por nós depois de a hipótese existir.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from ..config.schema import ExperimentConfig
from ..dataset import janelas as janelas_mod
from ..dataset import selado
from ..hipotese import registro as hipotese_registro
from ..hipotese import veredito as veredito_mod
from ..ledger import livro
from ..maos_rapidas import baselines, executor
from ..regra import registro as registro_de_regra
from ..regra.schema import Regra, condicoes_da_config
from ..simulador import execucao as simulador

log = logging.getLogger(__name__)

#: O dono dos runs de walk-forward. Distinto de tudo o mais para que o
#: resultado do agente nunca inclua uma execução fora da amostra por engano —
#: e o filtro que protege isso é positivo (`ciclo.ultimo_run_do_agente` casa
#: `agent_id`), não a exclusão deste nome.
AGENT_ID = "walk-forward"

#: Prefixo dos baselines de janela. `baseline-B3-wf1`, e não `baseline-B3`:
#: `veredito.observar` procura o segundo pelo nome exato, e um baseline de
#: janela que atendesse por aquele nome seria encontrado pela busca global e
#: usado como se fosse o da comparação.
def _agente_baseline(nome: str, ordem: int) -> str:
    return f"baseline-{nome}-wf{ordem}"


class SemJanelas(Exception):
    """O dataset não tem as janelas fixadas (§14.4 exige ao menos 3)."""


class SemRegra(Exception):
    """A hipótese não aponta para regra; não há candidata a testar."""


@dataclass(frozen=True)
class ResultadoDaJanela:
    ordem: int
    run_id: int
    teste_de_ms: int
    teste_ate_ms: int
    barras: int
    idas_e_voltas: int
    ordens_executadas: int
    patrimonio_final_cents: int
    baselines: dict[str, int]
    metrica_primaria: str
    observado: int | None
    por_que_sem_observado: str | None
    efeito_minimo: int
    #: A régua da própria hipótese, aplicada a esta janela. `None` quando a
    #: métrica não pôde ser observada — e `None` não é `False`.
    manteve: bool | None
    digest: str

    def como_dict(self) -> dict:
        return {
            "ordem": self.ordem,
            "run_id": self.run_id,
            "teste_de_ms": self.teste_de_ms,
            "teste_ate_ms": self.teste_ate_ms,
            "barras": self.barras,
            "idas_e_voltas": self.idas_e_voltas,
            "ordens_executadas": self.ordens_executadas,
            "patrimonio_final_cents": self.patrimonio_final_cents,
            "baselines": self.baselines,
            "metrica_primaria": self.metrica_primaria,
            "observado": self.observado,
            "por_que_sem_observado": self.por_que_sem_observado,
            "efeito_minimo": self.efeito_minimo,
            "manteve": self.manteve,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class ResultadoForward:
    hypothesis_id: int
    rule_id: int
    janelas: list[ResultadoDaJanela]

    @property
    def mantidas(self) -> int:
        return sum(1 for j in self.janelas if j.manteve is True)

    @property
    def nao_observadas(self) -> int:
        return sum(1 for j in self.janelas if j.manteve is None)

    def como_dict(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "rule_id": self.rule_id,
            "janelas": [j.como_dict() for j in self.janelas],
            "quantas": len(self.janelas),
            "mantidas": self.mantidas,
            "nao_observadas": self.nao_observadas,
            # O criterio B5 pede "ao menos 3 janelas independentes", e a
            # contagem que vale e a das MANTIDAS - uma janela que nao pode ser
            # observada nao conta a favor nem contra, e por isso aparece
            # separada em vez de somar como falha.
            "minimo_de_janelas": janelas_mod.JANELAS_MINIMAS,
            "nota": (
                "'se mantem' e medido com a regua da propria hipotese: a"
                " metrica primaria declarada alcanca o efeito minimo"
                " declarado. Qualquer outra definicao seria regua escolhida"
                " por nos depois de a hipotese existir"
            ),
        }


def _regra_da_hipotese(
    conn: sqlite3.Connection, hip: dict, config: ExperimentConfig
) -> tuple[int, Regra]:
    """A regra que a hipótese registrou. Lida, nunca escolhida aqui."""
    if not hip.get("rule_id"):
        raise SemRegra(
            f"a hipotese {hip['id']} nao aponta para regra; nao ha candidata"
            " a testar fora da amostra"
        )
    # Pelo modulo DONO da tabela: `params_json` funde `position_fraction_bps`
    # e `stop_loss_bps` dentro de si, e reconstruir a regra aqui exigiria
    # conhecer essa fusao - conhece-la errado da uma regra com dimensionamento
    # default e um resultado plausivel.
    #
    # As condicoes de validade sao as da config VIGENTE, e nao as gravadas: a
    # janela de walk-forward e outro periodo, e carregar as antigas faria o
    # resultado declarar validade sobre um intervalo que ele nao cobre.
    regra = registro_de_regra.reconstruir(
        conn, int(hip["rule_id"]), condicoes_da_config(config)
    )
    return int(hip["rule_id"]), regra


def _baselines_da_janela(
    conn: sqlite3.Connection,
    *,
    metrica: str,
    dataset_id: int,
    config: ExperimentConfig,
    config_version_id: int,
    ordem: int,
    barras,
) -> dict[str, int]:
    """Só os baselines de que a métrica declarada precisa.

    Rodar os três em toda janela custaria três vezes mais e mediria o que
    ninguém declarou. O pré-registro é imutável desde §8.2 — então ele é quem
    diz contra o que a hipótese pediu para ser medida.
    """
    saida: dict[str, int] = {}
    if metrica == "excesso_sobre_b2_cents":
        run_id, _ = livro.abrir_run(
            conn, config_version_id=config_version_id,
            seed_capital_usd_cents=config.seed_capital_usd_cents,
            agent_id=_agente_baseline("B2", ordem),
        )
        baselines.rodar_b2(
            conn, run_id=run_id, dataset_id=dataset_id, config=config,
            barras=barras,
        )
        livro.encerrar_run(conn, run_id, "concluido")
        saida["B2"] = run_id
    elif metrica == "excesso_sobre_b3_cents":
        run_id, _ = livro.abrir_run(
            conn, config_version_id=config_version_id,
            seed_capital_usd_cents=config.seed_capital_usd_cents,
            agent_id=_agente_baseline("B3", ordem),
        )
        # `rodar_b3` deriva a regra da config e a CONGELA — o mesmo caminho da
        # comparação, e não uma regra montada aqui. Uma segunda construção do
        # B3 poderia divergir da congelada, e aí o controle da janela não seria
        # o mesmo controle.
        baselines.rodar_b3(
            conn, run_id=run_id, dataset_id=dataset_id, config=config,
            barras=barras,
        )
        livro.encerrar_run(conn, run_id, "concluido")
        saida["B3"] = run_id
    return saida


def rodar(
    conn: sqlite3.Connection,
    *,
    hypothesis_id: int,
    dataset_id: int,
    config: ExperimentConfig,
    config_version_id: int,
) -> ResultadoForward:
    """Executa a regra da hipótese nas janelas de teste do walk-forward.

    Um run por janela, como cada baseline desde o incremento 3: são histórias
    econômicas independentes, e somá-las num run só faria o resultado da
    terceira janela partir do caixa que a segunda deixou.
    """
    hip = hipotese_registro.por_id(conn, hypothesis_id)
    if hip is None:
        raise SemRegra(f"hipotese {hypothesis_id} nao existe")
    rule_id, regra = _regra_da_hipotese(conn, hip, config)

    lista = janelas_mod.ler(conn, dataset_id)
    if len(lista) < janelas_mod.JANELAS_MINIMAS:
        raise SemJanelas(
            f"o dataset {dataset_id} tem {len(lista)} janela(s) de"
            f" walk-forward, e §14.4 (B5) exige ao menos"
            f" {janelas_mod.JANELAS_MINIMAS}"
        )

    metrica = hip["metrica_primaria"]
    efeito_minimo = int(hip["efeito_minimo"])
    saida: list[ResultadoDaJanela] = []

    for janela in lista:
        # As barras vem do caminho do VALIDADOR. O do agente nao alcanca isto,
        # e a diferenca e do SQL, nao da disciplina de quem chama.
        barras = selado.walk_forward(
            conn, dataset_id,
            de_ms=janela.teste_de_ms, ate_ms=janela.teste_ate_ms,
        )
        if len(barras) <= regra.janela_minima + config.latency_bars:
            saida.append(
                ResultadoDaJanela(
                    ordem=janela.ordem, run_id=0,
                    teste_de_ms=janela.teste_de_ms,
                    teste_ate_ms=janela.teste_ate_ms,
                    barras=len(barras), idas_e_voltas=0, ordens_executadas=0,
                    patrimonio_final_cents=0, baselines={},
                    metrica_primaria=metrica, observado=None,
                    por_que_sem_observado=(
                        f"a janela tem {len(barras)} barras e a regra precisa"
                        f" de {regra.janela_minima} mais"
                        f" {config.latency_bars} de latencia"
                    ),
                    efeito_minimo=efeito_minimo, manteve=None, digest="",
                )
            )
            continue

        # Os baselines PRIMEIRO: se a janela nao comporta o baseline, ela nao
        # comporta a candidata, e descobrir isso depois de executar deixaria um
        # run sem contra o que ser medido.
        dos_baselines = _baselines_da_janela(
            conn, metrica=metrica, dataset_id=dataset_id, config=config,
            config_version_id=config_version_id, ordem=janela.ordem,
            barras=barras,
        )

        run_id, _ = livro.abrir_run(
            conn, config_version_id=config_version_id,
            seed_capital_usd_cents=config.seed_capital_usd_cents,
            agent_id=AGENT_ID,
        )
        rule_da_janela = registro_de_regra.registrar(conn, regra)
        resultado = executor.rodar(
            conn, run_id=run_id, dataset_id=dataset_id, regra=regra,
            rule_id=rule_da_janela, config=config, barras=barras,
        )
        livro.encerrar_run(conn, run_id, "concluido")

        patrimonio = simulador.caixa_cents(conn, run_id)
        realizado = veredito_mod.observar(
            conn,
            run_id=run_id,
            patrimonio_cents=patrimonio,
            idas_e_voltas=resultado.operacoes,
            b1_casado=None,
            baselines_do_recorte=dos_baselines,
        )
        observado = realizado.de(metrica)
        saida.append(
            ResultadoDaJanela(
                ordem=janela.ordem,
                run_id=run_id,
                teste_de_ms=janela.teste_de_ms,
                teste_ate_ms=janela.teste_ate_ms,
                barras=len(barras),
                idas_e_voltas=resultado.operacoes,
                ordens_executadas=resultado.execucoes,
                patrimonio_final_cents=patrimonio,
                baselines=dos_baselines,
                metrica_primaria=metrica,
                observado=observado,
                por_que_sem_observado=(
                    None if observado is not None
                    else realizado.por_que_falta(metrica)
                ),
                efeito_minimo=efeito_minimo,
                manteve=(
                    None if observado is None else observado >= efeito_minimo
                ),
                digest=resultado.digest,
            )
        )

    log.info(
        "forward.rodado",
        extra={
            "hypothesis_id": hypothesis_id,
            "janelas": len(saida),
            "mantidas": sum(1 for j in saida if j.manteve is True),
        },
    )
    return ResultadoForward(
        hypothesis_id=hypothesis_id, rule_id=rule_id, janelas=saida
    )


def ler(conn: sqlite3.Connection, hypothesis_id: int) -> dict | None:
    """O que já foi executado fora da amostra para esta hipótese.

    Derivado dos runs marcados, e não guardado: os runs são append-only e o
    resumo é uma consulta sobre eles. Um campo gravado aqui seria a segunda
    fonte de verdade sobre quantas janelas se mantiveram.
    """
    runs = [
        int(l["id"])
        for l in conn.execute(
            "SELECT id FROM run WHERE agent_id = ? ORDER BY id", (AGENT_ID,)
        )
    ]
    if not runs:
        return None
    return {
        "runs": runs,
        "quantos": len(runs),
        "nota": (
            "os runs de walk-forward existem; o resultado por janela sai de"
            " `forward.rodar`, que os produz e os julga na mesma passagem"
        ),
    }
