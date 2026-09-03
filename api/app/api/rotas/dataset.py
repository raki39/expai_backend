"""Rotas de **dataset**.

Ingestao imutavel e a separacao por finalidade (secao 8.5.1).
"""

from __future__ import annotations

import logging
from ...config import service as config_service
from ...dataset import ingest as dataset_ingest
from ...dataset import janelas as dataset_janelas
from ...dataset import loader as dataset_loader
from ...dataset import selado as dataset_selado
from ...dataset import split as dataset_split
from ...dataset.binance import BloqueioPorJurisdicao, DadosInconsistentes, ErroDeFonte
from ...dataset.ingest import DivergenciaNaReingestao, LacunasNaoAceitas
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from typing import Any
from ..modelos import PedidoIngestao
from ..comum import _conn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dataset", tags=["dataset"])


@router.get("")
def dataset_atual(request: Request) -> dict[str, Any]:
    meta = dataset_loader.dataset_vigente(_conn(request))
    if meta is None:
        return {"existe": False, "aviso": "dataset ainda nao ingerido"}
    return {"existe": True, **dataset_loader.resumo(_conn(request), meta.id)}


@router.post("/ingestao", status_code=status.HTTP_201_CREATED)
def dataset_ingerir(
    request: Request, pedido: PedidoIngestao = Body(...)
) -> dict[str, Any]:
    """Ingestao unica e idempotente da janela decidida.

    Sincrona de proposito: acontece uma vez, e quem dispara precisa ver o
    relatorio de integridade para aceita-lo ou nao. Baixar ~25 arquivos leva
    dezenas de segundos; a rota e `def`, entao roda no threadpool e nao trava
    o laco de eventos.
    """
    conn = _conn(request)
    atual = config_service.versao_atual(conn)
    if atual is None:
        raise HTTPException(status_code=503, detail="configuracao nao inicializada")

    log.info("dataset.ingestao_pedida", extra={"author": pedido.author})

    try:
        resultado = dataset_ingest.ingerir(
            conn, atual.config, aceitar_lacunas=pedido.aceitar_lacunas
        )
    except LacunasNaoAceitas as e:
        # 409, e nao 422: o pedido esta correto, o estado dos dados e que
        # exige uma decisao humana.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "erro": "lacunas_nao_aceitas",
                "mensagem": str(e),
                "relatorio_integridade": e.relatorio.como_dict(),
                "como_prosseguir": (
                    "reenvie com aceitar_lacunas=true para aceitar o relatorio, "
                    "ou ajuste data_start/data_end na configuracao"
                ),
            },
        )
    except DivergenciaNaReingestao as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except BloqueioPorJurisdicao as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "erro": "bloqueio_por_jurisdicao",
                "mensagem": str(e),
                "referencia": "ADR 0012",
            },
        )
    # DadosInconsistentes ANTES de ErroDeFonte: e subclasse dela, e na ordem
    # inversa este ramo seria inalcancavel.
    except DadosInconsistentes as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
    except ErroDeFonte as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))

    return resultado.como_dict()


@router.post("/separacao")
def criar_separacao(request: Request) -> dict[str, Any]:
    """Cria a divisao por finalidade de um dataset ja ingerido (secao 8.5.1).

    Existe por um defeito real: o dataset de PRODUCAO foi ingerido no
    incremento 1, antes de a divisao existir. Sem esta rota, o unico caminho
    seria reingerir - e reingerir devolve `ja_existia` sem tocar em nada.

    Idempotente. Nao move a fronteira do holdout: ela e a reserva carvada na
    ingestao (D11), e `split.criar` recusa a divisao se as fatias nao
    terminarem exatamente nela.
    """
    conn = _conn(request)
    meta = dataset_loader.dataset_vigente(conn)
    if meta is None:
        raise HTTPException(status_code=409, detail="nao ha dataset ingerido")

    ja_estava = dataset_loader.esta_dividido(conn, meta.id)
    dataset_ingest.garantir_separacao(conn, meta.id)
    conn.commit()
    return {
        "dataset_id": meta.id,
        "ja_estava_dividido": ja_estava,
        "conjuntos": dataset_split.resumo(conn, meta.id),
        "janelas": dataset_janelas.conferir_sem_vazamento(conn, meta.id),
    }


@router.get("/separacao")
def separacao_de_dados(request: Request) -> dict[str, Any]:
    """Os quatro conjuntos, as janelas de walk-forward e o uso do holdout.

    Existe para que a divisao seja OLHAVEL. Uma `dataset_split` que ninguem
    consulta seria uma tabela declarada e nunca lida - o padrao que este
    projeto ja registrou oito vezes, e que aqui esconderia justamente a
    barreira que a fase inteira depende de ter.

    **Nao devolve barra nenhuma**, de nenhum conjunto: so onde cada um comeca,
    quantas barras tem e quem pode le-lo. Mostrar uma barra do holdout aqui
    seria vaza-lo pela porta da frente.
    """
    conn = _conn(request)
    meta = dataset_loader.dataset_vigente(conn)
    if meta is None:
        return {"existe": False, "aviso": "dataset ainda nao ingerido"}
    return {
        "existe": True,
        **dataset_split.resumo(conn, meta.id),
        "janelas_walk_forward": [
            j.como_dict() for j in dataset_janelas.ler(conn, meta.id)
        ],
        "sem_vazamento": dataset_janelas.conferir_sem_vazamento(conn, meta.id),
        "holdout": {
            "usos": dataset_selado.usos_do_holdout(conn, meta.id),
            "regra": (
                "uso unico por hipotese, imposto por UNIQUE no banco"
                " (R28, secao 8.4)"
            ),
        },
    }
