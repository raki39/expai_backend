"""Rotas de **validador**.

Independente do agente (secao 8.1). Maquina de estados, lote fechado com BY, DSR e creditos de teste.
"""

from __future__ import annotations

import logging
from ... import creditos as creditos_mod
from ...config import service as config_service
from ...dataset import selado as dataset_selado
from ...hipotese import registro as hipotese_registro
from ...validador import contador as validador_contador
from ...validador import estados as validador_estados
from ...validador import lote as validador_lote
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from typing import Any
from ..comum import _conn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/validador", tags=["validador"])


@router.get("")
def validador_estado(request: Request) -> dict[str, Any]:
    """A maquina de estados do conhecimento, e o contador de tentativas.

    Existe pelo mesmo motivo de `/api/separacao`: uma maquina de estados que
    ninguem consulta e uma tabela declarada e nunca lida. E porque o Portao A
    vai precisar dela - o criterio A4 exige que "nenhuma tentativa testada
    some do registro", e conferir isso pede o numero visivel.
    """
    conn = _conn(request)
    return {
        "populacao": validador_estados.populacao(conn),
        "contador": validador_contador.resumo(conn),
        "transicoes_legais": {
            de: validador_estados.transicoes_legais(conn, de)
            for de in sorted(
                {
                    l["de"]
                    for l in conn.execute("SELECT DISTINCT de FROM transicao_legal")
                }
            )
        },
        "quem_promove": validador_estados.PROMOTOR,
        "nota": (
            "estado corrente e DERIVADO da ultima transicao; `hypothesis` e"
            " imutavel e nao tem coluna de estado (secao 8.1, regra 16)"
        ),
    }


@router.get("/hipotese/{hypothesis_id}")
def validador_hipotese(request: Request, hypothesis_id: int) -> dict[str, Any]:
    """O caminho inteiro de uma hipotese. E ele que prova que nada foi pulado."""
    conn = _conn(request)
    hip = hipotese_registro.por_id(conn, hypothesis_id)
    if hip is None:
        raise HTTPException(status_code=404, detail="hipotese nao existe")
    estado = validador_estados.atual(conn, hypothesis_id)
    return {
        "pre_registro": hip,
        "estado": estado.como_dict() if estado else None,
        "historico": validador_estados.historico(conn, hypothesis_id),
        "holdout_consumido": dataset_selado.ja_consumiu(conn, hypothesis_id),
    }


@router.get("/lote")
def lote_fechado(request: Request) -> dict[str, Any]:
    """O procedimento de lote sobre a familia fechada, e o DSR (secao 8.6).

    NAO promove nada: e parecer sobre o conjunto, e por isso da para olhar
    quantas vezes quiser antes de decidir. Quem move cada hipotese e
    `promocao`, uma a uma, com a evidencia dela.
    """
    conn = _conn(request)
    vigente = config_service.versao_atual(conn)
    if vigente is None:
        raise HTTPException(status_code=409, detail="nao ha config vigente")
    cfg = vigente.config
    return {
        "config_version_id": vigente.id,
        "parametros": {
            "familia_max_hipoteses": cfg.familia_max_hipoteses,
            "fdr_procedimento": cfg.fdr_procedimento,
            "fdr_alvo_bps": cfg.fdr_alvo_bps,
            "dsr_minimo_milesimos": cfg.dsr_minimo_milesimos,
        },
        "fechamento": validador_lote.fechar(
            conn,
            config_version_id=vigente.id,
            familia_max=cfg.familia_max_hipoteses,
            procedimento=cfg.fdr_procedimento,
            alfa_bps=cfg.fdr_alvo_bps,
            dsr_minimo_milesimos=cfg.dsr_minimo_milesimos,
        ).como_dict(),
    }


@router.get("/creditos")
def creditos_de_teste(request: Request) -> dict[str, Any]:
    """Saldo por braco e os quatro numeros de calibracao da secao 8.6.1.

    O saldo e DERIVADO da view: nao existe coluna de saldo que pudesse
    divergir do consumo (regra 16).
    """
    return creditos_mod.calibracao(_conn(request))
