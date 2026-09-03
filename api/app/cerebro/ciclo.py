"""O ciclo da 0A, fechado de ponta a ponta.

    abre run -> cerebro lento propoe regra -> maos rapidas executam ->
    ledger registra tudo -> resultado sai com as condicoes declaradas

E o unico lugar do projeto onde o cerebro e as maos aparecem na mesma funcao,
e mesmo aqui eles nao se tocam: o grafo termina, a regra fica gravada, e so
entao o executor a le. O executor continua sem saber de onde ela veio - e por
isso que o B3, que nunca passou por modelo nenhum, roda pelo mesmo caminho.

**Se o cerebro parar** - teto atingido, resposta invalida, provedor fora do ar
- as maos rapidas rodam a regra padrao e o run termina normalmente (secao 3.6
regra 2). O resultado diz, sempre, quantas reflexoes houve e qual regra
executou: um run sem reflexao nenhuma nao esta medindo cerebro, e confundir os
dois seria atribuir ao agente o resultado do baseline.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .. import creditos as creditos_mod
from ..config.schema import ExperimentConfig
from ..dataset import loader
from ..ledger import livro
from ..maos_rapidas import baselines, executor
from ..regra import registro as registro_de_regra
from ..regra.schema import Regra
from ..settings import Settings
from ..simulador import execucao as simulador
from ..simulador.execucao import condicoes_do_run
from ..validador import promocao as validador_promocao
from . import avaliacao, grafo, paradas, propostas

log = logging.getLogger(__name__)


class SeparacaoAusente(Exception):
    """O dataset nao foi dividido por finalidade (secao 8.5.1).

    Excecao propria, e nao `ValueError`: quem chama precisa poder distinguir
    "falta preparar o dataset" de "a janela esta vazia". A primeira tem
    conserto de um comando; a segunda e defeito.
    """


def _retornos_bps(barras: list) -> list[int]:
    """Retorno de fechamento a fechamento, em bps inteiros.

    So alimenta a estimativa de autocorrelacao da secao 8.3. Nao toca dinheiro
    e nao entra em digest nenhum - por isso a divisao inteira aqui e barata, e
    nao uma violacao da regra 5 por caminho indireto.
    """
    saida: list[int] = []
    for anterior, atual in zip(barras, barras[1:]):
        if anterior.close <= 0:
            saida.append(0)
            continue
        saida.append((atual.close - anterior.close) * 10_000 // anterior.close)
    return saida


@dataclass(frozen=True)
class ResultadoDoCiclo:
    run_id: int
    reflexoes: int
    parou_em: str | None
    motivo: str | None
    regra_veio_do_cerebro: bool
    # A CATEGORIA da parada, e nao so o texto. E ela que decide se as maos
    # rapidas executaram (D35), entao precisa estar onde quem le o resultado
    # consegue ve-la.
    categoria_da_parada: str | None
    # Quem pode chamar isto de "resultado do agente", e por que. Ver
    # `paradas.atribuicao`.
    atribuicao: dict
    # `None` quando nada executou: nao ha regra a apontar, e um id de uma
    # regra que nao rodou seria pior que a ausencia dele.
    rule_id: int | None
    regra_hash: str | None
    proposal_id: int | None
    expectativa: str | None
    confianca_ppm: int | None
    execucao: dict
    # O resultado economico do run. Sem ele o relatorio do agente diz quantas
    # operacoes houve e nao diz se sobrou dinheiro - que e a unica pergunta
    # que a comparacao com os baselines responde.
    patrimonio_final_cents: int
    custos: dict
    # A distribuicao do acaso com o MESMO giro deste run. Produzida aqui, e
    # nao num passo a parte, porque um controle que pode ficar dessincronizado
    # do que ele controla nao e controle nenhum - e foi exatamente assim que
    # o primeiro run do agente saiu, ao lado de um B1 casado com o B3.
    b1_casado: dict | None
    gasto: dict
    sobreposicao: dict
    condicoes_validade: str
    # O evento filho da decisao que compara o declarado com o realizado
    # (R25.3). `None` quando o cerebro nao declarou nada - nao ha o que
    # avaliar, e pendurar a comparacao em quem nao afirmou seria invencao.
    avaliacao_event_id: int | None
    # O parecer independente do validador, e o estado da hipotese depois dele.
    # `None` quando nao houve hipotese - com o teto zerado o cerebro nao fala
    # (D23), e nao ha o que julgar.
    parecer_do_validador: dict | None
    hypothesis_id: int | None

    def como_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "reflexoes": self.reflexoes,
            "parou_em": self.parou_em,
            "motivo": self.motivo,
            "regra_veio_do_cerebro": self.regra_veio_do_cerebro,
            "categoria_da_parada": self.categoria_da_parada,
            "atribuicao": self.atribuicao,
            "rule_id": self.rule_id,
            "regra_hash": self.regra_hash,
            "proposal_id": self.proposal_id,
            "expectativa": self.expectativa,
            "confianca_ppm": self.confianca_ppm,
            "execucao": self.execucao,
            "patrimonio_final_cents": self.patrimonio_final_cents,
            "custos_cents": self.custos,
            "b1_casado_com_este_run": self.b1_casado,
            "excesso_sobre_b1_p50_cents": (
                self.patrimonio_final_cents - self.b1_casado["p50"]
                if self.b1_casado else None
            ),
            "gasto": self.gasto,
            "sobreposicao_amostral": self.sobreposicao,
            "condicoes_validade": self.condicoes_validade,
            "avaliacao_event_id": self.avaliacao_event_id,
            "hypothesis_id": self.hypothesis_id,
            "parecer_do_validador": self.parecer_do_validador,
        }


def rodar(
    conn: sqlite3.Connection,
    *,
    dataset_id: int,
    config: ExperimentConfig,
    config_version_id: int,
    settings: Settings,
    adaptador: Any | None = None,
    dormir: Any = time.sleep,
) -> ResultadoDoCiclo:
    """Abre um run proprio, roda o ciclo inteiro e o encerra.

    **Duas janelas, e nao uma** (D27, secao 8.5.1). O cerebro observa
    `exploracao` - "conhecer o mercado e formular hipoteses" - e as maos
    rapidas executam sobre `in_sample` - "desenvolver e ajustar estrategias".

    Isso REVISA a D22, que dizia "o cerebro observa a mesma janela que
    executa". Aquela decisao foi tomada na 0A porque nao havia separacao: a
    unica alternativa honesta era declarar a sobreposicao em vez de fingir que
    ela nao existia. Com os quatro conjuntos, a separacao existe, e manter a
    sobreposicao seria escolher o resultado em amostra tendo a alternativa na
    mao.

    `sobreposicao_amostral` continua sendo calculada e gravada, e agora ela
    deve dar **zero**. E numero, nao prosa - se algum dia voltar a ser maior
    que zero, alguem juntou os conjuntos de novo e o campo acusa.
    """
    # O ciclo da 0B RECUSA rodar sobre dataset nao dividido.
    #
    # O fallback de `loader.carregar` existe para que os runs da 0A continuem
    # reproduziveis (R12) - e so para isso. Deixar o CICLO cair nele produziria
    # um run que parece 0B e e 0A: cerebro e maos rapidas na mesma janela,
    # `sobreposicao_amostral` de volta aos 100%, e os quatro conjuntos da
    # secao 8.5.1 existindo no schema sem separar nada.
    #
    # Era exatamente o que aconteceria em producao: o dataset de la foi
    # ingerido no incremento 1, antes da migracao 10. Recusar alto e a unica
    # resposta honesta - um resultado 0B sem separacao seria pior que nenhum,
    # porque ninguem saberia que ele nao vale.
    if not loader.esta_dividido(conn, dataset_id):
        raise SeparacaoAusente(
            f"o dataset {dataset_id} nao tem a divisao por finalidade da"
            " secao 8.5.1. Rodar assim produziria um resultado que parece 0B"
            " e e 0A - cerebro e maos rapidas na mesma janela. Crie a divisao"
            " com `dataset.ingest.garantir_separacao` (ou POST"
            " /api/dataset/separacao) antes de rodar o ciclo"
        )

    barras_de_observacao = executor.carregar_janela(
        conn, dataset_id, finalidade="exploracao"
    )
    barras = executor.carregar_janela(
        conn, dataset_id, finalidade=executor.FINALIDADE_DE_EXECUCAO
    )
    if not barras:
        raise ValueError(
            "conjunto in_sample vazio: nao ha o que executar"
        )
    if not barras_de_observacao:
        raise ValueError(
            "conjunto de exploracao vazio: o cerebro nao tem o que observar."
            " Com a divisao presente isto e defeito da divisao, nao caso"
            " normal"
        )

    run_id, _ = livro.abrir_run(
        conn,
        config_version_id=config_version_id,
        seed_capital_usd_cents=config.seed_capital_usd_cents,
    )

    # O orcamento de creditos do braco, vindo da CONFIG versionada (D30) e
    # nao de escolha do agente. Idempotente por (braco, config_version): o
    # segundo run da mesma config nao ganha orcamento novo, e e o ponto -
    # senao bastaria reabrir run para comprar tentativas.
    creditos_mod.conceder(
        conn,
        braco="agente",
        config_version_id=config_version_id,
        creditos=config.creditos_por_braco,
    )

    dep = grafo.Dependencias(
        conn=conn, config=config, settings=settings, adaptador=adaptador,
        dormir=dormir,
    )
    estado = grafo.rodar(
        dep,
        run_id=run_id,
        barras=barras_de_observacao,
        # O horizonte da conta de poder e o da EXECUCAO, e nao o da
        # observacao: a amostra da hipotese vem de onde ela roda.
        horizonte_execucao=len(barras),
    )

    # A hipotese entra na maquina de estados (§8.1). O agente SOLICITA; quem
    # escreve a linha e o validador - a insercao em `hypothesis_state` so
    # acontece dentro de `app/validador`, e ha teste varrendo o codigo.
    hypothesis_id = estado.get("hypothesis_id")
    if hypothesis_id is not None:
        validador_promocao.admitir(conn, int(hypothesis_id), run_id=run_id)

    # ---------------------------------------------------------------- D35
    #
    # O QUE executa depende de POR QUE o cerebro parou, e antes nao dependia.
    #
    # A secao 3.6, regra 2, e sobre o TETO: "Ao atingir o teto, ele continua
    # operando com as maos rapidas, mas para de raciocinar ate o proximo
    # ciclo." Ali o agente DECIDIU nao gastar, e a regra padrao rodar e a
    # especificacao ao pe da letra.
    #
    # Ela nao diz nada sobre o cerebro tentar e falhar tecnicamente. Numa
    # falha de provedor o agente nao decidiu coisa nenhuma - e rodar a regra
    # padrao ali produz um resultado de B3 pendurado no run do agente. Foi
    # exatamente o que aconteceu no run 27: 244 idas e voltas "entre o p50 e o
    # p95" que nenhuma decisao cognitiva escolheu.
    #
    # Entao: teto executa, falha tecnica nao executa nada.
    categoria_da_parada = estado.get("categoria_da_parada")
    ativa = propostas.regra_ativa(conn, run_id)

    if ativa is not None:
        regra: Regra = estado["regra"]
        rule_id: int | None = int(ativa["rule_id"])
        veio_do_cerebro = True
        executa = True
    else:
        veio_do_cerebro = False
        executa = paradas.executa_regra_padrao(categoria_da_parada)
        regra = grafo.regra_padrao(config)
        # So registra a regra se ela for de fato executar. Registrar uma regra
        # que nunca rodou deixaria no catalogo uma linha que nao corresponde a
        # execucao nenhuma.
        rule_id = registro_de_regra.registrar(conn, regra) if executa else None

    resultado = None
    if executa:
        resultado = executor.rodar(
            conn,
            run_id=run_id,
            dataset_id=dataset_id,
            regra=regra,
            rule_id=int(rule_id),
            config=config,
            barras=barras,
        )
    operacoes = resultado.operacoes if resultado is not None else 0
    livro.encerrar_run(conn, run_id, "concluido")

    # O controle do acaso, casado com o giro DESTE run (D19, ADR 0014).
    # Cada ida e volta paga um pedagio fixo e entrada aleatoria nao tem
    # vantagem nenhuma - entao um B1 que gire mais que o agente perde por
    # atrito, e o agente pareceria bom por ter operado menos. Produzido aqui
    # para que os dois numeros nunca existam separados.
    b1_casado = None
    if operacoes > 0:
        try:
            b1_casado = baselines.b1_casado_com(
                conn,
                dataset_id=dataset_id,
                config=config,
                config_version_id=config_version_id,
                operacoes_alvo=operacoes,
                # O MESMO tamanho de posicao da regra executada (secao 14.3).
                # Casar o giro e nao casar o tamanho mede dimensionamento em
                # vez de timing - o mesmo erro da D19, um nivel abaixo.
                fracao_bps=regra.position_fraction_bps,
                semente=config.default_seed,
                barras=barras,
            )
        except ValueError as erro:
            # Janela curta demais para sortear tantos pares, por exemplo.
            # O run do agente ja terminou e continua valido; o que falta e o
            # controle, e isso precisa ficar dito em vez de sumir.
            log.warning("cerebro.b1_casado_falhou", extra={"motivo": str(erro)})

    reflexoes = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM agent_event"
            " WHERE run_id = ? AND provider IS NOT NULL",
            (run_id,),
        ).fetchone()["n"]
    )

    # A avaliacao posterior: evento NOVO, filho da decisao (R25.3, regra 17).
    #
    # Depois do encerramento de proposito. "Posterior" e o adjetivo que define
    # este evento: ele existe justamente porque o resultado so e conhecido
    # quando tudo acabou. Emiti-lo antes seria declarar realizado o que ainda
    # nao aconteceu - e o erro simetrico ao de editar a decisao depois.
    avaliacao_event_id = avaliacao.registrar(
        conn,
        run_id=run_id,
        config=config,
        b1_casado=b1_casado,
        reflexoes=reflexoes,
        # A serie de onde sai a autocorrelacao que desconta `n_bruto`
        # (secao 8.3). Vem das barras que este run de fato percorreu - nao de
        # uma releitura do dataset, que poderia estar sob outra janela.
        retornos_bps=_retornos_bps(barras),
        duracao_barra_ms=(
            int(barras[1].open_time_ms - barras[0].open_time_ms)
            if len(barras) >= 2
            else 900_000
        ),
    )

    # O parecer INDEPENDENTE do validador (§8.1, R36). Vem depois da
    # autoavaliacao do agente e nao a le: sao as duas avaliacoes que a visao
    # do painel lista lado a lado, e se a segunda copiasse a primeira seriam
    # uma so com dois nomes.
    #
    # Ele pode nao mover a hipotese, e isso e resultado: `inconclusiva` nem
    # promove nem descarta (§14.4).
    parecer = None
    if hypothesis_id is not None:
        try:
            parecer = validador_promocao.avaliar_in_sample(
                conn, hypothesis_id=int(hypothesis_id), run_id=run_id
            ).como_dict()
        except validador_promocao.NaoAvaliavel as erro:
            # Hipotese nao testavel ja foi arquivada na admissao, e arquivada
            # nao volta para a fila. Nao e falha do run.
            log.info("validador.sem_parecer", extra={"motivo": str(erro)})

    # Do LEDGER, e nao de um acumulador do executor: o saldo tem uma fonte
    # so (regra 16). E o mesmo `caixa_cents` que os baselines usam, entao os
    # numeros sao comparaveis por construcao e nao por coincidencia.
    carteira = livro.carteira(conn, run_id=run_id)

    ciclo = ResultadoDoCiclo(
        run_id=run_id,
        reflexoes=reflexoes,
        parou_em=estado.get("parou_em"),
        motivo=estado.get("motivo"),
        regra_veio_do_cerebro=veio_do_cerebro,
        categoria_da_parada=categoria_da_parada,
        atribuicao=paradas.atribuicao(
            veio_do_cerebro=veio_do_cerebro,
            categoria=categoria_da_parada,
            executou=resultado is not None,
        ),
        rule_id=rule_id,
        regra_hash=regra.hash() if resultado is not None else None,
        proposal_id=int(ativa["proposal_id"]) if ativa else None,
        expectativa=ativa["expectation"] if ativa else None,
        confianca_ppm=ativa["confidence_ppm"] if ativa else None,
        execucao=(
            resultado.como_dict()
            if resultado is not None
            else {
                "executou": False,
                "ordens_executadas": 0,
                "idas_e_voltas": 0,
                "por_que": (
                    "nenhuma regra foi executada: o cerebro parou por"
                    f" {categoria_da_parada!r} (D35)"
                ),
            }
        ),
        patrimonio_final_cents=simulador.caixa_cents(conn, run_id),
        custos={
            "execucao_total": carteira["simulado_usd"]["custo_execucao_minor"],
            "posicao_aberta_minor": carteira["simulado_usd"]["posicao_btc_minor"],
            "reflexao_total": carteira["simulado_usd"]["tesouraria_minor"],
        },
        b1_casado=b1_casado,
        gasto=livro.gasto_com_reflexao(conn, run_id),
        sobreposicao=propostas.sobreposicao_amostral(conn, run_id),
        condicoes_validade=condicoes_do_run(conn, run_id),
        avaliacao_event_id=avaliacao_event_id,
        hypothesis_id=int(hypothesis_id) if hypothesis_id else None,
        parecer_do_validador=parecer,
    )
    log.info(
        "cerebro.ciclo",
        extra={
            "run_id": run_id,
            "reflexoes": reflexoes,
            "regra_veio_do_cerebro": veio_do_cerebro,
            "idas_e_voltas": operacoes,
            "categoria_da_parada": categoria_da_parada,
            "patrimonio_final_cents": simulador.caixa_cents(conn, run_id),
        },
    )
    return ciclo


def parada_do_run(conn: sqlite3.Connection, run_id: int) -> dict | None:
    """A ultima parada deste run, com categoria e motivo. `None` se nao houve.

    Derivada do evento, e nao de um campo do run: o `agent_event` e a
    autoridade sobre decisao e caminho (regra 16), e o estado do grafo nao e
    persistido de proposito. Isto e o que faltava para o GET responder a mesma
    pergunta que o POST - antes, o motivo existia so no corpo da resposta do
    POST e no log da plataforma.
    """
    linha = conn.execute(
        "SELECT node, stop_category, stop_reason, occurred_at"
        "  FROM agent_event"
        " WHERE run_id = ? AND kind = 'parada'"
        " ORDER BY id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if linha is None:
        return None
    return {
        "node": linha["node"],
        "categoria": linha["stop_category"],
        "motivo": linha["stop_reason"],
        "quando": linha["occurred_at"],
        # As paradas gravadas ANTES da migracao 13 tem NULL nas duas colunas.
        # Dizer isso e melhor que devolver `None` e deixar quem le concluir
        # que a parada nao teve causa: o registro e incompleto, e o motivo
        # daquela parada especifica so existe no log da plataforma.
        "registro_completo": linha["stop_category"] is not None,
    }


def caminho_percorrido(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """A sequencia de eventos do run, na ordem em que aconteceram (R25.1).

    Inclui paradas e erros - e, desde a migracao 13, **por que** cada parada
    aconteceu. Um caminho que so mostra os runs bem-sucedidos nao e o caminho
    percorrido, e a metade agradavel dele; um que mostra a parada sem a causa
    e a metade inutil da metade que sobrou.

    Ate aqui esta funcao prometia a primeira frase e nao a segunda: o motivo
    ia para o log da plataforma e para o corpo do POST, e o GET nao o tinha.
    """
    return [
        dict(l)
        for l in conn.execute(
            "SELECT id, parent_event_id, occurred_at, node, kind, tier, provider,"
            " model, tokens_in, tokens_out, tokens_cache_read, tokens_cache_write,"
            " cost_usd_minor, cost_usd_micro, price_table_version, expectation,"
            " stop_category, stop_reason,"
            " confidence_ppm, ledger_transaction_id, profile_id"
            " FROM agent_event WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
    ]
