"""Rotas de **certificação**: reexecutar a suíte sem tocar o experimento.

Domínio próprio, e não um parâmetro de `/api/a1a`, pelo mesmo argumento que
separou A1a de B4: são perguntas diferentes. `/api/a1a` **injeta** os controles
no experimento e gasta família e crédito; aqui a mesma suíte roda numa **cópia
descartável** e não gasta nada. Juntá-las numa rota só faria o efeito colateral
depender de um booleano no corpo do pedido.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, status

from ...a1a import braco as a1a_braco
from ...certificacao import alvo as alvo_mod
from ...certificacao import escopos as escopos_mod
from ...certificacao import parcelado
from ...certificacao import suite as suite_mod
from ...config import service as config_service
from ...dataset import loader as dataset_loader
from ..comum import _conn
from ..modelos import PedidoBlocoA1b, PedidoCertificacao, PedidoEtapaA1a

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/certificacao", tags=["certificacao"])


def _build() -> str | None:
    """O commit, para RASTREAMENTO - ele nao entra no alvo."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip() or None
    except Exception:  # noqa: BLE001 - rastreamento nao derruba certificacao
        return None


def _fonte(conn):
    """A fonte de dados como uniao MARCADA, do dataset vigente."""
    meta = dataset_loader.dataset_vigente(conn)
    if meta is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="sem dataset vigente: nao ha fonte de dados a certificar",
        )
    return meta, getattr(meta, "sha256", None) or getattr(meta, "hash", None)


@router.get("")
def certificacao_estado(request: Request) -> dict[str, Any]:
    """O alvo de HOJE, e o certificado dele se existir."""
    conn = _conn(request)
    atual = config_service.versao_atual(conn)
    if atual is None:
        return {"existe": False, "motivo": "configuracao nao inicializada"}
    meta, fonte_hash = _fonte(conn)
    o_alvo = alvo_mod.montar(conn, dataset_hash=fonte_hash)
    certificado = suite_mod.certificado_do_alvo(
        conn, o_alvo["alvo_de_certificacao_hash"]
    )
    composicao = escopos_mod.composicao(
        conn, o_alvo["alvo_de_certificacao_hash"]
    )
    return {
        "existe": True,
        "alvo": o_alvo,
        # O PORTAO A e a composicao dos cinco escopos, e nao o a1a sozinho.
        "portao_a": composicao,
        "certificado_a1a": certificado,
        "vigente_certificada": composicao["passa"],
        "escopo": suite_mod.ESCOPO,
        "historico": [
            dict(linha)
            for linha in conn.execute(
                "SELECT e.id, e.alvo_hash, e.iniciada_em, e.build_do_backend,"
                "       e.micros_para_copiar, e.micros_da_suite,"
                "       e.bytes_da_copia, m.passa, m.selado_em"
                "  FROM certificacao_execucao e"
                "  LEFT JOIN certificacao_manifesto m ON m.execucao_id = e.id"
                " ORDER BY e.id DESC LIMIT 20"
            )
        ],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def certificacao_rodar(
    request: Request, pedido: PedidoCertificacao = Body(...)
) -> dict[str, Any]:
    """Roda a suite numa copia descartavel e sela o manifesto.

    **Nao gasta credito, nao registra hipotese e nao move o contador do DSR** -
    as escritas acontecem na copia. Ha conferencia antes/depois das nove
    tabelas que a certificacao nao pode tocar, e qualquer diferenca RECUSA o
    certificado em vez de avisar.
    """
    conn = _conn(request)
    if config_service.run_ativo(conn) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="encerre o run ativo antes de certificar",
        )
    atual = config_service.versao_atual(conn)
    if atual is None:
        raise HTTPException(status_code=503, detail="configuracao nao inicializada")
    meta, fonte_hash = _fonte(conn)

    if pedido.escopo == escopos_mod.A1A:
        # O a1a NAO roda de uma vez pela API. Produção mediu 196 a 207 s numa
        # requisicao so - o que o ADR 0018 chama de aposta no timeout.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "a1a e parcelado (OP-1): use POST /api/certificacao/a1a, uma"
                " etapa por requisicao, sobre uma copia selada e com o estado"
                " no banco"
            ),
        )

    log.info("certificacao.pedido", extra={"author": pedido.author})
    try:
        certificado = suite_mod.executar(
            conn,
            escopo=pedido.escopo,
            dataset_id=meta.id,
            config=atual.config,
            config_version_id=atual.id,
            dataset_hash=fonte_hash,
            build_do_backend=_build(),
        )
    except suite_mod.CertificacaoRecusada as e:
        # 409, e nao 500: a certificacao FUNCIONOU e o certificado nao pode
        # existir. Um 500 diria que o servidor falhou, e o que houve foi o
        # mecanismo cumprindo o papel dele.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except (a1a_braco.SeparacaoAusente, a1a_braco.BaselineAusente) as e:
        # 409 com o motivo, e nao 500: falta uma pre-condicao, e o servidor
        # nao falhou. Producao devolveu 500 aqui em 2026-09-09 porque a `cv9`
        # nao tinha B3 - hoje a suite prepara o laboratorio na COPIA, entao
        # este ramo so alcanca o que o preparo nao resolve.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
    return {
        "escopo": pedido.escopo,
        "execucao_id": certificado.execucao_id,
        "alvo_de_certificacao_hash": certificado.alvo_hash,
        "passa": certificado.passa,
        "manifesto": certificado.manifesto,
    }


# ---------------------------------------------------------------------------
# A1b: oito blocos, disparados pelo painel — sem worker
# ---------------------------------------------------------------------------


@router.get("/a1b")
def a1b_estado_certificacao(request: Request) -> dict[str, Any]:
    """O progresso dos oito blocos para o alvo de hoje.

    É por aqui que a **retomada** acontece: o estado vive no banco, e `proximo`
    diz qual bloco falta. Um `POST` sem `indice_bloco` roda esse.
    """
    conn = _conn(request)
    atual = config_service.versao_atual(conn)
    if atual is None:
        return {"existe": False, "motivo": "configuracao nao inicializada"}
    meta, fonte_hash = _fonte(conn)
    o_alvo = alvo_mod.montar(conn, dataset_hash=fonte_hash)
    linha = conn.execute(
        "SELECT id FROM certificacao_execucao"
        " WHERE escopo = 'a1b' AND alvo_hash = ?"
        " ORDER BY id DESC LIMIT 1",
        (o_alvo["alvo_de_certificacao_hash"],),
    ).fetchone()
    if linha is None:
        return {
            "existe": True,
            "iniciada": False,
            "alvo_de_certificacao_hash": o_alvo["alvo_de_certificacao_hash"],
            "motivo": (
                "nenhuma execucao a1b para este alvo. Um POST inicia e roda o"
                " primeiro bloco"
            ),
            "blocos_esperados": escopos_mod.BLOCOS[escopos_mod.A1B],
            "execucoes_esperadas": (
                escopos_mod.BLOCOS[escopos_mod.A1B] * escopos_mod.POR_BLOCO
            ),
        }
    return {
        "existe": True,
        "iniciada": True,
        **suite_mod.estado_a1b(conn, int(linha["id"])),
    }


@router.post("/a1b", status_code=status.HTTP_200_OK)
def a1b_rodar_bloco(
    request: Request, pedido: PedidoBlocoA1b = Body(...)
) -> dict[str, Any]:
    """Roda UM bloco de 50, ou sela quando os oito estiverem concluídos.

    **Idempotente**: pedir o mesmo bloco duas vezes devolve o que já existe e
    não roda de novo — o `UNIQUE (execucao_id, escopo, indice_bloco)` é quem
    garante, e não a disciplina de quem clica.

    Sem worker (regra 1): o painel dispara os oito sequencialmente, o estado
    fica no banco e a retomada é ler `faltando`.
    """
    conn = _conn(request)
    atual = config_service.versao_atual(conn)
    if atual is None:
        raise HTTPException(status_code=503, detail="configuracao nao inicializada")
    meta, fonte_hash = _fonte(conn)

    log.info("certificacao.a1b", extra={"author": pedido.author})
    try:
        estado = suite_mod.iniciar_a1b(
            conn,
            dataset_id=meta.id,
            config=atual.config,
            config_version_id=atual.id,
            dataset_hash=fonte_hash,
            build_do_backend=_build(),
        )
        if pedido.selar:
            certificado = suite_mod.selar_a1b(
                conn, execucao_id=estado["execucao_id"], config=atual.config
            )
            return {
                "escopo": "a1b",
                "selado": True,
                "execucao_id": certificado.execucao_id,
                "alvo_de_certificacao_hash": certificado.alvo_hash,
                "passa": certificado.passa,
                "manifesto": certificado.manifesto,
            }
        indice = (
            pedido.indice_bloco
            if pedido.indice_bloco is not None
            else estado["proximo"]
        )
        if indice is None:
            return {
                "escopo": "a1b",
                "selado": False,
                "por_que": (
                    "nao falta bloco nenhum: mande `selar: true` para agregar"
                    " os oito e selar o manifesto"
                ),
                **estado,
            }
        return {"escopo": "a1b", "selado": False,
                **suite_mod.rodar_bloco(
                    conn, execucao_id=estado["execucao_id"], indice_bloco=indice
                )}
    except suite_mod.CertificacaoRecusada as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )


# ---------------------------------------------------------------------------
# A1a: oito etapas sobre UMA copia selada - sem worker (OP-1)
# ---------------------------------------------------------------------------


@router.get("/a1a")
def a1a_estado_certificacao(request: Request) -> dict[str, Any]:
    """O progresso das oito etapas para o alvo de hoje.

    É por aqui que o painel acompanha: `proxima` diz qual etapa falta,
    `em_andamento` diz se uma está rodando agora, e `abortada` diz por quê,
    quando for o caso.
    """
    conn = _conn(request)
    atual = config_service.versao_atual(conn)
    if atual is None:
        return {"existe": False, "motivo": "configuracao nao inicializada"}
    meta, fonte_hash = _fonte(conn)
    o_alvo = alvo_mod.montar(conn, dataset_hash=fonte_hash)
    execucao_id = parcelado.ultima_do_alvo(
        conn, o_alvo["alvo_de_certificacao_hash"]
    )
    if execucao_id is None:
        return {
            "existe": True,
            "iniciada": False,
            "alvo_de_certificacao_hash": o_alvo["alvo_de_certificacao_hash"],
            "plano": list(escopos_mod.PLANO_A1A),
            "motivo": (
                "nenhuma execucao a1a parcelada para este alvo. Um POST congela"
                " o alvo e o plano, sela a copia e roda a primeira etapa"
            ),
        }
    return {"existe": True, "iniciada": True, **parcelado.estado(conn, execucao_id)}


@router.post("/a1a", status_code=status.HTTP_200_OK)
def a1a_rodar_etapa(
    request: Request, pedido: PedidoEtapaA1a = Body(...)
) -> dict[str, Any]:
    """Roda a PRÓXIMA etapa, ou sela quando as oito estiverem concluídas.

    **Idempotente**: repetir o pedido devolve o que já foi feito. Uma etapa em
    andamento responde 409 e não roda de novo — o painel lê o estado e chama
    outra vez quando ela terminar. Uma cópia perdida ou fora da impressão
    esperada **aborta** a execução, com o motivo gravado.
    """
    conn = _conn(request)
    if config_service.run_ativo(conn) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="encerre o run ativo antes de certificar",
        )
    atual = config_service.versao_atual(conn)
    if atual is None:
        raise HTTPException(status_code=503, detail="configuracao nao inicializada")
    meta, fonte_hash = _fonte(conn)

    log.info("certificacao.a1a", extra={"author": pedido.author})
    try:
        estado = parcelado.iniciar(
            conn,
            dataset_id=meta.id,
            config=atual.config,
            config_version_id=atual.id,
            dataset_hash=fonte_hash,
            build_do_backend=_build(),
            nova_execucao=pedido.nova_execucao,
        )
        if estado["selado"]:
            return {"escopo": "a1a", **estado}
        if pedido.selar:
            certificado = parcelado.selar(
                conn, execucao_id=estado["execucao_id"], config=atual.config
            )
            return {
                "escopo": "a1a",
                "execucao_id": certificado.execucao_id,
                "alvo_de_certificacao_hash": certificado.alvo_hash,
                "passa": certificado.passa,
                "manifesto": certificado.manifesto,
                **{
                    k: v
                    for k, v in parcelado.estado(conn, certificado.execucao_id).items()
                    if k not in ("execucao_id", "alvo_de_certificacao_hash")
                },
            }
        if estado["proxima"] is None:
            return {
                "escopo": "a1a",
                "por_que": (
                    "nao falta etapa nenhuma: mande `selar: true` para juntar as"
                    " oito e selar o manifesto"
                ),
                **estado,
            }
        return {
            "escopo": "a1a",
            **parcelado.rodar_etapa(
                conn, execucao_id=estado["execucao_id"], config=atual.config
            ),
        }
    except (parcelado.EtapaEmAndamento, parcelado.ExecucaoAbortada) as e:
        # 409, e nao 500: o mecanismo funcionou. Em andamento e "espere";
        # abortada e "o registro diz por que, e recomecar e decisao sua".
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except suite_mod.CertificacaoRecusada as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
