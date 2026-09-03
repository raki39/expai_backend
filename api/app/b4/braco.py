"""Roda o braco B4: 16 hipoteses pelo MESMO caminho do agente.

O criterio 1 do incremento 12 e o que da sentido ao resto: se B4 tivesse um
caminho de validacao proprio, a comparacao entre os bracos mediria a diferenca
entre duas implementacoes, e nao entre busca de parametro e reflexao.

Entao aqui nao ha nada que decida sobre hipotese. Este modulo **orquestra**:
abre run, grava evento, registra pre-registro, executa, pede parecer. Cada uma
dessas coisas e feita pela mesma funcao que o ciclo do agente chama.

## O que NAO tem, e e o ponto

Nenhum import de `app/cerebro`. Nenhuma chave de provedor, nenhum tier,
nenhuma reflexao. B4 nao consome tokens - so CPU (§14.3) -, e a maneira de
provar isso e a suite rodar o braco inteiro sem cliente de modelo em lugar
nenhum, que e o criterio 3.

`app.cerebro.paradas` fica de fora tambem: B4 nao para por falha de provedor
porque nao ha provedor a falhar.

## O evento nao cognitivo

`hypothesis.agent_event_id` e `NOT NULL`: toda hipotese aponta para o evento
que a produziu. B4 tambem produz evento - ele DECIDE parametros, so nao
reflete -, e o evento sai com `provider`, `model` e `tier` nulos e custo zero.

Isso nao e contorno de restricao: e o que faz `reflexoes` (que conta
`provider IS NOT NULL`) dar **zero** para todo run de B4, e o que faz o
criterio 3 ser conferivel no ledger em vez de na palavra de quem escreveu.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

from .. import creditos as creditos_mod
from ..config.schema import ExperimentConfig
from ..dataset import loader
from ..hipotese import registro as hipotese_registro
from ..ledger import livro
from ..maos_rapidas import executor
from ..regra import registro as registro_de_regra
from ..settings import Settings
from ..simulador import execucao as simulador
from ..validador import estados as validador_estados
from ..validador import promocao as validador_promocao
from . import busca

log = logging.getLogger(__name__)

#: O braco, para o orcamento de creditos. §8.6.1 exige orcamento **da
#: especialidade**, e a D30 fixou 60 identicos nos dois - identicos de
#: proposito: um controle com mais tentativas nao e controle.
BRACO = "b4"

#: O `agent_id` do run. Distinto dos baselines e do agente para que
#: `/api/agente` (que le `MAX(run_id) FROM agent_event`) nao passe a mostrar um
#: run de B4 como se fosse o do agente - o que seria a atribuicao errada que a
#: D35 acabou de consertar, por outra porta.
AGENT_ID = "b4-0001"


@dataclass(frozen=True)
class ResultadoDeUma:
    """Uma hipotese de B4, do pre-registro ao parecer."""

    indice: int
    tecnica: str
    run_id: int
    hypothesis_id: int
    rule_id: int
    testavel: bool
    idas_e_voltas: int
    ordens_executadas: int
    patrimonio_final_cents: int
    digest_do_run: str
    creditos_cobrados: int | None
    veredito: str | None
    motivo: str | None

    def como_dict(self) -> dict:
        return {
            "indice": self.indice,
            "tecnica": self.tecnica,
            "run_id": self.run_id,
            "hypothesis_id": self.hypothesis_id,
            "rule_id": self.rule_id,
            "testavel": self.testavel,
            "idas_e_voltas": self.idas_e_voltas,
            "ordens_executadas": self.ordens_executadas,
            "patrimonio_final_cents": self.patrimonio_final_cents,
            "digest_do_run": self.digest_do_run,
            "creditos_cobrados": self.creditos_cobrados,
            "veredito": self.veredito,
            "motivo": self.motivo,
        }


@dataclass(frozen=True)
class ResultadoDoBraco:
    config_version_id: int
    dataset_id: int
    semente: int
    digest_das_hipoteses: str
    hipoteses: list[ResultadoDeUma]
    cpu_micros: int

    def como_dict(self) -> dict:
        creditos = sum(h.creditos_cobrados or 0 for h in self.hipoteses)
        sustentadas = [h for h in self.hipoteses if h.veredito == "sustentada"]
        return {
            "braco": BRACO,
            # A LINHA que o painel mostra na caixa de resultado.
            #
            # Ela existe porque o corpo inteiro tem ~8.000 caracteres e o
            # painel o passa pela URL do redirect, cortado em 4.000: o JSON
            # chega truncado e nao parseavel, e a caixa mostraria um blob
            # numa linha so. Quem resume e a API, e nao a tela - escolher
            # quais campos importam e decidir sobre o experimento, e isso nao
            # acontece no painel (regra 19).
            #
            # O corpo completo continua em `GET /api/b4` e no export.
            "mensagem": (
                f"{len(self.hipoteses)} hipoteses de B4,"
                f" {sum(h.creditos_cobrados or 0 for h in self.hipoteses)}"
                f" creditos, zero reflexoes."
                f" {sum(1 for h in self.hipoteses if h.veredito == 'sustentada')}"
                f" sustentada(s)."
                f" Busca: {self.digest_das_hipoteses[:12]}"
            ),
            "config_version_id": self.config_version_id,
            "dataset_id": self.dataset_id,
            "semente": self.semente,
            # R12 na 0B: mesma semente e mesma config produzem o mesmo
            # CONJUNTO de hipoteses. Cobre a busca, e nao o resultado - o
            # resultado tem `digest_do_run`, que sai dos lancamentos.
            "digest_das_hipoteses": self.digest_das_hipoteses,
            "quantas": len(self.hipoteses),
            "creditos_consumidos": creditos,
            "sustentadas": len(sustentadas),
            # A COMPARACAO da fase e por credito gasto, e nao por hipotese
            # (§14.3, R44). Em ppm porque a divisao inteira daria zero em todo
            # caso interessante, e ponto flutuante nao entra em numero que
            # alimenta decisao.
            "sustentadas_por_credito_ppm": (
                len(sustentadas) * 1_000_000 // creditos if creditos else None
            ),
            "por_que_sem_taxa": (
                None if creditos else
                "nenhum credito consumido: sem denominador nao ha taxa, e"
                " devolver zero afirmaria que a taxa foi medida"
            ),
            "cpu_micros": self.cpu_micros,
            # Zero, e derivado: §14.3 diz que B4 nao consome tokens. O numero
            # sai do ledger, e nao de uma promessa neste docstring.
            "reflexoes": 0,
            "hipoteses": [h.como_dict() for h in self.hipoteses],
        }


class BaselineAusente(Exception):
    """O B3 nao rodou sob esta `config_version`.

    A metrica primaria de B4 e `excesso_sobre_b3_cents` (ver `busca.METRICA`),
    e `veredito.observar` so aceita B2 e B3 produzidos sob a MESMA config -
    comparar atravessando mudanca material e o que §10.2.3 invalida.

    Sem B3, as 16 hipoteses saem com veredito `None`: nao "refutada", nao
    "inconclusiva" - **nada**, porque a metrica que elas declaram nao tem
    contra o que ser medida. Isso e pior que uma recusa: gastaria 16 creditos
    para produzir dezesseis linhas que nao afirmam coisa nenhuma.

    Medido antes de escrever esta excecao: com os baselines ausentes, os
    dezesseis vereditos vieram `None` e catorze creditos foram cobrados.
    """


class SeparacaoAusente(Exception):
    """O dataset nao foi dividido por finalidade (§8.5.1).

    O mesmo motivo do ciclo do agente: sem os quatro conjuntos, B4 executaria
    sobre a janela inteira e as duas medicoes deixariam de ser comparaveis -
    alem de a hipotese olhar dado que deveria estar selado.
    """


def _evento_nao_cognitivo(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    config: ExperimentConfig,
    indice: int,
    tecnica: str,
) -> int:
    """O evento que produziu a hipotese. Custo zero, provedor nulo.

    Pela MESMA funcao que grava o evento do agente. Uma insercao propria em
    `agent_event` aqui criaria um segundo caminho de escrita para a tabela que
    a regra 16 chama de autoridade sobre decisao e caminho.
    """
    event_id, _ = livro.registrar_custo_reflexao(
        conn,
        run_id=run_id,
        node=f"b4_{tecnica}",
        kind="proposta_nao_cognitiva",
        custo_usd_minor=0,
        custo_usd_micro=0,
        fx_rate_micro=livro.fx_micro(config.fx_brl_per_usd),
        fx_rate_date=config.fx_rate_date,
        # `tier`, `provider` e `model` ficam nulos: nenhum modelo falou. E
        # `provider IS NULL` e exatamente o que faz `reflexoes` dar zero.
        expectation=None,
        confidence_ppm=None,
    )
    log.info(
        "b4.proposta", extra={"run_id": run_id, "indice": indice, "tecnica": tecnica}
    )
    return event_id


def rodar(
    conn: sqlite3.Connection,
    *,
    dataset_id: int,
    config: ExperimentConfig,
    config_version_id: int,
    settings: Settings | None = None,
    semente: int | None = None,
) -> ResultadoDoBraco:
    """As 16 hipoteses de B4, cada uma no seu run.

    `settings` entra na assinatura e **nao e usado**: e o que mantem a rota
    simetrica a do agente sem que B4 ganhe acesso a credencial nenhuma. Ha
    teste conferindo que o modulo nao importa `app/cerebro`.

    Um run por hipotese, como cada baseline desde o incremento 3: sao
    historias economicas independentes, e o defeito de contas globais em vez de
    por run ja custou uma migracao.
    """
    if not loader.esta_dividido(conn, dataset_id):
        raise SeparacaoAusente(
            f"o dataset {dataset_id} nao tem a divisao por finalidade da"
            " secao 8.5.1; rode a separacao antes de B4"
        )

    # A MESMA janela que o agente executa, pela mesma funcao: `in_sample`
    # (D27). Se B4 rodasse sobre outro conjunto, a comparacao entre os bracos
    # mediria janela em vez de tecnica.
    # O B3 tem de existir ANTES, e a conferencia vem antes de qualquer
    # credito ser cobrado: recusar depois de gastar seria cobrar pelo que nao
    # se conseguiu medir.
    # Pelo `agent_id` do RUN, e nao por `baseline_result`: B2 e B3 vivem em
    # runs proprios e nao gravam linha de resultado - so as 1.000 repeticoes
    # de B1 gravam. E a mesma consulta que `veredito.observar` faz para achar
    # o baseline, o que evita este check aprovar um caso que o veredito
    # depois recusa.
    if not conn.execute(
        "SELECT 1 FROM run WHERE agent_id = 'baseline-B3'"
        " AND config_version_id = ?",
        (config_version_id,),
    ).fetchone():
        raise BaselineAusente(
            f"nenhum B3 sob a config_version {config_version_id}; a metrica"
            f" primaria de B4 e {busca.METRICA!r} e sem o baseline ela nao tem"
            " contra o que ser medida. Rode a comparacao antes de B4"
        )

    barras = executor.carregar_janela(
        conn, dataset_id, finalidade=executor.FINALIDADE_DE_EXECUCAO
    )
    if not barras:
        raise ValueError("janela de execucao vazia")

    duracao = (
        int(barras[1].open_time_ms - barras[0].open_time_ms)
        if len(barras) >= 2
        else 900_000
    )
    base = config.default_seed if semente is None else semente

    # O orcamento do braco. Idempotente por (braco, config_version): rodar B4
    # duas vezes na mesma config nao ganha creditos novos, e e o ponto - senao
    # bastaria reexecutar para comprar tentativas.
    creditos_mod.conceder(
        conn,
        braco=BRACO,
        config_version_id=config_version_id,
        creditos=config.creditos_por_braco,
    )

    candidatas = busca.gerar(
        config=config,
        duracao_barra_ms=duracao,
        horizonte_barras=len(barras),
        semente=base,
    )

    comeco = time.perf_counter_ns()
    saida: list[ResultadoDeUma] = []
    for c in candidatas:
        run_id, _ = livro.abrir_run(
            conn,
            config_version_id=config_version_id,
            seed_capital_usd_cents=config.seed_capital_usd_cents,
            agent_id=AGENT_ID,
        )
        event_id = _evento_nao_cognitivo(
            conn,
            run_id=run_id,
            config=config,
            indice=c.indice,
            tecnica=c.tecnica,
        )
        rule_id = registro_de_regra.registrar(conn, c.regra)
        hypothesis_id, testavel = hipotese_registro.registrar(
            conn,
            run_id=run_id,
            agent_event_id=event_id,
            bruto=c.pre_registro,
            condicoes_validade=c.regra.condicoes_validade.model_dump(mode="json"),
            duracao_barra_ms=duracao,
            horizonte_barras=len(barras),
            rule_id=rule_id,
            # A origem propria: e por ela que o contador separa os bracos.
            agente_origem=hipotese_registro.AGENTE_ORIGEM_B4,
        )
        # O agente SOLICITA e o validador escreve. Identico ao ciclo: a
        # insercao em `hypothesis_state` so acontece dentro de `app/validador`.
        validador_promocao.admitir(conn, hypothesis_id, run_id=run_id)

        resultado = executor.rodar(
            conn,
            run_id=run_id,
            dataset_id=dataset_id,
            regra=c.regra,
            rule_id=rule_id,
            config=config,
            barras=barras,
        )
        livro.encerrar_run(conn, run_id, "concluido")

        veredito = motivo = None
        creditos = None
        try:
            parecer = validador_promocao.avaliar_in_sample(
                conn, hypothesis_id=hypothesis_id, run_id=run_id
            )
            veredito, motivo, creditos = (
                parecer.veredito,
                parecer.motivo,
                parecer.creditos,
            )
        except validador_promocao.NaoAvaliavel as erro:
            # Hipotese arquivada como nao testavel, ou orcamento esgotado. Nao
            # e falha do braco: e resultado, e fica escrito na linha dela.
            motivo = str(erro)
            log.info(
                "b4.sem_parecer",
                extra={"hypothesis_id": hypothesis_id, "motivo": motivo},
            )

        saida.append(
            ResultadoDeUma(
                indice=c.indice,
                tecnica=c.tecnica,
                run_id=run_id,
                hypothesis_id=hypothesis_id,
                rule_id=rule_id,
                testavel=testavel,
                idas_e_voltas=resultado.operacoes,
                ordens_executadas=resultado.execucoes,
                patrimonio_final_cents=simulador.caixa_cents(conn, run_id),
                digest_do_run=resultado.digest,
                creditos_cobrados=creditos,
                veredito=veredito,
                motivo=motivo,
            )
        )

    cpu = (time.perf_counter_ns() - comeco) // 1_000
    log.info(
        "b4.braco",
        extra={
            "quantas": len(saida),
            "creditos": sum(h.creditos_cobrados or 0 for h in saida),
            "cpu_micros": cpu,
        },
    )
    return ResultadoDoBraco(
        config_version_id=config_version_id,
        dataset_id=dataset_id,
        semente=base,
        digest_das_hipoteses=busca.digest(candidatas),
        hipoteses=saida,
        cpu_micros=cpu,
    )


def resumo(conn: sqlite3.Connection, config_version_id: int) -> dict:
    """O estado do braco B4 sob esta config, derivado do banco.

    Nao guarda nada: as hipoteses de B4 sao linhas de `hypothesis` com
    `agente_origem` proprio, e o resumo e uma consulta sobre elas. Um campo
    gravado aqui seria a segunda fonte de verdade sobre quantas tentativas o
    controle fez - e o contador global do DSR le a primeira.
    """
    linhas = []
    for l in conn.execute(
        "SELECT h.id AS hypothesis_id, h.run_id AS run_id,"
        "       h.testavel AS testavel, h.content_hash AS content_hash,"
        "       h.rule_id AS rule_id, h.enunciado AS enunciado,"
        "       h.n_minimo AS n_minimo, h.efeito_minimo AS efeito_minimo,"
        "       h.metrica_primaria AS metrica_primaria"
        "  FROM hypothesis h JOIN run r ON r.id = h.run_id"
        " WHERE h.agente_origem = ? AND r.config_version_id = ?"
        " ORDER BY h.id",
        (hipotese_registro.AGENTE_ORIGEM_B4, config_version_id),
    ):
        linha = dict(l)
        # O VEREDITO e o CREDITO de cada uma. Sem eles, o resumo devolvia
        # dezesseis ids e nenhum resultado - e o export do painel mostraria
        # que B4 rodou sem dizer o que ele concluiu.
        #
        # Quarta vez que um campo existe no POST e falta no GET, depois de
        # `motivo`, `regra_veio_do_cerebro` e `parecer_do_validador`. Aqui
        # entra na primeira escrita porque a lista `CAMPOS_QUE_JA_SUMIRAM` do
        # incremento 11b existe justamente para a pergunta ser feita.
        linha["parecer"] = validador_promocao.parecer_derivado(
            conn, int(l["hypothesis_id"])
        )
        linha["testes"] = creditos_mod.testes_da_hipotese(
            conn, int(l["hypothesis_id"])
        )
        linha["estado"] = (
            e.estado
            if (e := validador_estados.atual(conn, int(l["hypothesis_id"])))
            else None
        )
        linhas.append(linha)
    saldo = creditos_mod.saldo(conn, braco=BRACO, config_version_id=config_version_id)
    sustentadas = sum(
        1 for l in linhas if (l["parecer"] or {}).get("veredito") == "sustentada"
    )
    consumido = saldo.consumido if saldo else 0
    return {
        "braco": BRACO,
        "config_version_id": config_version_id,
        "hipoteses": linhas,
        "quantas": len(linhas),
        "sustentadas": sustentadas,
        # A comparacao da fase, no GET tambem. Estava so no corpo do POST -
        # que acontece uma vez, no clique, enquanto todo o resto da vida do
        # braco e lido por aqui.
        "sustentadas_por_credito_ppm": (
            sustentadas * 1_000_000 // consumido if consumido else None
        ),
        "por_que_sem_taxa": (
            None if consumido else
            "nenhum credito consumido: sem denominador nao ha taxa, e devolver"
            " zero afirmaria que ela foi medida"
        ),
        "creditos": saldo,
        "agente_origem": hipotese_registro.AGENTE_ORIGEM_B4,
        "nota": (
            "B4 nao consome tokens (§14.3): todo evento dele tem provider nulo"
            " e custo zero, e por isso `reflexoes` de um run de B4 e zero -"
            " conferivel no ledger, e nao afirmado em prosa"
        ),
    }
