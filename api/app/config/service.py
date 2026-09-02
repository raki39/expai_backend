"""Versionamento da configuracao do experimento (ADR 0008).

Implementa o que a secao 10.2.3 exige: "Toda alteracao de configuracao e
evento versionado no ledger, com autor, data, valor anterior e novo."

Tres travas:

1. Config **congela durante run ativo** - alterar parametro no meio de um
   replay quebra reprodutibilidade.
2. O teto operacional do banco **nao pode exceder** `LLM_MAX_USD_ABSOLUTE`
   do ambiente (secao 12.1: o limite mora fora do codigo).
3. Alteracao material e **marcada**, para o painel avisar que invalida
   comparacao com runs anteriores.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from ..migrations import ESTADOS_ATIVOS
from ..settings import Settings
from .schema import ExperimentConfig, campo_material

log = logging.getLogger(__name__)


class ErroConfig(Exception):
    """Base das recusas de alteracao de configuracao."""


class ConfigCongelada(ErroConfig):
    """Ha run ativo; alterar agora quebraria a reprodutibilidade dele."""


class TetoExcedido(ErroConfig):
    """A alteracao tentou ultrapassar o limite inviolavel do ambiente."""


class SemMudanca(ErroConfig):
    """A alteracao nao muda nenhum campo."""


class SchemaDivergente(ErroConfig):
    """O hash gravado nao descreve mais a configuracao que ele identifica.

    Acontece quando `ExperimentConfig` ganha ou perde um campo depois que uma
    versao foi gravada: o `payload_json` continua o mesmo, mas reconstrui-lo
    produz um objeto diferente do que produzia antes, e portanto outro hash.

    Isso importa porque `config_hash` e a IDENTIDADE da configuracao de um
    run. Dois runs poderiam reportar o mesmo hash tendo rodado com configs
    diferentes - e ai a comparacao entre eles mente sem que nada acuse.
    """


@dataclass(frozen=True)
class VersaoConfig:
    id: int
    created_at: str
    author: str
    parent_version_id: int | None
    config_hash: str
    material: bool
    note: str
    config: ExperimentConfig


# --------------------------------------------------------------- consultas


def run_ativo(conn: sqlite3.Connection) -> int | None:
    """Id do run ativo, se houver. Base da trava de congelamento."""
    marcadores = ",".join("?" for _ in ESTADOS_ATIVOS)
    linha = conn.execute(
        f"SELECT id FROM run WHERE state IN ({marcadores}) LIMIT 1",
        ESTADOS_ATIVOS,
    ).fetchone()
    return int(linha["id"]) if linha else None


def _linha_para_versao(linha: sqlite3.Row) -> VersaoConfig:
    return VersaoConfig(
        id=int(linha["id"]),
        created_at=linha["created_at"],
        author=linha["author"],
        parent_version_id=(
            int(linha["parent_version_id"])
            if linha["parent_version_id"] is not None
            else None
        ),
        config_hash=linha["config_hash"],
        material=bool(linha["material"]),
        note=linha["note"] or "",
        config=ExperimentConfig.model_validate_json(linha["payload_json"]),
    )


def conferir_hash(versao: VersaoConfig) -> str | None:
    """O hash gravado ainda descreve esta configuracao? Devolve o recalculado
    quando NAO bate, e None quando bate.

    Barato de rodar e a unica coisa que separa "o hash identifica a config" de
    "o hash identificava a config quando foi escrito".
    """
    recalculado = versao.config.config_hash()
    return None if recalculado == versao.config_hash else recalculado


def versao_atual(conn: sqlite3.Connection) -> VersaoConfig | None:
    linha = conn.execute(
        "SELECT * FROM config_version ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _linha_para_versao(linha) if linha else None


def exigir_hash_integro(conn: sqlite3.Connection) -> None:
    """Recusa seguir se o hash da configuracao vigente nao a descreve mais.

    Chamado antes de ABRIR RUN, e nao no boot: derrubar o servico deixaria o
    painel inacessivel justamente quando e preciso olhar a configuracao para
    resolver. O que nao pode acontecer e produzir RESULTADO sob um hash que
    identifica outra coisa.
    """
    atual = versao_atual(conn)
    if atual is None:
        return
    recalculado = conferir_hash(atual)
    if recalculado is not None:
        raise SchemaDivergente(
            f"a versao {atual.id} foi gravada com config_hash "
            f"{atual.config_hash}, mas reconstrui-la hoje produz "
            f"{recalculado}. O schema da configuracao mudou desde entao. "
            "Crie uma nova versao antes de abrir run: rodar assim faria dois "
            "runs reportarem o mesmo hash com configuracoes diferentes."
        )


def versao_por_id(conn: sqlite3.Connection, version_id: int) -> VersaoConfig | None:
    linha = conn.execute(
        "SELECT * FROM config_version WHERE id = ?", (version_id,)
    ).fetchone()
    return _linha_para_versao(linha) if linha else None


def historico(conn: sqlite3.Connection, limite: int = 50) -> list[dict]:
    """Versoes com os campos que mudaram em cada uma.

    E o que a secao 10.2.3 pede que o painel mostre: autor, data, valor
    anterior e valor novo.
    """
    versoes = conn.execute(
        "SELECT * FROM config_version ORDER BY id DESC LIMIT ?", (limite,)
    ).fetchall()

    saida: list[dict] = []
    for v in versoes:
        mudancas = conn.execute(
            "SELECT field, old_value_json, new_value_json, material"
            " FROM config_change WHERE version_id = ? ORDER BY field",
            (v["id"],),
        ).fetchall()
        saida.append(
            {
                "version_id": int(v["id"]),
                "created_at": v["created_at"],
                "author": v["author"],
                "parent_version_id": v["parent_version_id"],
                "config_hash": v["config_hash"],
                "material": bool(v["material"]),
                "note": v["note"] or "",
                "changes": [
                    {
                        "field": m["field"],
                        "old_value": (
                            json.loads(m["old_value_json"])
                            if m["old_value_json"] is not None
                            else None
                        ),
                        "new_value": (
                            json.loads(m["new_value_json"])
                            if m["new_value_json"] is not None
                            else None
                        ),
                        "material": bool(m["material"]),
                    }
                    for m in mudancas
                ],
            }
        )
    return saida


# ----------------------------------------------------------------- travas


def _checar_teto(config: ExperimentConfig, settings: Settings) -> None:
    """Trava 2: o teto do banco nao pode exceder o limite do ambiente."""
    limite_cents = int(
        (settings.llm_max_usd_absolute * Decimal(100)).to_integral_value()
    )
    if config.max_llm_usd_per_run_cents > limite_cents:
        raise TetoExcedido(
            "max_llm_usd_per_run_cents="
            f"{config.max_llm_usd_per_run_cents} excede o limite inviolavel "
            f"LLM_MAX_USD_ABSOLUTE={settings.llm_max_usd_absolute} "
            f"({limite_cents} centavos). O limite mora fora do codigo."
        )


# ------------------------------------------------------------- alteracao


def _inserir_versao(
    conn: sqlite3.Connection,
    config: ExperimentConfig,
    author: str,
    parent_id: int | None,
    mudancas: list[tuple[str, object, object]],
    note: str,
) -> VersaoConfig:
    material = any(campo_material(campo) for campo, _, _ in mudancas)

    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            "INSERT INTO config_version"
            " (created_at, author, parent_version_id, payload_json,"
            "  config_hash, material, note)"
            " VALUES (datetime('now'), ?, ?, ?, ?, ?, ?)",
            (
                author,
                parent_id,
                config.model_dump_json(),
                config.config_hash(),
                1 if material else 0,
                note,
            ),
        )
        version_id = int(cur.lastrowid)

        for campo, antes, depois in mudancas:
            conn.execute(
                "INSERT INTO config_change"
                " (version_id, field, old_value_json, new_value_json, material)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    version_id,
                    campo,
                    None if antes is None else json.dumps(antes, ensure_ascii=False),
                    None if depois is None else json.dumps(depois, ensure_ascii=False),
                    1 if campo_material(campo) else 0,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    log.info(
        "config.version_created",
        extra={
            "config_version": version_id,
            "config_hash": config.config_hash(),
            "author": author,
            "material": material,
            "changed_fields": [c for c, _, _ in mudancas],
        },
    )
    versao = versao_por_id(conn, version_id)
    assert versao is not None
    return versao


class SemDeriva(ErroConfig):
    """Nao ha o que reancorar: o hash gravado ainda descreve a configuracao."""


def reancorar(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    author: str,
    note: str = "",
) -> VersaoConfig:
    """Grava uma versao nova com a config vigente e o hash CORRETO.

    Existe porque acrescentar campo a `ExperimentConfig` muda o hash de toda
    versao ja gravada: o `payload_json` continua o mesmo, mas reconstrui-lo
    passa a produzir um objeto diferente. `exigir_hash_integro` recusa abrir
    run nesse estado, e com razao - e o caminho de saida nao pode ser
    `criar_versao`, que devolveria `SemMudanca` porque a config EFETIVA nao
    mudou. So o hash mudou.

    O que fica registrado nao e um valor alterado: e a **mudanca de schema**,
    campo por campo, com o valor que cada campo novo assumiu. Isso importa
    porque o campo novo passa a fazer parte do experimento a partir daqui, e
    quem comparar runs atraves desta versao precisa saber disso.

    Nao muda nenhum valor. Para tambem alterar algo, use `criar_versao` depois.
    """
    linha = conn.execute(
        "SELECT * FROM config_version ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if linha is None:
        raise ErroConfig("nao ha config_version; rode o bootstrap primeiro")

    atual = _linha_para_versao(linha)
    if conferir_hash(atual) is None:
        raise SemDeriva(
            f"a versao {atual.id} tem hash integro; nao ha o que reancorar"
        )

    ativo = run_ativo(conn)
    if ativo is not None:
        raise ConfigCongelada(
            f"run {ativo} esta ativo; encerre-o antes de reancorar a "
            "configuracao"
        )

    _checar_teto(atual.config, settings)

    # A "mudanca" e a diferenca entre o payload GRAVADO e o reconstruido -
    # ou seja, exatamente os campos que o schema ganhou ou perdeu.
    gravado = json.loads(linha["payload_json"])
    reconstruido = atual.config.model_dump(mode="json")
    campos = sorted(set(gravado) | set(reconstruido))
    mudancas = [
        (campo, gravado.get(campo), reconstruido.get(campo))
        for campo in campos
        if gravado.get(campo) != reconstruido.get(campo)
    ]
    if not mudancas:
        # Hash divergente sem diferenca de payload significaria que a propria
        # funcao de hash mudou. E possivel, e precisa ficar registrado como
        # tal em vez de virar uma lista de mudancas vazia.
        mudancas = [("__hash__", atual.config_hash, atual.config.config_hash())]

    log.warning(
        "config.reancorada",
        extra={
            "versao_anterior": atual.id,
            "hash_anterior": atual.config_hash,
            "hash_novo": atual.config.config_hash(),
            "campos": [c for c, _, _ in mudancas],
        },
    )
    return _inserir_versao(
        conn,
        atual.config,
        author=author,
        parent_id=atual.id,
        mudancas=mudancas,
        note=note or (
            "reancoragem: o schema da configuracao mudou e o hash gravado "
            "deixou de descrever a config"
        ),
    )


def bootstrap(conn: sqlite3.Connection, settings: Settings) -> VersaoConfig:
    """Cria a versao 1 a partir dos defaults, se ainda nao existir.

    A partir dai o ambiente e IGNORADO para os parametros do experimento.
    """
    atual = versao_atual(conn)
    if atual is not None:
        return atual

    config = ExperimentConfig()
    _checar_teto(config, settings)

    campos = sorted(config.model_dump(mode="json").keys())
    mudancas = [
        (campo, None, config.model_dump(mode="json")[campo]) for campo in campos
    ]
    return _inserir_versao(
        conn,
        config,
        author="bootstrap",
        parent_id=None,
        mudancas=mudancas,
        note="versao inicial gerada dos defaults",
    )


def criar_versao(
    conn: sqlite3.Connection,
    settings: Settings,
    alteracoes: dict,
    author: str,
    note: str = "",
) -> VersaoConfig:
    """Aplica `alteracoes` sobre a versao vigente e grava uma nova.

    Levanta `ConfigCongelada`, `TetoExcedido` ou `SemMudanca`.
    """
    atual = versao_atual(conn)
    if atual is None:
        raise ErroConfig("nao ha config_version; rode o bootstrap primeiro")

    # Trava 1: congelamento durante run ativo.
    ativo = run_ativo(conn)
    if ativo is not None:
        raise ConfigCongelada(
            f"run {ativo} esta ativo; alterar configuracao agora quebraria a "
            "reprodutibilidade dele"
        )

    base = atual.config.model_dump(mode="json")
    base.update(alteracoes)
    nova = ExperimentConfig.model_validate(base)  # valida coerencia

    # Trava 2: limite inviolavel do ambiente.
    _checar_teto(nova, settings)

    mudancas = atual.config.diff(nova)
    if not mudancas:
        raise SemMudanca("a alteracao nao muda nenhum campo")

    return _inserir_versao(
        conn, nova, author=author, parent_id=atual.id, mudancas=mudancas, note=note
    )
