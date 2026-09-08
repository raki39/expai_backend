"""O in-sample congelado. Criterio 3 do incremento 19, R74.

    "O in-sample da comparacao e lido do run CONGELADO, nunca recalculado."

## Por que uma tabela, e nao uma consulta

Uma consulta devolve o que existe **hoje**. Rodar o in-sample de novo com
sorte melhor mudaria a base de comparacao do forward **sem ninguem decidir** -
e a comparacao do forward e contra "50% do in-sample", entao mexer no
denominador e mexer no criterio.

O usuario nomeou o risco com precisao: o run congelado **nao pode ser
substituido depois por outro mais favoravel**.

## Sete campos, e nao so o `run_id`

Um id sozinho aponta para uma linha que pode ter sido reescrita ao redor. O que
amarra o resultado e o **conjunto**, e a avaliacao recusa divergencia em
qualquer um:

    run_digest ............ o resultado economico daquele run
    content_hash .......... a identidade do pre-registro
    abordagem + versao .... o mecanismo, e o esquema que o assinou
    identidade_executavel . config_hash + perfil de calibracao
    fonte ................. dataset ou snapshot, por hash
    timeframe ............. em que grade

**Recalcular em silencio com o estado atual** e o defeito que este projeto
conta dezoito vezes, e aqui ele valeria dinheiro: o forward passaria a ser
comparado contra um in-sample que nao e o que gerou a hipotese.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from ..calibracao import identidade
from ..maos_rapidas import executor
from ..store import bloco_atomico
from . import abordagem as abordagem_mod

log = logging.getLogger(__name__)

# A versao do ESQUEMA de assinatura. Nao e a versao da abordagem: e a do
# procedimento que a deriva.
#
# Ela existe porque a assinatura depende de `CAMPOS_DE_FORMA` e do catalogo
# fechado. Se um deles mudar, "cruzamento_medias com stop" passaria a nomear
# outra coisa - e o congelado sobreviveria a uma mudanca que o invalida, e
# sobreviveria calado. E o mesmo argumento pelo qual o perfil de calibracao
# cita a taxonomia dentro do proprio hash (ADR 0033).
ASSINATURA_VERSAO = 1


class JaCongelado(Exception):
    """Ja existe in-sample congelado para esta hipotese."""


class DivergenciaDoCongelado(Exception):
    """Algo mudou entre o congelado e o estado atual. NAO avaliar.

    Nao e aviso: e recusa. Avaliar com divergencia significaria comparar o
    forward contra um in-sample que nao e o que gerou a hipotese - e o numero
    sairia com aparencia normal.
    """


@dataclass(frozen=True)
class Congelado:
    hypothesis_id: int
    run_id: int
    run_digest: str
    content_hash: str
    abordagem_assinatura: str
    abordagem_versao: int
    identidade_executavel: str
    dataset_sha256: str | None
    snapshot_sha256: str | None
    timeframe: str
    metrica_primaria: str
    metrica_valor_cents: int


def _agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fonte_do_run(conn: sqlite3.Connection, run_id: int) -> tuple[str | None, str | None]:
    """O hash da fonte: dataset OU snapshot, nunca os dois.

    Os gatilhos da migracao 17 ja impoem a exclusividade do lado do `run`
    (`run_ao_vivo_exige_snapshot` e `run_historico_sem_snapshot`). Aqui a
    mesma verdade e lida, e nao reafirmada por conta propria.
    """
    linha = conn.execute(
        "SELECT r.fonte_de_dados AS fonte, r.snapshot_id AS snap"
        "  FROM run r WHERE r.id = ?",
        (run_id,),
    ).fetchone()
    if linha is None:
        raise DivergenciaDoCongelado(f"run {run_id} nao existe")

    if linha["snap"] is not None:
        s = conn.execute(
            "SELECT sha256 FROM snapshot WHERE id = ?", (int(linha["snap"]),)
        ).fetchone()
        return None, (None if s is None else str(s["sha256"]))

    d = conn.execute(
        "SELECT sha256 FROM dataset ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return (None if d is None else str(d["sha256"])), None


def congelar(
    conn: sqlite3.Connection, *, hypothesis_id: int, run_id: int,
    regra, metrica_primaria: str, metrica_valor_cents: int,
) -> Congelado:
    """Congela o in-sample. **Uma vez.** Recusa a segunda.

    Idempotente na leitura seria pior aqui do que em outros lugares: devolver o
    existente em silencio faria "congelei de novo com outro run" parecer ter
    funcionado. A segunda chamada com run DIFERENTE levanta, e a informacao
    fica no erro.
    """
    ja = ler(conn, hypothesis_id)
    if ja is not None:
        if ja.run_id != run_id:
            raise JaCongelado(
                f"a hipotese {hypothesis_id} ja tem in-sample congelado no run "
                f"{ja.run_id}, e voce pediu {run_id}. **Nao se substitui**: "
                f"trocar por um run mais favoravel depois e escolher a base de "
                f"comparacao olhando o resultado"
            )
        return ja

    assinatura = abordagem_mod.assinatura_de_regra(regra)
    abordagem_mod.registrar(
        conn,
        params=regra.params.model_dump(mode="json"),
        extras={
            "position_fraction_bps": regra.position_fraction_bps,
            "stop_loss_bps": regra.stop_loss_bps,
        },
    )

    content_hash = conn.execute(
        "SELECT content_hash FROM hypothesis WHERE id = ?", (hypothesis_id,)
    ).fetchone()
    if content_hash is None:
        raise DivergenciaDoCongelado(f"hipotese {hypothesis_id} nao existe")

    dataset_sha, snapshot_sha = _fonte_do_run(conn, run_id)
    if dataset_sha is None and snapshot_sha is None:
        raise DivergenciaDoCongelado(
            f"o run {run_id} nao aponta para dataset nem para snapshot: sem "
            f"fonte com hash, o congelado nao amarra o dado"
        )

    c = Congelado(
        hypothesis_id=hypothesis_id, run_id=run_id,
        run_digest=executor.digest_do_run(conn, run_id),
        content_hash=str(content_hash["content_hash"]),
        abordagem_assinatura=assinatura,
        abordagem_versao=ASSINATURA_VERSAO,
        identidade_executavel=identidade.do_run(conn, run_id),
        dataset_sha256=dataset_sha, snapshot_sha256=snapshot_sha,
        timeframe=regra.condicoes_validade.timeframe,
        metrica_primaria=metrica_primaria,
        metrica_valor_cents=metrica_valor_cents,
    )

    with bloco_atomico(conn, "congelar_in_sample"):
        conn.execute(
            "INSERT INTO quarentena_congelado (hypothesis_id, run_id,"
            " run_digest, content_hash, abordagem_assinatura,"
            " abordagem_versao, identidade_executavel, dataset_sha256,"
            " snapshot_sha256, timeframe, metrica_primaria,"
            " metrica_valor_cents, congelado_em)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c.hypothesis_id, c.run_id, c.run_digest, c.content_hash,
             c.abordagem_assinatura, c.abordagem_versao,
             c.identidade_executavel, c.dataset_sha256, c.snapshot_sha256,
             c.timeframe, c.metrica_primaria, c.metrica_valor_cents, _agora()),
        )
    log.info("quarentena.in_sample_congelado", extra={
        "hypothesis_id": hypothesis_id, "run_id": run_id,
        "run_digest": c.run_digest[:16],
        "identidade": c.identidade_executavel[:24],
        "abordagem": c.abordagem_assinatura,
    })
    return c


def ler(conn: sqlite3.Connection, hypothesis_id: int) -> Congelado | None:
    linha = conn.execute(
        "SELECT hypothesis_id, run_id, run_digest, content_hash,"
        "       abordagem_assinatura, abordagem_versao, identidade_executavel,"
        "       dataset_sha256, snapshot_sha256, timeframe, metrica_primaria,"
        "       metrica_valor_cents FROM quarentena_congelado"
        " WHERE hypothesis_id = ?",
        (hypothesis_id,),
    ).fetchone()
    if linha is None:
        return None
    return Congelado(
        hypothesis_id=int(linha["hypothesis_id"]),
        run_id=int(linha["run_id"]), run_digest=str(linha["run_digest"]),
        content_hash=str(linha["content_hash"]),
        abordagem_assinatura=str(linha["abordagem_assinatura"]),
        abordagem_versao=int(linha["abordagem_versao"]),
        identidade_executavel=str(linha["identidade_executavel"]),
        dataset_sha256=linha["dataset_sha256"],
        snapshot_sha256=linha["snapshot_sha256"],
        timeframe=str(linha["timeframe"]),
        metrica_primaria=str(linha["metrica_primaria"]),
        metrica_valor_cents=int(linha["metrica_valor_cents"]),
    )


def conferir(conn: sqlite3.Connection, hypothesis_id: int) -> Congelado:
    """Recusa QUALQUER divergencia entre o congelado e o estado atual.

    Sete conferencias, e todas as que divergirem aparecem na mensagem - nao
    so a primeira. Quem le um erro assim precisa saber a extensao do
    desencontro, e nao apenas onde a conferencia parou.
    """
    c = ler(conn, hypothesis_id)
    if c is None:
        raise DivergenciaDoCongelado(
            f"a hipotese {hypothesis_id} nao tem in-sample congelado. "
            f"Congele ANTES do primeiro tick do forward - depois, escolher o "
            f"run de comparacao e escolher a base olhando o resultado"
        )

    divergencias: list[str] = []

    digest_agora = executor.digest_do_run(conn, c.run_id)
    if digest_agora != c.run_digest:
        divergencias.append(
            f"run_digest: congelado {c.run_digest[:16]}, agora "
            f"{digest_agora[:16]} - os lancamentos do run mudaram"
        )

    linha = conn.execute(
        "SELECT content_hash FROM hypothesis WHERE id = ?", (hypothesis_id,)
    ).fetchone()
    if linha is None:
        divergencias.append("a hipotese nao existe mais")
    elif str(linha["content_hash"]) != c.content_hash:
        divergencias.append(
            f"content_hash: congelado {c.content_hash[:16]}, agora "
            f"{str(linha['content_hash'])[:16]} - o pre-registro e imutavel, "
            f"entao isto nao deveria ser possivel"
        )

    if c.abordagem_versao != ASSINATURA_VERSAO:
        divergencias.append(
            f"abordagem_versao: congelado {c.abordagem_versao}, esquema atual "
            f"{ASSINATURA_VERSAO} - a assinatura passou a significar outra "
            f"coisa, e o congelado nao descreve mais o mecanismo que descrevia"
        )

    identidade_agora = identidade.do_run(conn, c.run_id)
    if identidade_agora != c.identidade_executavel:
        divergencias.append(
            f"identidade_executavel: congelada {c.identidade_executavel[:24]}, "
            f"agora {identidade_agora[:24]} - config ou perfil de calibracao "
            f"mudaram, e o preco executado com eles"
        )

    dataset_sha, snapshot_sha = _fonte_do_run(conn, c.run_id)
    if (dataset_sha, snapshot_sha) != (c.dataset_sha256, c.snapshot_sha256):
        divergencias.append(
            f"fonte: congelada (dataset={c.dataset_sha256 and c.dataset_sha256[:12]}, "
            f"snapshot={c.snapshot_sha256 and c.snapshot_sha256[:12]}), agora "
            f"(dataset={dataset_sha and dataset_sha[:12]}, "
            f"snapshot={snapshot_sha and snapshot_sha[:12]})"
        )

    registro = abordagem_mod.ler(conn, c.abordagem_assinatura)
    if registro is None:
        divergencias.append(
            f"a abordagem {c.abordagem_assinatura} nao esta mais registrada"
        )

    if divergencias:
        raise DivergenciaDoCongelado(
            f"o congelado da hipotese {hypothesis_id} NAO descreve mais o "
            f"estado atual, em {len(divergencias)} ponto(s): "
            + "; ".join(divergencias)
            + ". A avaliacao para aqui - recalcular com o estado atual "
              "compararia o forward contra um in-sample que nao e o que gerou "
              "a hipotese, e o numero sairia com aparencia normal"
        )
    return c
