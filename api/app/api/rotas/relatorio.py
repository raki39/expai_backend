"""Rotas de **relatorio**.

Fechamento derivado, vinculo nos dois sentidos e export do estado.
"""

from __future__ import annotations

import json
import logging
from ... import fase as fase_mod
from ...config import service as config_service
from ...dataset import loader as dataset_loader
from ...relatorio import montar as relatorio_montar
from ...relatorio import auditoria as relatorio_auditoria
from ...relatorio import portao_a as relatorio_portao_a
from ...relatorio import portao_b as relatorio_portao_b
from ...relatorio import reprodutibilidade as relatorio_reprodutibilidade
from ...relatorio import texto as relatorio_texto
from ...relatorio import vinculo as relatorio_vinculo
from ...store import (
    conexao_do_thread,
    versao_schema,
    volume_gravavel,
    volume_montado,
)
from datetime import datetime, timezone
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi import Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import PlainTextResponse
from typing import Any
from ..modelos import PedidoAuditoria, PedidoPortaoB, PedidoProva
from ..comum import _conn

# O export REUNE todas as telas, entao ele depende de um handler de cada
# dominio. Antes do split isso ficava escondido pela proximidade - as treze
# funcoes moravam no mesmo arquivo. Aqui a dependencia esta escrita.
#
# E ela e real e vale manter visivel: `exportar` nao recalcula nada, so chama
# as funcoes que ja servem cada tela. Se recalculasse, o export poderia
# discordar do painel, e a divergencia so apareceria em quem exportou.
from .a1a import a1a_estado
from .a1b import a1b_estado
from .agente import agente_estado
from .b4 import b4_estado
from .baselines import comparacao_atual, curva
from .config import config_atual, config_historico
from .aovivo import (
    estado as aovivo_estado,
    listar_snapshots as aovivo_snapshots,
)
from .dataset import dataset_atual, separacao_de_dados
from .diagnostico import sentinela_listar
from .ledger import ledger_estado, ledger_transacoes
from .simulador import execucoes_listar, simulador_estado
from .substrato import health
from .validador import creditos_de_teste, lote_fechado, validador_estado

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/relatorio", tags=["relatorio"])


@router.get("")
def relatorio(request: Request, run_id: int | None = None) -> dict[str, Any]:
    """O relatorio de fechamento em JSON. `run_id` ausente usa o ultimo run."""
    return relatorio_montar.montar(_conn(request), run_id)


@router.get("/portao-a")
def portao_a(request: Request) -> dict[str, Any]:
    """O relatório do Portão A: **o protocolo rejeita defeito?** (§14.4).

    Pergunta diferente da do `/api/relatorio`, que é o fechamento da 0A e
    responde "o ciclo básico fecha?". Rota própria porque juntar as duas faria
    um relatório de fase antiga responder pela fase corrente — e o campo
    `fase` de `/api/health` já mentiu uma vez por menos que isso.
    """
    conn = _conn(request)
    atual = config_service.versao_atual(conn)
    if atual is None:
        return {"existe": False, "motivo": "configuracao nao inicializada"}
    meta = dataset_loader.dataset_vigente(conn)
    return {
        "existe": True,
        **relatorio_portao_a.montar(
            conn,
            config_version_id=atual.id,
            config=atual.config,
            dataset_id=meta.id if meta else None,
        ),
    }


def _config_e_dataset(conn):
    """A config vigente e o dataset. `None` quando falta um dos dois."""
    atual = config_service.versao_atual(conn)
    meta = dataset_loader.dataset_vigente(conn)
    return atual, (meta.id if meta else None)


@router.get("/portao-b")
def portao_b(request: Request) -> dict[str, Any]:
    """O Portão B: **existe candidata digna de auditoria?** (§14.4).

    Leitura: **não roda o walk-forward**. Ele executa a regra sobre as três
    janelas e abre runs, e uma rota de leitura que escrevesse a cada
    carregamento do painel encheria um registro append-only de runs que
    ninguém pediu. Para executá-lo, `POST`.
    """
    conn = _conn(request)
    atual, dataset_id = _config_e_dataset(conn)
    if atual is None:
        return {"existe": False, "motivo": "configuracao nao inicializada"}
    return {
        "existe": True,
        **relatorio_portao_b.montar(
            conn,
            config_version_id=atual.id,
            config=atual.config,
            dataset_id=dataset_id,
            rodar_forward=False,
        ),
    }


@router.post("/portao-b", status_code=status.HTTP_201_CREATED)
def portao_b_executar(
    request: Request, pedido: PedidoPortaoB = Body(...)
) -> dict[str, Any]:
    """Roda o walk-forward das candidatas e devolve o Portão B completo.

    **Não gasta dinheiro:** o walk-forward é execução determinística sobre
    barras que já existem, e nenhuma reflexão acontece aqui. O que ele gasta é
    CPU e runs.
    """
    conn = _conn(request)
    if config_service.run_ativo(conn) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="encerre o run ativo antes de rodar o walk-forward",
        )
    atual, dataset_id = _config_e_dataset(conn)
    if atual is None:
        raise HTTPException(status_code=503, detail="configuracao nao inicializada")
    if dataset_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ingira o dataset antes de rodar o walk-forward",
        )
    log.info("portao_b.pedido", extra={"author": pedido.author})
    return relatorio_portao_b.montar(
        conn,
        config_version_id=atual.id,
        config=atual.config,
        dataset_id=dataset_id,
        rodar_forward=True,
    )


@router.get("/auditoria/{hypothesis_id}")
def auditoria_ler(request: Request, hypothesis_id: int) -> dict[str, Any]:
    """O roteiro de §14.4.1, na parte que só **lê**.

    A revisão das cinco operações mais lucrativas e a conferência de que nada
    vive fora do modo pessimista não escrevem nada. Reexecutar com semente
    trocada e com custo dobrado escreve, e por isso é `POST`.
    """
    conn = _conn(request)
    atual, dataset_id = _config_e_dataset(conn)
    if atual is None:
        raise HTTPException(status_code=503, detail="configuracao nao inicializada")
    try:
        return relatorio_auditoria.montar(
            conn,
            hypothesis_id=hypothesis_id,
            dataset_id=dataset_id,
            config=atual.config,
            config_version_id=atual.id,
            executar=False,
        )
    except relatorio_auditoria.SemRun as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/auditoria", status_code=status.HTTP_201_CREATED)
def auditoria_executar(
    request: Request, pedido: PedidoAuditoria = Body(...)
) -> dict[str, Any]:
    """Executa o roteiro inteiro de §14.4.1. Abre runs, não gasta dinheiro.

    §14.4.1 é explícito sobre a postura: **passar no Portão B é tratado como
    suspeita de defeito até prova em contrário**, e esta rota é o que produz a
    prova — ou a falta dela.
    """
    conn = _conn(request)
    if config_service.run_ativo(conn) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="encerre o run ativo antes da auditoria",
        )
    atual, dataset_id = _config_e_dataset(conn)
    if atual is None:
        raise HTTPException(status_code=503, detail="configuracao nao inicializada")
    if dataset_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ingira o dataset antes da auditoria",
        )
    try:
        return relatorio_auditoria.montar(
            conn,
            hypothesis_id=pedido.hypothesis_id,
            dataset_id=dataset_id,
            config=atual.config,
            config_version_id=atual.id,
            executar=True,
            semente_alternativa=pedido.semente_alternativa,
        )
    except relatorio_auditoria.SemRun as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/markdown", response_class=PlainTextResponse)
def relatorio_markdown(request: Request, run_id: int | None = None) -> str:
    """O mesmo relatorio, para humano. So formata; nao calcula nada."""
    return relatorio_texto.markdown(
        relatorio_montar.montar(_conn(request), run_id)
    )


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
    # (nome da parte, ROTA que ela espelha, como produzi-la).
    #
    # A rota esta na tupla de proposito. Sem ela, a unica maneira de conferir
    # se o export cobre as telas era casar nome de parte com caminho por
    # heuristica - e heuristica de nome erra em `sentinelas` -> `/sentinela` e
    # em `ledger_transacoes` -> `/ledger/transacoes`. Com a rota declarada, o
    # teste compara caminho com caminho, e uma rota nova quebra a guarda ate
    # alguem decidir se ela entra no pacote.
    for nome, _rota, produzir in (
        ("health", "/api/substrato/health", lambda: health(request)),
        ("config", "/api/config", lambda: config_atual(request)),
        ("config_history", "/api/config/historico",
         lambda: config_historico(request, limite=200)),
        ("dataset", "/api/dataset", lambda: dataset_atual(request)),
        ("separacao", "/api/dataset/separacao",
         lambda: separacao_de_dados(request)),
        # O fluxo ao vivo e os intervalos fechados (ADR 0029). As duas entram
        # porque respondem perguntas que o export sem elas nao responderia:
        # `aovivo` diz quanto ATRASO existe - e atraso nao e lacuna, porque
        # kline e recuperavel -, e `snapshots` diz quais intervalos fechados
        # existem, com hash e completude. Todo resultado do forward cita um
        # deles, entao um export sem eles mostraria o resultado e nao a
        # procedencia dele.
        ("aovivo", "/api/aovivo/estado", lambda: aovivo_estado(request)),
        ("snapshots", "/api/aovivo/snapshots",
         lambda: aovivo_snapshots(request)),
        ("ledger", "/api/ledger", lambda: ledger_estado(request)),
        ("ledger_transacoes", "/api/ledger/transacoes",
         lambda: ledger_transacoes(request, limite=200)),
        ("simulador", "/api/simulador", lambda: simulador_estado(request)),
        ("execucoes", "/api/simulador/execucoes",
         lambda: execucoes_listar(request, limite=200)),
        ("comparacao", "/api/baselines", lambda: comparacao_atual(request)),
        ("curva", "/api/baselines/curva", lambda: curva(request)),
        ("agente", "/api/agente", lambda: agente_estado(request)),
        # O braco de controle. Entra no pacote pelo mesmo motivo do agente: a
        # comparacao entre os dois e o produto da fase, e um export com um
        # braco so responderia metade da pergunta.
        ("b4", "/api/b4", lambda: b4_estado(request)),
        # Os dois controles do PROTOCOLO. B4 e o controle do agente; A1a e
        # A1b sao o controle de quem julga - e o Portao A e sobre eles.
        # Um export sem eles mostraria o placar e nao mostraria se o juiz
        # funciona, que e a pergunta que a 0B faz (§14.4).
        ("a1a", "/api/a1a", lambda: a1a_estado(request)),
        ("a1b", "/api/a1b", lambda: a1b_estado(request)),
        # As quatro partes da 0B. Faltavam: este export foi escrito no
        # incremento 7 com uma tupla literal, e parou de descrever o sistema
        # no instante em que a 0B acrescentou rota - em silencio, que e o
        # padrao de sempre. Foi preciso o usuario mandar os JSONs a mao para
        # a falta aparecer.
        ("validador", "/api/validador", lambda: validador_estado(request)),
        ("lote", "/api/validador/lote", lambda: lote_fechado(request)),
        ("creditos", "/api/validador/creditos",
         lambda: creditos_de_teste(request)),
        ("relatorio", "/api/relatorio", lambda: relatorio(request, run_id)),
        # O PRODUTO da fase corrente. O `relatorio` acima e o fechamento
        # da 0A e responde a pergunta DELA; este responde a da 0B.
        ("portao_a", "/api/relatorio/portao-a", lambda: portao_a(request)),
        # O Portao B em LEITURA: ele nao roda o walk-forward, e por isso
        # entra no pacote sem escrever nada. Sem ele o export mostraria o
        # portao que autoriza e nao o que ele autorizou.
        ("portao_b", "/api/relatorio/portao-b", lambda: portao_b(request)),
        ("sentinelas", "/api/diagnostico/sentinela",
         lambda: sentinela_listar(request)),
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
        "fase": fase_mod.FASE,
        "schema_version": versao_schema(conn),
        "build": request.app.state.build_id,
        "partes": partes,
        "partes_que_falharam": falhas,
        "aviso": fase_mod.AVISO,
    }

    nome = f"fase0a-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    # INDENTADO, e nao pelo `JSONResponse` padrao.
    #
    # `JSONResponse` serializa compacto - `separators=(",", ":")` -, o que e
    # certo para resposta de API e errado para isto: o export existe para uma
    # PESSOA abrir num editor e ler. Trezentos kB numa linha unica nao sao
    # legiveis em editor nenhum, e o arquivo era a forma de tirar o estado da
    # tela sem perder estrutura - perde-la na serializacao anula o motivo.
    #
    # `ensure_ascii=False` mantem os simbolos de secao e os acentos como
    # caracteres, em vez de escapes: o texto e para ler.
    return Response(
        content=json.dumps(
            jsonable_encoder(corpo), indent=2, ensure_ascii=False
        ),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


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


@router.get("/vinculo/execucao/{execution_id}")
def vinculo_da_execucao(request: Request, execution_id: int) -> dict[str, Any]:
    """De uma execucao qualquer ao evento cognitivo que a autorizou (R25.2)."""
    return relatorio_vinculo.da_execucao_ao_evento(_conn(request), execution_id)


@router.get("/vinculo/evento/{event_id}")
def vinculo_do_evento(request: Request, event_id: int) -> dict[str, Any]:
    """Da decisao ao custo, a regra, as execucoes e ao resultado (R25.2)."""
    return relatorio_vinculo.do_evento_ao_resultado(_conn(request), event_id)
