"""Rotas HTTP do incremento 0.

Sem logica de negocio (secao 10.2.1). As rotas leem o banco, delegam as
travas ao `config.service` e devolvem JSON.

Nenhuma rota expoe segredo, nem parcialmente (secao 10.2.4).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..config import service as config_service
from ..config.service import (
    ConfigCongelada,
    SchemaDivergente,
    SemDeriva,
    SemMudanca,
    TetoExcedido,
)
from ..dataset import ingest as dataset_ingest
from ..dataset import loader as dataset_loader
from ..dataset.binance import BloqueioPorJurisdicao, DadosInconsistentes, ErroDeFonte
from ..dataset.ingest import DivergenciaNaReingestao, LacunasNaoAceitas
from ..ledger import livro
from ..maos_rapidas import baselines
from ..simulador import execucao as simulador
from ..security import exigir_token_de_servico
from ..settings import Settings
from ..store import versao_schema, volume_gravavel, volume_montado

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(exigir_token_de_servico)])


def _conn(request: Request) -> sqlite3.Connection:
    return request.app.state.conn


def _settings(request: Request) -> Settings:
    return request.app.state.settings


# ------------------------------------------------------------------ health


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    """Estado do substrato. Sem segredo, nem parcial."""
    settings = _settings(request)
    conn = _conn(request)
    atual = config_service.versao_atual(conn)

    return {
        "status": "ok",
        "app_env": settings.app_env,
        "build": request.app.state.build_id,
        "db_path": str(settings.db_path),
        "db_path_absoluto": settings.db_path.is_absolute(),
        # Escrever com sucesso NAO prova persistencia (o /data pode ser da
        # propria imagem). As duas linhas dizem coisas diferentes.
        "volume_gravavel": volume_gravavel(settings.db_path.parent),
        "volume_montado": volume_montado(settings.db_path.parent),
        "data_dir": str(settings.data_dir),
        "schema_version": versao_schema(conn),
        "config_version": atual.id if atual else None,
        "config_hash": atual.config_hash if atual else None,
        # O hash gravado ainda descreve esta configuracao? Se nao, ele parou
        # de identificar o que diz identificar - e o painel precisa gritar.
        "config_hash_confere": (
            config_service.conferir_hash(atual) is None if atual else None
        ),
        "run_ativo": config_service.run_ativo(conn),
        # Nao e segredo: e configuracao publica de politica de origem.
        "cors_allowed_origins": settings.cors_origins,
        # Presenca das credenciais, nunca o valor (secao 10.2.4).
        "credenciais_configuradas": {
            "anthropic": bool(settings.anthropic_api_key.get_secret_value()),
            "openai": bool(settings.openai_api_key.get_secret_value()),
        },
        "fase": "0A",
        "aviso": (
            "Fase 0A. Nenhuma conclusao estatistica. Nenhum conhecimento "
            "promovido."
        ),
    }


# ------------------------------------------------------------ configuracao


class AlteracaoConfig(BaseModel):
    """Pedido de nova versao de configuracao."""

    author: str = Field(min_length=1, max_length=120)
    changes: dict[str, Any] = Field(min_length=1)
    note: str = Field(default="", max_length=500)


@router.get("/config")
def config_atual(request: Request) -> dict[str, Any]:
    atual = config_service.versao_atual(_conn(request))
    if atual is None:
        raise HTTPException(status_code=503, detail="configuracao nao inicializada")
    return {
        "version_id": atual.id,
        "created_at": atual.created_at,
        "author": atual.author,
        "parent_version_id": atual.parent_version_id,
        "config_hash": atual.config_hash,
        "material": atual.material,
        "note": atual.note,
        "config": atual.config.model_dump(mode="json"),
        "congelada": config_service.run_ativo(_conn(request)) is not None,
    }


@router.get("/config/history")
def config_historico(request: Request, limite: int = 50) -> dict[str, Any]:
    """Autor, data, valor anterior e valor novo (secao 10.2.3)."""
    return {"versions": config_service.historico(_conn(request), limite=limite)}


@router.post("/config", status_code=status.HTTP_201_CREATED)
def config_nova_versao(
    request: Request, pedido: AlteracaoConfig = Body(...)
) -> dict[str, Any]:
    try:
        nova = config_service.criar_versao(
            _conn(request),
            _settings(request),
            alteracoes=pedido.changes,
            author=pedido.author,
            note=pedido.note,
        )
    except ConfigCongelada as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except TetoExcedido as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e))
    except SemMudanca as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e))

    return {
        "version_id": nova.id,
        "config_hash": nova.config_hash,
        "material": nova.material,
        # O painel usa isto para avisar alto (secao 10.2.3).
        "aviso": (
            "Alteracao material: invalida comparacao com runs anteriores."
            if nova.material
            else ""
        ),
    }


class PedidoReancoragem(BaseModel):
    author: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=500)


@router.post("/config/reancorar", status_code=status.HTTP_201_CREATED)
def config_reancorar(
    request: Request, pedido: PedidoReancoragem = Body(...)
) -> dict[str, Any]:
    """Regrava a config vigente sob o hash correto, apos mudanca de schema.

    Nao altera valor nenhum. Existe porque `criar_versao` devolveria
    `SemMudanca` neste caso - a configuracao efetiva nao mudou, so o hash.
    """
    try:
        nova = config_service.reancorar(
            _conn(request), _settings(request),
            author=pedido.author, note=pedido.note,
        )
    except SemDeriva as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ConfigCongelada as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except TetoExcedido as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
    return {
        "version_id": nova.id,
        "config_hash": nova.config_hash,
        "material": nova.material,
        "aviso": (
            "Configuracao reancorada. O schema mudou: toda comparacao que "
            "atravesse esta versao e invalida."
        ),
    }


# ----------------------------------------------------------------- dataset


class PedidoIngestao(BaseModel):
    """Disparo da ingestao unica.

    `aceitar_lacunas` e o "relatorio aceito" do criterio 3: a decisao de
    prosseguir com serie incompleta e de uma pessoa, tem autor e fica no log.
    Por isso o default e recusar.
    """

    author: str = Field(min_length=1, max_length=120)
    aceitar_lacunas: bool = False


@router.get("/dataset")
def dataset_atual(request: Request) -> dict[str, Any]:
    meta = dataset_loader.dataset_vigente(_conn(request))
    if meta is None:
        return {"existe": False, "aviso": "dataset ainda nao ingerido"}
    return {"existe": True, **dataset_loader.resumo(_conn(request), meta.id)}


@router.post("/dataset/ingest", status_code=status.HTTP_201_CREATED)
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


# ------------------------------------------------------------------ ledger


class PedidoRun(BaseModel):
    author: str = Field(min_length=1, max_length=120)


@router.get("/ledger")
def ledger_estado(request: Request) -> dict[str, Any]:
    """Saldos derivados e o resultado das conferencias.

    As conferencias vao junto do saldo de proposito: um numero de dinheiro sem
    a prova de que o livro fecha e so um numero.
    """
    conn = _conn(request)
    ativo = config_service.run_ativo(conn)
    violacoes = livro.conferir_partidas_dobradas(conn)
    divergencias = livro.reconciliar(conn)
    vinculos = livro.conferir_vinculo_inferencia(conn)

    return {
        "run_ativo": ativo,
        # Carteira DO RUN ativo: contas sao globais e somam a historia toda,
        # o que responderia outra pergunta.
        "carteira": livro.carteira(conn, run_id=ativo),
        "contas": livro.saldos(conn, run_id=ativo) if ativo else livro.saldos(conn),
        "conferencias": {
            "partidas_dobradas_ok": not violacoes,
            "violacoes": violacoes,
            "saldo_reconciliado_ok": not divergencias,
            "divergencias": divergencias,
            "vinculos_ok": not any(vinculos.values()),
            "vinculos": vinculos,
            "sem_ponto_flutuante": livro.colunas_em_ponto_flutuante(conn) == [],
        },
        "eventos": conn.execute(
            "SELECT COUNT(*) AS n FROM agent_event"
        ).fetchone()["n"],
        "transacoes": conn.execute(
            "SELECT COUNT(*) AS n FROM ledger_transaction"
        ).fetchone()["n"],
    }


@router.get("/ledger/transacoes")
def ledger_transacoes(request: Request, limite: int = 50) -> dict[str, Any]:
    """Historico. Estorno e original aparecem os dois - nada e apagado."""
    linhas = _conn(request).execute(
        """
        SELECT t.id, t.kind, t.occurred_at, t.posted_at, t.run_id,
               t.fx_rate_micro, t.fx_rate_date, t.agent_event_id,
               t.reverses_transaction_id, t.memo,
               (SELECT COUNT(*) FROM ledger_entry WHERE transaction_id = t.id)
                   AS lancamentos
        FROM ledger_transaction t
        ORDER BY t.id DESC LIMIT ?
        """,
        (limite,),
    ).fetchall()
    return {"total": len(linhas), "items": [dict(l) for l in linhas]}


@router.post("/run", status_code=status.HTTP_201_CREATED)
def run_abrir(request: Request, pedido: PedidoRun = Body(...)) -> dict[str, Any]:
    """Abre um run e credita o capital semente como lancamento (criterio 7)."""
    conn = _conn(request)
    if config_service.run_ativo(conn) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ja existe run ativo; encerre antes de abrir outro",
        )
    atual = config_service.versao_atual(conn)
    if atual is None:
        raise HTTPException(status_code=503, detail="configuracao nao inicializada")

    # O hash e a IDENTIDADE da config do run. Se ele nao descreve mais a
    # configuracao, nenhum resultado produzido aqui pode ser comparado.
    try:
        config_service.exigir_hash_integro(conn)
    except SchemaDivergente as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    log.info("run.abertura_pedida", extra={"author": pedido.author})
    run_id, tx_id = livro.abrir_run(
        conn,
        config_version_id=atual.id,
        seed_capital_usd_cents=atual.config.seed_capital_usd_cents,
    )
    return {
        "run_id": run_id,
        "transaction_id": tx_id,
        "config_version_id": atual.id,
        "seed_capital_usd_cents": atual.config.seed_capital_usd_cents,
        "aviso": "a configuracao esta congelada enquanto o run estiver ativo",
    }


@router.post("/run/{run_id}/encerrar")
def run_encerrar(
    request: Request, run_id: int, estado: str = Body(embed=True)
) -> dict[str, Any]:
    try:
        livro.encerrar_run(_conn(request), run_id, estado)
    except livro.TransacaoInvalida as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
    return {"run_id": run_id, "state": estado}


# -------------------------------------------------------------- simulador


@router.get("/simulador")
def simulador_estado(request: Request) -> dict[str, Any]:
    """Agregado das execucoes do run ativo.

    A fidelidade e as condicoes de validade vao SEMPRE junto do numero
    (criterio 5, secao 8.4.1.1). Um custo agregado sem o nivel de fidelidade
    que o produziu convida a conclusao que a Fase 0A nao pode sustentar.
    """
    conn = _conn(request)
    ativo = config_service.run_ativo(conn)
    # Sem run ativo, mostra o ULTIMO que teve execucao. A tela ficava zerada
    # com milhares de execucoes gravadas, porque a comparacao encerra os runs
    # que ela abre - e "nao ha run ativo" nao e a mesma coisa que "nao houve
    # execucao nenhuma".
    alvo = ativo
    if alvo is None:
        linha = conn.execute(
            "SELECT MAX(run_id) AS run_id FROM execution"
        ).fetchone()
        alvo = int(linha["run_id"]) if linha and linha["run_id"] else None
    if alvo is None:
        return {
            "run_ativo": None,
            "run_exibido": None,
            "condicoes_validade": simulador.CONDICOES_DE_VALIDADE,
        }
    return {
        "run_ativo": ativo,
        "run_exibido": alvo,
        "encerrado": ativo is None,
        **simulador.resumo(conn, alvo),
    }


@router.get("/execucoes")
def execucoes_listar(request: Request, limite: int = 50) -> dict[str, Any]:
    linhas = _conn(request).execute(
        "SELECT id, run_id, side, decision_bar_ms, execution_bar_ms,"
        " quantity_sats, price_ref, price_exec, notional_ref_cents, fee_cents,"
        " spread_cents, slippage_cents, penalty_cents, fidelity_level,"
        " ledger_transaction_id FROM execution ORDER BY id DESC LIMIT ?",
        (limite,),
    ).fetchall()
    return {
        "total": len(linhas),
        "items": [dict(l) for l in linhas],
        "condicoes_validade": simulador.CONDICOES_DE_VALIDADE,
    }


# ------------------------------------------------------------- comparacao


class PedidoComparacao(BaseModel):
    author: str = Field(min_length=1, max_length=120)
    # Semente diferente reexecuta legitimamente com o MESMO config_hash
    # (secao 14.4.1). `None` usa a da configuracao.
    semente: int | None = None


@router.get("/comparacao")
def comparacao_atual(request: Request) -> dict[str, Any]:
    return baselines.resumo_comparacao(_conn(request))


@router.post("/comparacao", status_code=status.HTTP_201_CREATED)
def comparacao_rodar(
    request: Request, pedido: PedidoComparacao = Body(...)
) -> dict[str, Any]:
    """Roda B1, B2 e B3 sobre a mesma janela, mesmo simulador, mesmo custo.

    Nenhum LLM e envolvido. Se o encanamento nao fecha sem o modelo, o
    problema nao e o modelo.
    """
    conn = _conn(request)
    if config_service.run_ativo(conn) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="encerre o run ativo antes de rodar a comparacao",
        )
    try:
        config_service.exigir_hash_integro(conn)
    except SchemaDivergente as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    atual = config_service.versao_atual(conn)
    if atual is None:
        raise HTTPException(status_code=503, detail="configuracao nao inicializada")
    meta = dataset_loader.dataset_vigente(conn)
    if meta is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ingira o dataset antes de rodar a comparacao",
        )

    semente = pedido.semente if pedido.semente is not None else atual.config.default_seed
    log.info("comparacao.pedida",
             extra={"author": pedido.author, "semente": semente})
    try:
        return baselines.rodar_comparacao(
            conn, dataset_id=meta.id, config=atual.config,
            config_version_id=atual.id, semente=semente,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )


# --------------------------------------------------------------- sentinela


@router.post("/sentinel", status_code=status.HTTP_201_CREATED)
def sentinela_criar(request: Request, label: str = Body(embed=True)) -> dict[str, Any]:
    """Grava um marcador para provar que o volume persiste entre deploys."""
    conn = _conn(request)
    cur = conn.execute(
        "INSERT INTO sentinel (label, created_at) VALUES (?, datetime('now'))",
        (label,),
    )
    log.info("sentinel.created", extra={"sentinel_id": cur.lastrowid})
    return {"id": int(cur.lastrowid), "label": label}


@router.get("/sentinel")
def sentinela_listar(request: Request) -> dict[str, Any]:
    linhas = _conn(request).execute(
        "SELECT id, label, created_at FROM sentinel ORDER BY id DESC LIMIT 50"
    ).fetchall()
    return {
        "total": len(linhas),
        "items": [dict(linha) for linha in linhas],
    }
