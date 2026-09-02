"""Persistencia da regra e o vinculo com a execucao (criterio 7, R25.5).

Duas garantias:

- **Id de conteudo.** Registrar a mesma regra duas vezes devolve a mesma
  linha. O hash e do que a regra DIZ, nao de quando foi escrita.

- **Congelamento com data.** B3 tem de ser congelado antes do primeiro run do
  agente (D4, criterio 5). Sem timestamp, "congelado" e intencao; com ele, o
  trigger recusa qualquer alteracao posterior. Retunar parametro depois de
  ver o resultado destroi o grupo de controle - e o banco impede.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone

from .schema import CondicoesValidade, Regra

log = logging.getLogger(__name__)


class RegraCongelada(Exception):
    pass


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonico(dados: dict) -> str:
    return json.dumps(dados, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def registrar(conn: sqlite3.Connection, regra: Regra) -> int:
    """Grava a regra do catalogo, ou devolve a existente com o mesmo hash."""
    return _gravar(
        conn,
        hash_=regra.hash(),
        kind="catalogo",
        family=regra.familia,
        params_json=_canonico(regra.params.model_dump(mode="json")),
        condicoes=regra.condicoes_validade,
        extra={
            "position_fraction_bps": regra.position_fraction_bps,
            "stop_loss_bps": regra.stop_loss_bps,
        },
    )


def registrar_baseline(
    conn: sqlite3.Connection,
    family: str,
    params: dict,
    condicoes: CondicoesValidade,
) -> int:
    """Grava a "regra" de um baseline.

    Buy and hold e aleatorio nao vem do catalogo fechado da D5 e nunca
    poderiam vir: nao sao hipoteses, sao controles. Mas sao a regra que
    autorizou aquelas execucoes, e o criterio 7 nao abre excecao para
    controle - de qualquer execucao se chega a regra que a autorizou.
    """
    return _gravar(
        conn,
        hash_=hashlib.sha256(
            _canonico({"baseline": family, **params}).encode("utf-8")
        ).hexdigest(),
        kind="baseline",
        family=family,
        params_json=_canonico(params),
        condicoes=condicoes,
        extra={},
    )


def _gravar(
    conn: sqlite3.Connection,
    *,
    hash_: str,
    kind: str,
    family: str,
    params_json: str,
    condicoes: CondicoesValidade,
    extra: dict,
) -> int:
    existente = conn.execute(
        "SELECT id FROM rule WHERE hash = ?", (hash_,)
    ).fetchone()
    if existente is not None:
        return int(existente["id"])

    cur = conn.execute(
        "INSERT INTO rule (hash, kind, family, params_json,"
        " condicoes_validade_json, created_at) VALUES (?,?,?,?,?,?)",
        (
            hash_,
            kind,
            family,
            _canonico({**json.loads(params_json), **extra}),
            _canonico(condicoes.model_dump(mode="json")),
            _agora(),
        ),
    )
    rule_id = int(cur.lastrowid)
    log.info("regra.registrada", extra={"rule_id": rule_id, "family": family,
                                        "hash": hash_})
    return rule_id


def congelar(conn: sqlite3.Connection, rule_id: int) -> str:
    """Marca a regra como congelada. Idempotente na data ja gravada."""
    linha = conn.execute(
        "SELECT frozen_at FROM rule WHERE id = ?", (rule_id,)
    ).fetchone()
    if linha is None:
        raise ValueError(f"regra {rule_id} nao existe")
    if linha["frozen_at"]:
        return linha["frozen_at"]
    quando = _agora()
    conn.execute("UPDATE rule SET frozen_at = ? WHERE id = ?", (quando, rule_id))
    log.info("regra.congelada", extra={"rule_id": rule_id, "frozen_at": quando})
    return quando


def esta_congelada(conn: sqlite3.Connection, rule_id: int) -> bool:
    linha = conn.execute(
        "SELECT frozen_at FROM rule WHERE id = ?", (rule_id,)
    ).fetchone()
    return bool(linha and linha["frozen_at"])


def regra_da_execucao(conn: sqlite3.Connection, execution_id: int) -> dict | None:
    """Criterio 7: partindo de uma execucao, chegar a regra que a autorizou."""
    linha = conn.execute(
        "SELECT r.id, r.hash, r.kind, r.family, r.params_json,"
        " r.condicoes_validade_json, r.frozen_at"
        " FROM execution e JOIN rule r ON r.id = e.rule_id"
        " WHERE e.id = ?",
        (execution_id,),
    ).fetchone()
    return dict(linha) if linha else None


def execucoes_sem_regra(conn: sqlite3.Connection, run_id: int) -> list[int]:
    """Execucoes de um run que nao apontam para regra nenhuma.

    Uma conferencia que nunca acusa nada nao esta conferindo: esta existe para
    que o criterio 7 seja verificavel em qualquer momento, e nao so no teste.
    """
    return [
        int(l["id"])
        for l in conn.execute(
            "SELECT id FROM execution WHERE run_id = ? AND rule_id IS NULL",
            (run_id,),
        )
    ]
