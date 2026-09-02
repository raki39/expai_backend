"""Cache de respostas do provedor, enderecado por conteudo (criterio 4).

A chave e o hash do pedido INTEIRO. Trocar o modelo, o sistema, a mensagem, o
schema ou o limite de saida troca a chave - entao um acerto so acontece quando
a pergunta e literalmente a mesma. Nao existe "acerto aproximado", e e por
isso que reexecutar um run com o cache quente reproduz a decisao em vez de
inventar outra.

**O que o acerto de cache muda e o que ele nao muda:**

    nao muda  a decisao, a regra, as execucoes, o digest do run
    nao muda  o custo no livro SIMULADO - o agente pagou pelo pensamento,
              e isso e fato do experimento, nao do nosso cache
    muda      o livro REAL: nenhum centavo saiu da conta

Se o acerto de cache tornasse o run mais barato para o agente, reexecutar um
run melhoraria o resultado dele. Seria uma vantagem vinda de fora do
experimento, indistinguivel de desempenho no numero final.

O cache **nao e historia**: esvazia-lo nao apaga registro nenhum, so faz o
proximo run pagar de novo. Por isso a tabela aceita DELETE e recusa UPDATE -
a mesma chave nao pode passar a devolver outra resposta sem que a chave mude.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from ..ledger.livro import Uso
from .provedores.base import Pedido, Resposta

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Acerto:
    resposta: Resposta
    custo_micro_original: int
    criado_em: str


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def buscar(conn: sqlite3.Connection, pedido: Pedido) -> Acerto | None:
    linha = conn.execute(
        "SELECT response_json, usage_json, cost_usd_minor, created_at"
        " FROM llm_cache WHERE key = ?",
        (pedido.chave(),),
    ).fetchone()
    if linha is None:
        return None

    guardado = json.loads(linha["response_json"])
    uso = json.loads(linha["usage_json"])
    log.info(
        "cerebro.cache_acerto",
        extra={"chave": pedido.chave()[:12], "provider": pedido.provider},
    )
    return Acerto(
        resposta=Resposta(
            texto=guardado["texto"],
            uso=Uso(
                tokens_in=uso.get("tokens_in"),
                tokens_out=uso.get("tokens_out"),
                tokens_cache_read=uso.get("tokens_cache_read"),
                tokens_cache_write=uso.get("tokens_cache_write"),
                bruto=uso.get("bruto"),
            ),
            bruto=guardado.get("bruto", {}),
        ),
        custo_micro_original=int(guardado["custo_micro"]),
        criado_em=linha["created_at"],
    )


def guardar(
    conn: sqlite3.Connection,
    pedido: Pedido,
    resposta: Resposta,
    *,
    custo_micro: int,
) -> None:
    """Grava a resposta. Silencioso se a chave ja existe.

    `INSERT OR IGNORE` e nao `REPLACE`: a chave e o conteudo do pedido, entao
    duas respostas diferentes sob a mesma chave significam que o provedor nao
    foi deterministico - e a primeira e a que o run ja usou. Sobrescrever
    faria a reexecucao divergir da execucao original sem nenhum aviso.
    """
    conn.execute(
        "INSERT OR IGNORE INTO llm_cache (key, provider, model, request_json,"
        " response_json, usage_json, cost_usd_minor, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            pedido.chave(),
            pedido.provider,
            pedido.model,
            pedido.canonico(),
            json.dumps(
                {
                    "texto": resposta.texto,
                    "bruto": resposta.bruto,
                    "custo_micro": custo_micro,
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "tokens_in": resposta.uso.tokens_in,
                    "tokens_out": resposta.uso.tokens_out,
                    "tokens_cache_read": resposta.uso.tokens_cache_read,
                    "tokens_cache_write": resposta.uso.tokens_cache_write,
                    "bruto": resposta.uso.bruto,
                },
                ensure_ascii=False,
            ),
            -(-custo_micro // 10_000),
            _agora(),
        ),
    )


def tamanho(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM llm_cache").fetchone()["n"])
