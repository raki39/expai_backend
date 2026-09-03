"""Rotas HTTP do incremento 0.

Sem logica de negocio (secao 10.2.1). As rotas leem o banco, delegam as
travas ao `config.service` e devolvem JSON.

Nenhuma rota expoe segredo, nem parcialmente (secao 10.2.4).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi import Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

# O cerebro lento e importado aqui, mas nenhum SDK de provedor vem junto:
# os adaptadores carregam o proprio SDK dentro da funcao que chama. Subir a
# API nao carrega provedor nenhum, e ha teste que confere a variacao de
# `sys.modules` em vez do estado absoluto dela.
from ..cerebro import avaliacao as cerebro_avaliacao
from ..cerebro import cache as cerebro_cache
from ..cerebro import ciclo as cerebro_ciclo
from ..cerebro import propostas as propostas_de_regra
from ..config import service as config_service
from ..config.service import (
    ConfigCongelada,
    SchemaDivergente,
    SemDeriva,
    SemMudanca,
    TetoExcedido,
)
from ..dataset import ingest as dataset_ingest
from ..dataset import janelas as dataset_janelas
from ..dataset import loader as dataset_loader
from ..dataset import selado as dataset_selado
from ..dataset import split as dataset_split
from ..dataset.binance import BloqueioPorJurisdicao, DadosInconsistentes, ErroDeFonte
from ..dataset.ingest import DivergenciaNaReingestao, LacunasNaoAceitas
from ..hipotese import registro as hipotese_registro
from ..ledger import livro
from ..maos_rapidas import baselines
from ..maos_rapidas import executor as maos_executor
from ..maos_rapidas import curva as curva_de_patrimonio
from ..relatorio import montar as relatorio_montar
from ..relatorio import reprodutibilidade as relatorio_reprodutibilidade
from ..relatorio import texto as relatorio_texto
from ..relatorio import vinculo as relatorio_vinculo
from ..simulador import execucao as simulador
from .. import creditos as creditos_mod
from ..validador import contador as validador_contador
from ..validador import lote as validador_lote
from ..validador import estados as validador_estados
from ..security import exigir_token_de_servico
from ..settings import Settings
from ..store import (
    conexao_do_thread,
    versao_schema,
    volume_gravavel,
    volume_montado,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(exigir_token_de_servico)])


def _conn(request: Request) -> sqlite3.Connection:
    # A conexao DESTA thread, e nao uma unica compartilhada pelo processo.
    # O painel faz catorze chamadas em paralelo, e o threadpool do FastAPI
    # as espalha por threads diferentes: uma conexao so entre elas produzia
    # `sqlite3.InterfaceError` em cerca de 1,4% das requisicoes.
    return conexao_do_thread(request.app.state.settings.db_path)


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
        # A fase vem daqui e de nenhum outro lugar. Ficou em "0A" por um
        # commit depois de a 0B abrir, que e a nona vez que um valor deste
        # projeto para de descrever o que diz - e este seria dos piores, porque
        # o aviso que o acompanha e sobre o que pode ser afirmado.
        "fase": "0B",
        "aviso": (
            "Fase 0B. O Portao A e o produto da fase; conclusao estatistica "
            "so pelo validador independente, e 'inconclusivo' nunca vira "
            "'sucesso'. Nenhuma aprovacao autoriza capital real."
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
        # Campos de catalogo em que o banco discorda do catalogo verificado.
        # Nao e "o banco esta errado": o banco e a autoridade (regra 20). E a
        # constatacao de que existe diferenca, para que ela seja resolvida por
        # ato humano registrado em vez de passar despercebida.
        "catalogo_desatualizado": config_service.catalogo_desatualizado(
            _conn(request)
        ),
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


@router.post("/config/catalogo", status_code=status.HTTP_201_CREATED)
def config_adotar_catalogo(
    request: Request, pedido: PedidoReancoragem = Body(...)
) -> dict[str, Any]:
    """Adota o catalogo de provedores verificado: tiers e tabela de precos.

    Toca APENAS esses campos, e cria uma `config_version` como qualquer outra
    alteracao, com autor, data, valor anterior e novo. E material: preco
    alimenta o teto, e o teto decide quantas reflexoes cabem num run.
    """
    try:
        nova = config_service.adotar_catalogo_de_provedores(
            _conn(request), _settings(request),
            author=pedido.author, note=pedido.note,
        )
    except ConfigCongelada as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except SemMudanca as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except TetoExcedido as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
    return {
        "version_id": nova.id,
        "config_hash": nova.config_hash,
        "material": nova.material,
        "aviso": (
            "Alteracao material: invalida comparacao com runs anteriores."
            if nova.material else ""
        ),
    }


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


@router.get("/validador")
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


@router.get("/validador/hipotese/{hypothesis_id}")
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


@router.post("/dataset/separacao")
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
        # Sem run ativo, o que vem e o LIVRO INTEIRO - a soma de todos os runs
        # que ja existiram. E um numero legitimo ("quanto ja passou por esta
        # conta"), mas responde outra pergunta que "quanto este run tem", e o
        # painel precisa dizer qual das duas esta mostrando. Rotulo errado
        # aqui faz somar duas comparacoes e ler o total como carteira.
        "escopo": "run" if ativo else "livro_inteiro",
        "runs_somados": (
            1 if ativo
            else conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"]
        ),
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
        atual = config_service.versao_atual(conn)
        return {
            "run_ativo": None,
            "run_exibido": None,
            "condicoes_validade": (
                simulador.condicoes_de_validade(atual.config) if atual else ""
            ),
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
    atual = config_service.versao_atual(_conn(request))
    return {
        "total": len(linhas),
        "items": [dict(l) for l in linhas],
        "condicoes_validade": (
            simulador.condicoes_de_validade(atual.config) if atual else ""
        ),
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


@router.get("/curva")
def curva(request: Request, pontos: int = curva_de_patrimonio.PONTOS_PADRAO) -> dict[str, Any]:
    """Curva de patrimonio do agente e dos baselines, na MESMA escala (§6.2).

    Derivada das execucoes gravadas. Nao ha tabela de curva: uma serie
    persistida ao lado do ledger seria segunda fonte de verdade sobre
    dinheiro (regra 16).

    B1 nao tem curva e nunca vai ter: sao mil repeticoes das quais so o
    resultado final e guardado. Ele entra como FAIXA do resultado final, e
    desenha-lo atravessando o tempo afirmaria que mil caminhos foram
    acompanhados - um grafico que mente sem que nenhum numero esteja errado.
    """
    conn = _conn(request)
    meta = dataset_loader.dataset_vigente(conn)
    if meta is None:
        return {"existe": False, "motivo": "dataset ainda nao ingerido"}

    runs: dict[str, int] = {}
    for marcador, chave in (("baseline-B2", "B2"), ("baseline-B3", "B3")):
        linha = conn.execute(
            "SELECT MAX(id) AS id FROM run WHERE agent_id = ?", (marcador,)
        ).fetchone()
        if linha and linha["id"]:
            runs[chave] = int(linha["id"])

    # O run do agente e derivado: e o ultimo que tem evento cognitivo. Nao ha
    # coluna "e do agente" para ficar desatualizada.
    agente = conn.execute(
        "SELECT MAX(run_id) AS id FROM agent_event WHERE run_id IS NOT NULL"
    ).fetchone()
    if agente and agente["id"]:
        runs["agente"] = int(agente["id"])

    if not runs:
        return {"existe": False, "motivo": "nenhum run para desenhar"}

    atual = config_service.versao_atual(conn)
    curvas = curva_de_patrimonio.curvas_da_comparacao(
        conn, dataset_id=meta.id, config=atual.config, runs=runs,
        pontos=max(50, min(pontos, 2_000)),
    )
    comparacao = baselines.resumo_comparacao(conn)

    finais = {
        nome: (c[-1]["patrimonio_cents"] if c else None)
        for nome, c in curvas.items()
    }
    # Regra 14: desempenho SEMPRE como excesso sobre baseline. O absoluto
    # responde "quanto sobrou", que nao e a pergunta do experimento.
    excessos = {}
    if finais.get("agente") is not None:
        for base in ("B2", "B3"):
            if finais.get(base) is not None:
                excessos[f"agente_sobre_{base}"] = (
                    curva_de_patrimonio.excesso_sobre_baseline_cents(
                        finais["agente"], finais[base]
                    )
                )
        # Contra o B1 CASADO com o giro do agente, e nao contra o da
        # comparacao, que e casado com o B3 (D19).
        casado = baselines.b1_do_agente(conn)
        if casado:
            excessos["agente_sobre_B1_casado_p50"] = (
                curva_de_patrimonio.excesso_sobre_baseline_cents(
                    finais["agente"], casado["p50"]
                )
            )

    return {
        "existe": True,
        "curvas": curvas,
        "finais_cents": finais,
        "excesso_cents": excessos,
        # A faixa de B1 e do RESULTADO FINAL, e nao um caminho. A do agente
        # e a casada com o giro dele; a da comparacao e casada com o B3.
        "b1_faixa_final": baselines.b1_do_agente(conn) or comparacao.get("B1"),
        "b1_faixa_da_comparacao": comparacao.get("B1"),
        "runs": runs,
        "config_version_vigente": comparacao.get("config_version_vigente"),
        "sob_a_config_vigente": comparacao.get("sob_a_config_vigente"),
        "aviso": (
            "Entre uma compra e a venda seguinte a curva marca a posicao ao"
            " fechamento da barra: e otimista no meio e exata nas pontas,"
            " onde a posicao esta zerada. Vender pagaria custos que a"
            " marcacao nao desconta."
        ),
    }


# ------------------------------------------------------------ cerebro lento


class PedidoAgente(BaseModel):
    author: str = Field(min_length=1, max_length=120)


@router.get("/agente")
def agente_estado(request: Request) -> dict[str, Any]:
    """O ultimo run do agente: caminho percorrido, propostas e gasto.

    Um run do agente e um run que tem `agent_event` - derivado, e nao uma
    coluna nova: quem tem evento cognitivo passou pelo cerebro, e quem nao
    tem, nao passou. Nao ha estado para ficar desatualizado.
    """
    conn = _conn(request)
    linha = conn.execute(
        "SELECT MAX(run_id) AS run_id FROM agent_event WHERE run_id IS NOT NULL"
    ).fetchone()
    run_id = linha["run_id"] if linha else None
    if run_id is None:
        return {"run_id": None, "caminho": [], "propostas": [], "gasto": None}

    carteira = livro.carteira(conn, run_id=int(run_id))
    return {
        "run_id": int(run_id),
        # O resultado economico do run, do ledger. Sem ele o relatorio diz
        # quantas operacoes houve e nao diz se sobrou dinheiro - que e a unica
        # pergunta que a comparacao com os baselines responde.
        "patrimonio_final_cents": simulador.caixa_cents(conn, int(run_id)),
        # A mesma definicao que o relatorio e a comparacao usam. Era
        # `COUNT(*) / 2` aqui e contagem de compras la: iguais enquanto toda
        # compra fecha, divergentes no run que termina comprado.
        "operacoes": maos_executor.idas_e_voltas(conn, int(run_id)),
        "custos_cents": {
            "execucao_total": carteira["simulado_usd"]["custo_execucao_minor"],
            "reflexao_total": carteira["simulado_usd"]["tesouraria_minor"],
            "posicao_aberta": carteira["simulado_usd"]["posicao_btc_minor"],
        },
        "config_version_id": conn.execute(
            "SELECT config_version_id FROM run WHERE id = ?", (int(run_id),)
        ).fetchone()["config_version_id"],
        # O controle do acaso casado com o giro DESTE run (D19). O B1 da
        # comparacao e casado com o B3 e tem outro giro - comparar o agente
        # com aquele mediria giro em vez de escolha de momento.
        "b1_casado": baselines.b1_do_agente(conn),
        # Onde o resultado caiu na distribuicao do acaso. Vem da api porque
        # classificar e decidir, e decidir sobre o experimento nao acontece no
        # painel (regra 19). Ele comparava com p5/p50/p95 na tela - e a mesma
        # regua ficava escrita em dois lugares, livre para divergir.
        "faixa": cerebro_avaliacao.faixa_contra_o_acaso(
            simulador.caixa_cents(conn, int(run_id)), baselines.b1_do_agente(conn)
        ),
        "caminho": cerebro_ciclo.caminho_percorrido(conn, int(run_id)),
        "propostas": propostas_de_regra.do_run(conn, int(run_id)),
        "regra_ativa": propostas_de_regra.regra_ativa(conn, int(run_id)),
        "gasto": livro.gasto_com_reflexao(conn, int(run_id)),
        "sobreposicao_amostral": propostas_de_regra.sobreposicao_amostral(
            conn, int(run_id)
        ),
        "condicoes_validade": simulador.condicoes_do_run(conn, int(run_id)),
        # Quantas vezes o cerebro falou neste run. O painel precisa disto
        # para nao confundir ausencia com zero: por D23, ZERO reflexoes
        # significa que o agente E o B3, que e uma afirmacao forte - e nao
        # pode ser o que a tela mostra so porque o campo nao veio.
        "reflexoes": conn.execute(
            "SELECT COUNT(*) AS n FROM agent_event"
            " WHERE run_id = ? AND provider IS NOT NULL",
            (int(run_id),),
        ).fetchone()["n"],
        "cache_de_respostas": cerebro_cache.tamanho(conn),
        "arredondamento_do_custo_ok": (
            livro.conferir_arredondamento_do_custo(conn) == []
        ),
    }


@router.post("/agente", status_code=status.HTTP_201_CREATED)
def agente_rodar(
    request: Request, pedido: PedidoAgente = Body(...)
) -> dict[str, Any]:
    """Fecha o ciclo: observa, reflete, propoe regra, executa, contabiliza.

    E a unica rota do projeto que pode gastar dinheiro de verdade. Os tetos
    ficam dentro do ciclo, e nao aqui: uma trava no caminho HTTP protege
    contra o painel, nao contra o programa (secao 12.1).
    """
    conn = _conn(request)
    if config_service.run_ativo(conn) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="encerre o run ativo antes de rodar o agente",
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
            detail="ingira o dataset antes de rodar o agente",
        )

    log.info("agente.pedido", extra={"author": pedido.author})
    try:
        resultado = cerebro_ciclo.rodar(
            conn,
            dataset_id=meta.id,
            config=atual.config,
            config_version_id=atual.id,
            settings=_settings(request),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
    return resultado.como_dict()


class PedidoProva(BaseModel):
    """Semente da prova de reprodutibilidade.

    Opcional: ausente usa `default_seed` da config. E entrada do run, e nao
    campo do `config_hash` - e o que torna enunciavel a segunda metade da
    prova, "digest diferente com config_hash igual".
    """

    semente: int | None = None

# --------------------------------------------------------------- fechamento
#
# O relatorio da 0A. Uma funcao so o monta - a mesma que o `python -m
# app.relatorio` usa para escrever o arquivo. Dois geradores independentes
# poderiam discordar, e este e o documento em que a 0A responde a propria
# pergunta: o pior lugar do sistema para duas versoes da verdade.


@router.get("/relatorio")
def relatorio(request: Request, run_id: int | None = None) -> dict[str, Any]:
    """O relatorio de fechamento em JSON. `run_id` ausente usa o ultimo run."""
    return relatorio_montar.montar(_conn(request), run_id)


@router.get("/relatorio/markdown", response_class=PlainTextResponse)
def relatorio_markdown(request: Request, run_id: int | None = None) -> str:
    """O mesmo relatorio, para humano. So formata; nao calcula nada."""
    return relatorio_texto.markdown(
        relatorio_montar.montar(_conn(request), run_id)
    )


@router.get("/vinculo/execucao/{execution_id}")
def vinculo_da_execucao(request: Request, execution_id: int) -> dict[str, Any]:
    """De uma execucao qualquer ao evento cognitivo que a autorizou (R25.2)."""
    return relatorio_vinculo.da_execucao_ao_evento(_conn(request), execution_id)


@router.get("/vinculo/evento/{event_id}")
def vinculo_do_evento(request: Request, event_id: int) -> dict[str, Any]:
    """Da decisao ao custo, a regra, as execucoes e ao resultado (R25.2)."""
    return relatorio_vinculo.do_evento_ao_resultado(_conn(request), event_id)


@router.post("/reprodutibilidade", status_code=status.HTTP_201_CREATED)
def reprodutibilidade_provar(
    request: Request, pedido: PedidoProva = Body(default=PedidoProva())
) -> dict[str, Any]:
    """Roda a prova dos tres digests. Cria tres runs proprios, sem LLM.

    POST porque escreve: tres runs de B1 pelo ledger. Nenhuma chamada a
    provedor acontece aqui - a prova de reprodutibilidade nao pode custar
    dinheiro nem depender de o cache estar quente.
    """
    conn = _conn(request)
    config_service.exigir_hash_integro(conn)
    versao = config_service.versao_atual(conn)
    if versao is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "nenhuma configuracao gravada")

    meta = dataset_loader.dataset_vigente(conn)
    if meta is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "dataset ainda nao ingerido")

    ativo = config_service.run_ativo(conn)
    if ativo is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"run {ativo} esta ativo; encerre antes"
        )

    try:
        return relatorio_reprodutibilidade.provar(
            conn,
            dataset_id=meta.id,
            config=versao.config,
            config_version_id=versao.id,
            semente=pedido.semente,
        )
    except ValueError as erro:
        raise HTTPException(status.HTTP_409_CONFLICT, str(erro)) from erro


# ---------------------------------------------------------------- exportar
#
# Um arquivo com tudo o que o painel mostra, para sair da tela e virar anexo.
#
# **Nao existe leitor novo aqui.** Esta rota chama as PROPRIAS funcoes de rota
# que servem cada tela. Reimplementar as consultas produziria um segundo jeito
# de responder as mesmas perguntas - e no dia em que os dois discordassem, o
# arquivo exportado seria a versao que alguem leva para analisar sem ter como
# conferir. A regra 16 vale aqui como vale para saldo.
#
# Por isso tambem nao ha calculo: se um numero nao esta numa tela, ele nao
# esta no export.




@router.get("/exportar")
def exportar(request: Request, run_id: int | None = None) -> Response:
    """Baixa um JSON com o estado inteiro do experimento.

    Serve para tirar o estado da tela sem copiar texto do navegador, que perde
    a estrutura e transforma campo em prosa.

    Nenhum segredo entra: as partes sao as mesmas que as telas ja mostram, e
    `/api/health` reporta presenca de credencial, nunca valor (secao 10.2.4).
    Ha teste conferindo chave por chave.
    """
    conn = _conn(request)

    partes: dict[str, Any] = {}
    falhas: dict[str, str] = {}
    for nome, produzir in (
        ("health", lambda: health(request)),
        ("config", lambda: config_atual(request)),
        ("config_history", lambda: config_historico(request, limite=200)),
        ("dataset", lambda: dataset_atual(request)),
        ("ledger", lambda: ledger_estado(request)),
        ("ledger_transacoes", lambda: ledger_transacoes(request, limite=200)),
        ("simulador", lambda: simulador_estado(request)),
        ("execucoes", lambda: execucoes_listar(request, limite=200)),
        ("comparacao", lambda: comparacao_atual(request)),
        ("curva", lambda: curva(request)),
        ("agente", lambda: agente_estado(request)),
        ("relatorio", lambda: relatorio(request, run_id)),
        ("sentinelas", lambda: sentinela_listar(request)),
    ):
        try:
            partes[nome] = produzir()
        except Exception as erro:  # noqa: BLE001
            # Uma parte que falha nao derruba o pacote. Um export vazio por
            # causa de uma tela quebrada e pior que um export que diz qual
            # tela quebrou - e e justamente quando algo quebrou que alguem
            # exporta o estado.
            falhas[nome] = f"{type(erro).__name__}: {erro}"
            log.warning("export.parte_falhou", extra={"parte": nome})

    corpo = {
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fase": "0A",
        "schema_version": versao_schema(conn),
        "build": request.app.state.build_id,
        "partes": partes,
        "partes_que_falharam": falhas,
        "aviso": (
            "Estado do experimento na Fase 0A. Nenhuma conclusao estatistica,"
            " nenhum conhecimento promovido. Numeros em amostra."
        ),
    }

    nome = f"fase0a-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    return JSONResponse(
        corpo,
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
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
