"""O validador promove. O agente não (§8.1, R36).

> "Um agente não pode promover a própria hipótese; a promoção é feita pelo
> módulo validador, que é **independente do agente**." — §8.1

## O que "independente" quer dizer aqui, em três garantias

**1. Não importa o agente.** `app/validador` não importa `app/cerebro`, nem
adaptador de provedor, nem LangGraph. Verificado por AST, como a fronteira de
§3.2 entre mãos rápidas e cérebro. Independência que depende de disciplina já
foi violada.

**2. Não lê a autoavaliação do agente para decidir.** A avaliação posterior de
R25.3 existe e é do agente — é a "avaliação do próprio agente". Esta aqui é a
"avaliação independente do Validador". Se a segunda lesse a primeira para
decidir, seriam uma só com dois nomes. Há teste que apaga a autoavaliação e
exige que o veredito do validador não mude.

**3. Recalcula do que ficou gravado.** Ledger, execuções e pré-registro — não
o `ResultadoDoCiclo` que o ciclo devolveu. Um defeito no caminho do agente que
o validador herdasse não seria pego por nenhum dos dois.

## As duas avaliações podem divergir, e a divergência é informação

Não há nada que force o veredito do validador a bater com o do agente. Se
baterem sempre, ou uma é cópia da outra, ou o cálculo é o mesmo — e nos dois
casos a segunda não está acrescentando o que §8.1 pede dela.

## Inconclusivo não move a hipótese

§14.4: "Nem promove nem descarta. A hipótese permanece em observação e **não
pode ser citada como evidência de sucesso**." Então `inconclusiva` produz
**nenhuma transição** — e isso é resultado, não omissão. Uma hipótese parada em
`hipotese_registrada` depois de avaliada é distinguível de uma nunca avaliada
pelo registro de avaliação, não pelo estado.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

from .. import creditos as creditos_mod
from ..dataset import loader, selado
from ..estatistica import pvalor as pvalor_mod
from ..estatistica import sharpe as sharpe_mod
from ..hipotese import poder
from ..hipotese import registro as hipotese_registro
from ..hipotese import veredito as veredito_mod
from ..hipotese.schema import PreRegistroBruto
from ..maos_rapidas import executor
from ..simulador import execucao as simulador
from . import estados

log = logging.getLogger(__name__)

# O braço experimental do agente. O B4 usa 'b4', e entra no incremento 12 -
# §14.3 exige "mesmo orçamento de créditos de teste" para os dois.
BRACO_DO_AGENTE = "agente"


class NaoAvaliavel(Exception):
    """Falta o que a avaliação precisa. Diferente de "avaliou e reprovou"."""


@dataclass(frozen=True)
class Parecer:
    hypothesis_id: int
    etapa: str
    veredito: str | None
    motivo: str
    transicao: str | None
    estado_final: str
    creditos: int
    detalhe: dict

    def como_dict(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "etapa": self.etapa,
            "veredito": self.veredito,
            "motivo": self.motivo,
            "transicao": self.transicao,
            "estado_final": self.estado_final,
            "creditos": self.creditos,
            "detalhe": self.detalhe,
        }


def _pre_registro(hip: dict) -> PreRegistroBruto:
    return PreRegistroBruto.model_validate(
        {
            "enunciado": hip["enunciado"],
            "metrica_primaria": hip["metrica_primaria"],
            "efeito_minimo": hip["efeito_minimo"],
            "sharpe_esperado_milesimos": hip["sharpe_esperado_milesimos"],
            "criterio_parada": hip["criterio_parada"],
            "condicoes_falseamento": hip["condicoes_falseamento"],
        }
    )


def _b1_do_run(conn: sqlite3.Connection, run_id: int) -> dict | None:
    """A distribuição do acaso casada com este run, lida do banco.

    Do `baseline_result`, e não do objeto que o ciclo devolveu: o validador
    recalcula do que ficou gravado. Se o ciclo tivesse errado ao montar o
    resumo, ler daquele objeto herdaria o erro.
    """
    linhas = [
        int(l["equity_final_cents"])
        for l in conn.execute(
            "SELECT equity_final_cents FROM baseline_result"
            " WHERE run_id = ? AND baseline = 'B1'"
            " ORDER BY repeticao",
            (run_id,),
        )
    ]
    if not linhas:
        return None
    ordenado = sorted(linhas)

    def p(q: int) -> int:
        if len(ordenado) == 1:
            return ordenado[0]
        pos = (len(ordenado) - 1) * q // 100
        return ordenado[pos]

    alvo = conn.execute(
        "SELECT MAX(operacoes) AS n FROM baseline_result"
        " WHERE run_id = ? AND baseline = 'B1'",
        (run_id,),
    ).fetchone()
    return {
        "p5": p(5),
        "p50": p(50),
        "p95": p(95),
        "repeticoes": len(ordenado),
        "operacoes_alvo": int(alvo["n"] or 0),
    }


def _duracao_barra_ms(conn: sqlite3.Connection, run_id: int) -> int:
    linha = conn.execute(
        "SELECT d.interval_ms AS ms FROM execution e"
        " JOIN dataset d ON d.id = e.dataset_id"
        " WHERE e.run_id = ? LIMIT 1",
        (run_id,),
    ).fetchone()
    if linha is None:
        raise NaoAvaliavel(
            f"o run {run_id} não tem execução; não há amostra a avaliar"
        )
    return int(linha["ms"])


def _julgar(
    conn: sqlite3.Connection, hypothesis_id: int, run_id: int
) -> tuple[veredito_mod.Veredito, dict]:
    """O veredito do validador, recalculado do que ficou gravado."""
    hip = hipotese_registro.por_id(conn, hypothesis_id)
    if hip is None:
        raise NaoAvaliavel(f"hipótese {hypothesis_id} não existe")

    pre = _pre_registro(hip)
    duracao = _duracao_barra_ms(conn, run_id)
    bruto = executor.barras_expostas(conn, run_id, duracao)
    efetivo = poder.efetivo_de_bruto(
        # A MESMA serie que a autoavaliacao do agente le. As duas
        # produziam numeros diferentes para `n_efetivo`, e e ele que
        # decide entre `refutada` e `inconclusiva` (§14.4).
        executor.retornos_do_run(conn, run_id),
        bruto,
    )

    realizado = veredito_mod.observar(
        conn,
        run_id=run_id,
        patrimonio_cents=simulador.caixa_cents(conn, run_id),
        idas_e_voltas=executor.idas_e_voltas(conn, run_id),
        b1_casado=_b1_do_run(conn, run_id),
    )
    v = veredito_mod.emitir(
        pre, realizado, n_efetivo=efetivo.efetivo, n_minimo=hip["n_minimo"]
    )
    detalhe = v.como_dict()
    detalhe["amostra"]["n_bruto"] = efetivo.bruto
    detalhe["amostra"]["autocorrelacao_ppm"] = efetivo.autocorrelacao_ppm
    detalhe["run_id"] = run_id
    detalhe["recalculado_pelo_validador"] = True

    # O Sharpe REALIZADO e o p-valor. Nao existiam antes do incremento 11, e
    # sem eles BH/BY nao tem o que ordenar nem o DSR o que deflacionar.
    #
    # Vem junto do veredito, e nao num passo a parte: um p-valor calculado
    # depois, a partir de outra leitura da mesma serie, poderia divergir do
    # que o veredito viu - e ai o lote seria ordenado por numeros que nao
    # descrevem os julgamentos que ele contem.
    detalhe["estatistica"] = _estatistica_do_run(
        conn, run_id, duracao_barra_ms=duracao, n_efetivo=efetivo.efetivo
    )
    return v, detalhe


def _estatistica_do_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    duracao_barra_ms: int,
    n_efetivo: int,
) -> dict:
    """Sharpe realizado, momentos e p-valor. `None` quando a serie e curta.

    `None` com motivo, e nao zero: uma serie de tres barras nao tem quarto
    momento, e afirmar curtose 3 (normal) sobre ela seria inventar
    normalidade que ninguem mediu.
    """
    retornos = executor.retornos_do_run(conn, run_id)
    try:
        m = sharpe_mod.momentos(retornos)
    except sharpe_mod.AmostraCurta as erro:
        return {
            "disponivel": False,
            "por_que": str(erro),
            "p_valor_ppm": None,
        }
    anualizado = m.sharpe_anualizado(duracao_barra_ms)
    teste = pvalor_mod.de_sharpe(
        sharpe_anualizado=anualizado,
        n_efetivo=n_efetivo,
        barras_por_ano_=poder.barras_por_ano(duracao_barra_ms),
    )
    return {
        "disponivel": True,
        "momentos": m.como_dict(duracao_barra_ms),
        "teste": teste.como_dict(),
        "p_valor_ppm": teste.p_valor_ppm,
    }


def avaliar_in_sample(
    conn: sqlite3.Connection, *, hypothesis_id: int, run_id: int
) -> Parecer:
    """`hipotese_registrada` → `candidata`, ou → `invalidado`, ou nada.

    A primeira etapa da máquina de §8.1. Custa 1 crédito (§8.6.1).
    """
    return _avaliar(
        conn,
        hypothesis_id=hypothesis_id,
        run_id=run_id,
        etapa="in_sample",
        de_esperado=estados.ENTRADA,
        promove_para="candidata",
    )


def avaliar_out_of_sample(
    conn: sqlite3.Connection, *, hypothesis_id: int, run_id: int
) -> Parecer:
    """`candidata` → `em_quarentena`, ou → `invalidado`, ou nada.

    Custa 5 créditos: "consome dados reservados, que são finitos e não
    renováveis" (§8.6.1).

    **Exige que o holdout tenha sido consumido por esta hipótese.** Promover
    para quarentena sem ter tocado o período selado seria chamar de
    out-of-sample um teste que não saiu da amostra.
    """
    # O estado vem ANTES da evidência. Uma hipótese em `hipotese_registrada`
    # que ainda não passou pelo in-sample não deve ouvir "faltou o holdout":
    # ela ouviria que o problema é o insumo quando o problema é a ordem.
    _exigir_estado(conn, hypothesis_id, "candidata", etapa="out_of_sample")
    if not selado.ja_consumiu(conn, hypothesis_id):
        raise NaoAvaliavel(
            f"a hipótese {hypothesis_id} não leu o holdout; sem o teste no"
            " período selado não há out-of-sample a avaliar (§8.5.1)"
        )
    return _avaliar(
        conn,
        hypothesis_id=hypothesis_id,
        run_id=run_id,
        etapa="out_of_sample",
        de_esperado="candidata",
        promove_para=estados.QUARENTENA,
    )


def _config_version(conn: sqlite3.Connection, run_id: int) -> int:
    linha = conn.execute(
        "SELECT config_version_id FROM run WHERE id = ?", (run_id,)
    ).fetchone()
    if linha is None:
        raise NaoAvaliavel(f"run {run_id} não existe")
    return int(linha["config_version_id"])


def _familia_max(conn: sqlite3.Connection, run_id: int) -> int:
    """O teto da família da config que abriu o run (§8.6, D25).

    Da config do RUN, e não da vigente: a família é propriedade do lote, e
    "não ajustável durante" (§8.6). Ler a vigente faria a multiplicidade
    mudar no meio.
    """
    linha = conn.execute(
        "SELECT COALESCE("
        "  json_extract(cv.payload_json, '$.familia_max_hipoteses'), 48"
        ") AS teto FROM config_version cv"
        " WHERE cv.id = (SELECT config_version_id FROM run WHERE id = ?)",
        (run_id,),
    ).fetchone()
    return int(linha["teto"]) if linha else 48


def _barras_do_holdout(conn: sqlite3.Connection, run_id: int) -> int:
    """Pergunta ao modulo dono do selado, e nao ao banco.

    Escrever a consulta aqui foi recusado pela guarda do incremento 9 - com
    razao: uma guarda que precisasse distinguir "ler o dado" de "ler quanto
    dado existe" deixaria de ser verificavel.
    """
    linha = conn.execute(
        "SELECT MIN(dataset_id) AS ds FROM execution WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if linha is None or linha["ds"] is None:
        return 0
    return selado.barras_do_holdout(conn, int(linha["ds"]))


def parecer_derivado(
    conn: sqlite3.Connection, hypothesis_id: int
) -> dict | None:
    """O veredito desta hipotese, RECALCULADO do que ficou gravado.

    Derivado, e nao lido de coluna, pelo mesmo motivo do saldo e do estado
    corrente: o veredito e funcao do pre-registro (imutavel), das execucoes
    (append-only) e do controle do run. Nenhum dos tres muda, entao recalcular
    da a mesma resposta para sempre - e guardar uma copia criaria a segunda
    fonte de verdade que a regra 16 proibe.

    **Por que precisou existir.** Um parecer `inconclusiva` nao escreve
    transicao (§14.4: nem promove nem descarta), entao ele nao aparecia em
    rota nenhuma: `/api/validador/hipotese/{id}` mostrava estado e historico, e
    os dois ficam iguais aos de uma hipotese nunca avaliada. O parecer existia
    no corpo do POST do ciclo e em `log.info`, e morria ali - a terceira vez
    que este projeto guarda uma resposta so onde ninguem consegue le-la.

    `None` quando nao ha o que julgar (a hipotese nao existe, ou o run dela nao
    executou nada). E resposta, e nao falha: com a D35, um run sem decisao
    cognitiva nao executa - e sem execucao nao ha amostra sobre que opinar.
    """
    linha = conn.execute(
        "SELECT run_id FROM hypothesis WHERE id = ?", (hypothesis_id,)
    ).fetchone()
    if linha is None:
        return None
    try:
        v, detalhe = _julgar(conn, hypothesis_id, int(linha["run_id"]))
    except NaoAvaliavel as erro:
        return {
            "veredito": None,
            "motivo": str(erro),
            "recalculado": True,
        }
    return {
        "veredito": v.veredito,
        "motivo": v.motivo,
        "detalhe": detalhe,
        "recalculado": True,
        "nota": (
            "recalculado do banco a cada leitura, e nao gravado: o veredito e"
            " funcao do pre-registro imutavel e de tabelas append-only"
        ),
    }


def _exigir_estado(
    conn: sqlite3.Connection, hypothesis_id: int, esperado: str, *, etapa: str
) -> estados.Estado:
    estado = estados.atual(conn, hypothesis_id)
    if estado is None:
        raise NaoAvaliavel(f"hipótese {hypothesis_id} não existe")
    if estado.estado != esperado:
        raise NaoAvaliavel(
            f"a etapa '{etapa}' parte de '{esperado}', e a hipótese"
            f" {hypothesis_id} está em '{estado.estado}'. Nenhum estado pode"
            " ser pulado (§8.1)"
        )
    return estado


def _avaliar(
    conn: sqlite3.Connection,
    *,
    hypothesis_id: int,
    run_id: int,
    etapa: str,
    de_esperado: str,
    promove_para: str,
) -> Parecer:
    estado = _exigir_estado(conn, hypothesis_id, de_esperado, etapa=etapa)

    # ---------------------------------------------------------- creditos
    #
    # COBRADO ANTES de julgar, e nao depois. Cobrar depois deixaria o teste
    # rodar sem orcamento e so entao recusar - o gasto de CPU e a leitura do
    # dado teriam acontecido, e a escassez da secao 8.6.1 seria decorativa.
    #
    # O tipo sai do CONTEUDO: hipotese cujo hash ja apareceu antes e reteste
    # com parametro alterado, e custa 3 em vez de 1.
    tipo = creditos_mod.tipo_do_teste(conn, hypothesis_id, etapa=etapa)
    familia_max = _familia_max(conn, run_id)
    inicio = time.perf_counter_ns()

    v, detalhe = _julgar(conn, hypothesis_id, run_id)

    creditos = creditos_mod.cobrar(
        conn,
        braco=BRACO_DO_AGENTE,
        config_version_id=_config_version(conn, run_id),
        hypothesis_id=hypothesis_id,
        tipo=tipo,
        # Quarto numero de R43: custo computacional REAL, medido. O relogio
        # cobre exatamente o julgamento - nao a transicao nem o log.
        cpu_micros=(time.perf_counter_ns() - inicio) // 1_000,
        # Quinto: dado reservado consumido. So o out-of-sample toca o selado.
        barras_reservadas=(
            _barras_do_holdout(conn, run_id) if etapa == "out_of_sample" else 0
        ),
        familia_max=familia_max,
    )
    evidencia = {"etapa": etapa, "tipo_cobrado": tipo, "creditos": creditos, **detalhe}

    if v.veredito == "sustentada":
        estados.transitar(
            conn, hypothesis_id, para=promove_para, evidencia=evidencia
        )
        transicao, final = promove_para, promove_para
    elif v.veredito == "refutada":
        estados.transitar(
            conn, hypothesis_id, para="invalidado", evidencia=evidencia
        )
        transicao, final = "invalidado", "invalidado"
    else:
        # `inconclusiva` e `None` não movem a hipótese. §14.4: nem promove nem
        # descarta. Não é omissão - é o terceiro resultado.
        transicao, final = None, estado.estado

    log.info(
        "validador.parecer",
        extra={
            "hypothesis_id": hypothesis_id,
            "etapa": etapa,
            "veredito": v.veredito,
            "transicao": transicao,
        },
    )
    return Parecer(
        hypothesis_id=hypothesis_id,
        etapa=etapa,
        veredito=v.veredito,
        motivo=v.motivo,
        transicao=transicao,
        estado_final=final,
        creditos=creditos,
        detalhe=detalhe,
    )


def arquivar_nao_testavel(
    conn: sqlite3.Connection, hypothesis_id: int
) -> Parecer:
    """`hipotese_registrada` → `nao_testavel`, terminal (§8.3, R35).

    Só o validador arquiva, como só ele promove. A hipótese nasce marcada
    `testavel = 0` no pré-registro (incremento 8); esta função é o que a tira
    da fila de avaliação — e D33 já registrou que isso bloqueia a **promoção**,
    não a execução retrospectiva que já aconteceu.
    """
    hip = hipotese_registro.por_id(conn, hypothesis_id)
    if hip is None:
        raise NaoAvaliavel(f"hipótese {hypothesis_id} não existe")
    if hip["testavel"]:
        raise NaoAvaliavel(
            f"a hipótese {hypothesis_id} é testável; arquivá-la como não"
            " testável seria afirmar sobre a amostra o contrário do que a"
            " conta de poder disse no pré-registro"
        )
    evidencia = {
        "etapa": "triagem",
        "motivo": hip["motivo_nao_testavel"],
        "n_minimo": hip["n_minimo"],
        "horizonte_barras": hip["horizonte_barras"],
        "creditos": 0,
    }
    estados.transitar(
        conn, hypothesis_id, para="nao_testavel", evidencia=evidencia
    )
    return Parecer(
        hypothesis_id=hypothesis_id,
        etapa="triagem",
        veredito=None,
        motivo=hip["motivo_nao_testavel"] or "",
        transicao="nao_testavel",
        estado_final="nao_testavel",
        creditos=0,
        detalhe=evidencia,
    )


def admitir(
    conn: sqlite3.Connection, hypothesis_id: int, *, run_id: int
) -> Parecer:
    """Põe a hipótese na máquina, em `hipotese_registrada` (§8.1).

    **Mora aqui, e não no grafo, de propósito.** A inserção em
    `hypothesis_state` acontece exclusivamente dentro de `app/validador` — há
    teste que varre o código e recusa qualquer outro módulo que escreva nessa
    tabela. Se o grafo gravasse a linha de entrada, o agente estaria mexendo
    na máquina que decide se ele promove, ainda que só para entrar nela.

    O ciclo do agente **solicita** esta admissão, e é a mesma relação que
    §11.2.1 descreve: "o agente solicita; quem executa é o Validador".
    """
    hip = hipotese_registro.por_id(conn, hypothesis_id)
    if hip is None:
        raise NaoAvaliavel(f"hipótese {hypothesis_id} não existe")

    estados.registrar_entrada(
        conn,
        hypothesis_id,
        evidencia={
            "etapa": "pre_registro",
            "run_id": run_id,
            "content_hash": hip["content_hash"],
            "n_minimo": hip["n_minimo"],
            "testavel": hip["testavel"],
            "creditos": 0,
        },
    )
    # Não testável nasce e é arquivada no mesmo ato (§8.3, R35). São duas
    # transições, e não uma: a hipótese entrou na máquina e saiu dela por um
    # motivo registrado. Pular a entrada esconderia que ela chegou a existir.
    if not hip["testavel"]:
        return arquivar_nao_testavel(conn, hypothesis_id)

    return Parecer(
        hypothesis_id=hypothesis_id,
        etapa="pre_registro",
        veredito=None,
        motivo="admitida na máquina de estados; ainda não avaliada",
        transicao=estados.ENTRADA,
        estado_final=estados.ENTRADA,
        creditos=0,
        detalhe={"content_hash": hip["content_hash"]},
    )
