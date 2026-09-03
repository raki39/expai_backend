"""Grava e le o pre-registro. Nunca edita (secao 8.2).

O modulo Python **nao valida** a obrigatoriedade do falseamento nem a
coerencia entre `testavel` e o motivo. Isso e de proposito, e e o mesmo
desenho das partidas dobradas do incremento 2: se validasse aqui, um defeito
neste arquivo mascararia a ausencia da regra no banco, e a suite passaria a
provar que o Python esta correto em vez de provar que o dado esta protegido.

O que este modulo faz e montar a linha e deixar o banco recusar. Os testes
inserem SQL cru e esperam a recusa.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

from . import poder
from .schema import PreRegistroBruto, hash_do_conteudo

log = logging.getLogger(__name__)

# Um agente so na Fase 0. O identificador existe porque a secao 8.2 pede
# `agente_origem`, e porque o contador global de tentativas e POR
# especialidade (secao 8.6): sem o campo, migrar para mais de um agente
# exigiria reprocessar todo o registro.
#
# Inerte para a DECISAO, como `profile_id` (regra 18): nenhum ramo de decisao
# le este valor. Mas ele **nao** e inerte para a contabilidade da fase - e por
# ele que `tentativas_por_especialidade` separa os bracos, e §8.6 exige que o
# orcamento seja da especialidade.
AGENTE_ORIGEM = "transacao@0b"

#: O braco de controle da secao 14.3. Origem propria, e nao um sinalizador
#: dentro do do agente: a comparacao da fase e "por credito gasto" entre os
#: dois bracos, e ela precisa que o contador os separe sozinho.
#:
#: Entra no `content_hash`, entao a MESMA regra proposta pelos dois bracos
#: conta como duas hipoteses, e nao como reteste. E o que se quer: sao duas
#: tentativas independentes na mesma familia (§9.2), e tratar uma como
#: repeticao da outra subestimaria a multiplicidade.
AGENTE_ORIGEM_B4 = "b4@0b"

#: Os controles negativos de §14.4, cada um com origem propria.
#:
#: A1a sao os DETERMINISTICOS - "construidas para revelar defeito", tolerancia
#: zero. A1b sao as NULAS ESTOCASTICAS do lote real, avaliadas contra o FDR
#: pre-registrado. Duas origens e nao uma porque as tolerancias sao
#: diferentes: uma promocao de A1a reprova a fase, e uma promocao ocasional de
#: A1b e o comportamento esperado de um procedimento com FDR positivo. Somar
#: as duas num rotulo so faria "controle promovido" perder o significado.
AGENTE_ORIGEM_A1A = "a1a@0b"
AGENTE_ORIGEM_A1B = "a1b@0b"


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonico(dados) -> str:
    return json.dumps(dados, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def registrar(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    agent_event_id: int,
    bruto: PreRegistroBruto,
    condicoes_validade: dict,
    duracao_barra_ms: int,
    horizonte_barras: int,
    rule_id: int | None = None,
    supersedes: int | None = None,
    agente_origem: str = AGENTE_ORIGEM,
) -> tuple[int, bool]:
    """Grava a hipotese. Devolve `(id, testavel)`.

    `n_minimo` e CALCULADO aqui (R34) a partir do Sharpe declarado, nunca
    recebido pronto. Quando ele nao cabe no horizonte, a hipotese nasce
    **nao testavel** (secao 8.3, R35), com o motivo gravado.

    **O que "nao testavel" gateia, e o que nao gateia** (D33, ADR 0020): ela
    nao pode ser PROMOVIDA, e por construcao seu veredito sai `inconclusiva`,
    porque `n_efetivo` nunca alcanca um `n_minimo` maior que o horizonte. Ela
    continua executando retrospectivamente.

    Recusar a execucao seria a leitura literal de "arquivada", e foi
    considerada. Duas coisas a derrubaram. A secao 8.3 da o proprio motivo da
    triagem - impedir "que uma hipotese lenta ocupe capacidade de observacao
    indefinidamente" -, e capacidade de observacao e o forward, que e 0C; um
    backtest retrospectivo custa CPU. E recusar aqui faria `arquivada` e
    `refutada` terem o mesmo efeito pratico, que e exatamente a confusao que
    a secao 14.4 chama de "erro simetrico ao de promover ruido".
    """
    n_min = poder.n_minimo(
        sharpe_milesimos=bruto.sharpe_esperado_milesimos,
        duracao_barra_ms=duracao_barra_ms,
    )

    testavel = 1
    motivo: str | None = None
    try:
        poder.conferir_horizonte(n_min=n_min, horizonte_barras=horizonte_barras)
    except poder.HorizonteInsuficiente as erro:
        testavel = 0
        motivo = str(erro)

    conteudo = hash_do_conteudo(bruto, condicoes_validade, agente_origem)

    cur = conn.execute(
        "INSERT INTO hypothesis ("
        "  run_id, agent_event_id, enunciado, agente_origem,"
        "  timestamp_registro, metrica_primaria, efeito_minimo, n_minimo,"
        "  sharpe_esperado_milesimos, criterio_parada,"
        "  condicoes_validade_json, condicoes_falseamento_json,"
        "  testavel, motivo_nao_testavel, horizonte_barras,"
        "  rule_id, supersedes, content_hash"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            agent_event_id,
            bruto.enunciado.strip(),
            agente_origem,
            _agora(),
            bruto.metrica_primaria,
            bruto.efeito_minimo,
            n_min,
            bruto.sharpe_esperado_milesimos,
            bruto.criterio_parada,
            _canonico(condicoes_validade),
            _canonico(
                [c.model_dump(mode="json") for c in bruto.condicoes_falseamento]
            ),
            testavel,
            motivo,
            horizonte_barras,
            rule_id,
            supersedes,
            conteudo,
        ),
    )
    hypothesis_id = int(cur.lastrowid)
    log.info(
        "hipotese.registrada",
        extra={
            "run_id": run_id,
            "hypothesis_id": hypothesis_id,
            "n_minimo": n_min,
            "testavel": bool(testavel),
            "content_hash": conteudo[:12],
        },
    )
    return hypothesis_id, bool(testavel)


def do_run(conn: sqlite3.Connection, run_id: int) -> dict | None:
    """A ultima hipotese pre-registrada neste run, ja desserializada."""
    linha = conn.execute(
        "SELECT * FROM hypothesis WHERE run_id = ? ORDER BY id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    return None if linha is None else como_dict(linha)


def por_id(conn: sqlite3.Connection, hypothesis_id: int) -> dict | None:
    linha = conn.execute(
        "SELECT * FROM hypothesis WHERE id = ?", (hypothesis_id,)
    ).fetchone()
    return None if linha is None else como_dict(linha)


def como_dict(linha: sqlite3.Row) -> dict:
    return {
        "id": int(linha["id"]),
        "run_id": int(linha["run_id"]),
        "agent_event_id": int(linha["agent_event_id"]),
        "enunciado": linha["enunciado"],
        "agente_origem": linha["agente_origem"],
        "timestamp_registro": linha["timestamp_registro"],
        "metrica_primaria": linha["metrica_primaria"],
        "efeito_minimo": int(linha["efeito_minimo"]),
        "n_minimo": int(linha["n_minimo"]),
        "sharpe_esperado_milesimos": int(linha["sharpe_esperado_milesimos"]),
        "criterio_parada": linha["criterio_parada"],
        "condicoes_validade": json.loads(linha["condicoes_validade_json"]),
        "condicoes_falseamento": json.loads(linha["condicoes_falseamento_json"]),
        "testavel": bool(linha["testavel"]),
        "motivo_nao_testavel": linha["motivo_nao_testavel"],
        "horizonte_barras": int(linha["horizonte_barras"]),
        "rule_id": (
            int(linha["rule_id"]) if linha["rule_id"] is not None else None
        ),
        "supersedes": (
            int(linha["supersedes"]) if linha["supersedes"] is not None else None
        ),
        "content_hash": linha["content_hash"],
    }


def tentativas_por_hash(conn: sqlite3.Connection, content_hash: str) -> int:
    """Quantas vezes esta MESMA hipotese ja foi registrada.

    A secao 8.6.1 cobra 1 credito pelo teste in-sample e 3 pelo reteste com
    parametro alterado, "porque varredura de parametro e a principal fonte de
    sobreajuste". Distinguir os dois exige reconhecer a hipotese pelo
    conteudo - e nao pela boa memoria de quem a registrou.

    O contador so e CONSUMIDO no incremento 11; aqui ele ja existe porque
    perde-lo agora significaria nao poder reconstrui-lo depois.
    """
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM hypothesis WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()["n"]
    )
