"""Rota da calibracao do simulador. Incremento 18, ADR 0027 e ADR 0032.

**A rota nao aceita regra.** §11.2.1 e literal - na 0C o shadow "roda apenas o
baseline B3 (...) e nao valida estrategia nenhuma" -, e a recusa e por
CONSTRUCAO: nao ha campo de regra no corpo, nao ha parametro, e o modulo que
grava deriva o B3 da config versionada.

Uma checagem seria pior: protegeria enquanto ninguem a removesse, e §8.5.1 ja
disse que garantia que depende de boa vontade ja foi violada. E o mesmo desenho
do `acesso = 'agente'` literal no SQL do incremento 9.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ...aovivo import bbo
from ...calibracao import ajuste, bootstrap, observacao, piloto, shadow
from ...dataset import loader as dataset_loader
from ...maos_rapidas import baselines
from ...config import service as config_service
from ..comum import _conn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/calibracao", tags=["calibracao"])

CONTRATO_PADRAO = "bbo@1"

# O nocional de uma ordem entra na condicao de validade por tamanho (ADR
# 0027), e ele e o do B3 - o unico que roda em shadow na 0C. Quem o deriva e
# `_capital_por_ordem_cents`, abaixo; repetir o valor aqui num comentario o
# faria envelhecer separado da conta.


class PedidoDeCalibracao(BaseModel):
    """O corpo do POST.

    **Sem campo de regra, e a ausencia e o ponto.** Um campo `regra` aqui,
    ainda que validado, tornaria "roda apenas o B3" uma checagem em vez de uma
    propriedade - e checagem se remove.
    """

    model_config = {"extra": "forbid"}

    venue: str = "binance"
    symbol: str = "BTCUSDT"
    contrato: str = CONTRATO_PADRAO
    de_ms: int | None = Field(default=None, ge=0)
    ate_ms_exclusive: int | None = Field(default=None, ge=0)


def _capital_por_ordem_cents(conn, config) -> int:
    """O nocional que uma ordem do B3 giraria, em centavos.

    Capital semente x a fracao de posicao **DA PROPRIA REGRA B3**, e nao um
    numero escrito aqui. A primeira versao desta funcao fazia
    `semente * 10_000 // 10_000` - um no-op com cara de conta, que continuaria
    devolvendo a semente inteira no dia em que o B3 passasse a operar com
    fracao menor. E a forma exata do defeito que este projeto ja registrou:
    um valor que descreve algo, para de descrever, e nada avisa.
    """
    from ...maos_rapidas.baselines import regra_b3

    fracao = regra_b3(config).position_fraction_bps
    return int(config.seed_capital_usd_cents) * int(fracao) // 10_000


@router.post("/rodar", status_code=status.HTTP_200_OK)
def rodar(request: Request, pedido: PedidoDeCalibracao) -> dict[str, Any]:
    """Roda o shadow do B3 e registra as observacoes do periodo.

    As duas coisas juntas porque sao duas metades da mesma medicao: o shadow
    produz o CAMINHO (regra congelada -> sinal -> ordem no instante real), e a
    observacao produz a BASE ESTATISTICA (todo instante da grade, nos dois
    lados). `E2` e computavel sem ordem nenhuma ter existido, e e por isso que
    a base e maior que o caminho.
    """
    conn = _conn(request)
    versao = config_service.versao_atual(conn)
    if versao is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="nao ha config_version: o boot deveria ter criado a 1",
        )

    try:
        r_shadow = shadow.rodar(
            conn, contrato=pedido.contrato, venue=pedido.venue,
            symbol=pedido.symbol, config=versao.config,
            config_version_id=versao.id,
            de_ms=pedido.de_ms, ate_ms_exclusive=pedido.ate_ms_exclusive,
        )
        r_obs = observacao.registrar(
            conn, contrato=pedido.contrato, venue=pedido.venue,
            symbol=pedido.symbol, config=versao.config,
            config_version_id=versao.id,
            orcamento_cents=_capital_por_ordem_cents(conn, versao.config),
            de_ms=pedido.de_ms, ate_ms_exclusive=pedido.ate_ms_exclusive,
        )
    except observacao.ContratoIncompativel as e:
        # 409, e nao 422: o pedido esta perfeito e o MUNDO e que mudou. A
        # semantica de execucao vigente deixou de ser a que o contrato assume.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except bbo.ContratoDesconhecido as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    except shadow.SemBarras as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e

    return {
        "config_version_id": versao.id,
        "shadow": r_shadow,
        "observacoes": r_obs,
        "limite": LIMITE_DECLARADO,
    }


# O limite vai no PROPRIO resultado, e nao numa nota de rodape do relatorio
# (R65). §8.4.1.2 diz que o shadow calibra o que ele consegue observar, e ele
# so observa o lado agressor.
LIMITE_DECLARADO = (
    "calibra TAKER, e nao maker. O shadow observa o preco que uma ordem "
    "agressora encontraria no topo de livro; nada aqui mede fila, prioridade "
    "nem preenchimento passivo, e a regra 10 proibe afirmar fidelidade de "
    "book. Calibrar maker exigiria ENVIAR ordem, o que e Fase 3."
)


@router.get("")
def estado(
    request: Request,
    venue: str = "binance",
    symbol: str = "BTCUSDT",
    contrato: str = CONTRATO_PADRAO,
    lado: str = "compra",
) -> dict[str, Any]:
    """Onde a calibracao esta: cobertura, piloto, shadow e o erro medido.

    **O intervalo so sai com a janela do piloto FECHADA.** Publicar `p10(E2)`
    de um piloto em andamento seria olhar o resultado antes de o periodo
    declarado terminar - a quinta pergunta do teste de escopo, exatamente.
    """
    conn = _conn(request)
    versao = config_service.versao_atual(conn)
    if versao is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="sem config_version"
        )

    serie_bbo = bbo.Serie(venue=venue, symbol=symbol,
                          price_scale_exp=0, volume_scale_exp=0)
    janela = piloto.ler(conn, serie_bbo, contrato)

    erro: dict[str, Any] = {"estado": "piloto_em_andamento"}
    if janela is not None:
        e2 = observacao.serie_e2(
            conn, contrato=contrato, venue=venue, symbol=symbol, lado=lado,
            config_version_id=versao.id,
            de_ms=janela.de_ms, ate_ms_exclusive=janela.ate_ms_exclusive,
        )
        try:
            ic = bootstrap.limite_inferior_do_quantil(e2, 0.10)
            sigma = bootstrap.desvio_padrao([float(v) for v in e2])
            necessario = bootstrap.tamanho_necessario(
                sigma_e=sigma, delta=500.0, tau=ic.tau.tau
            )
            erro = {
                "estado": "medido",
                "n": ic.n,
                "n_efetivo": round(ic.n_efetivo, 1),
                "p10_mili_bps": round(ic.ponto, 1),
                "limite_inferior_95_mili_bps": round(ic.limite_inferior, 1),
                "pessimista": ic.limite_inferior >= 0,
                "bloco": ic.bloco,
                "tau": round(ic.tau.tau, 3),
                "tau_bruto": round(ic.tau.tau_bruto, 3),
                "ar1_plausivel": ic.tau.ar1_plausivel,
                "sigma_e_mili_bps": round(sigma, 1),
                "n_efetivo_necessario": necessario.n_efetivo,
                "n_bruto_necessario": necessario.n_bruto,
                "amostra_suficiente": ic.n_efetivo >= necessario.n_efetivo,
            }
        except bootstrap.SerieCurtaDemais as e:
            erro = {"estado": "amostra_insuficiente", "motivo": str(e)}

    return {
        "config_version_id": versao.id,
        "contrato": contrato,
        "lado": lado,
        "piloto": (
            {"estado": "acumulando"} if janela is None
            else {
                "estado": "fechada",
                "de_ms": janela.de_ms,
                "ate_ms_exclusive": janela.ate_ms_exclusive,
                "observacoes_validas": janela.observacoes_validas,
                "dias_corridos": janela.dias_corridos,
                "fechada_por": janela.fechada_por,
            }
        ),
        "shadow": shadow.resumo(
            conn, contrato=contrato, venue=venue, symbol=symbol,
            config_version_id=versao.id,
        ),
        "erro_de_execucao": erro,
        "delta_mili_bps": 500,
        "limite": LIMITE_DECLARADO,
    }


# ===========================================================================
# O ajuste. As tres acoes sao SEPARADAS de proposito.
#
# Selar, ajustar e revalidar sao irreversiveis e acontecem em momentos
# diferentes do calendario. Junta-las num botao so faria a ordem virar detalhe
# de implementacao - e a ORDEM e a garantia 7: a janela de revalidacao e selada
# ANTES do ajuste, porque sela-la depois de ver `p10` e escolher o periodo que
# confirma.
# ===========================================================================


class PedidoDeAjuste(BaseModel):
    model_config = {"extra": "forbid"}

    author: str = Field(min_length=1, max_length=120)
    venue: str = "binance"
    symbol: str = "BTCUSDT"
    contrato: str = CONTRATO_PADRAO
    lado: str = Field(default="compra", pattern="^(compra|venda)$")


@router.post("/selar-revalidacao", status_code=status.HTTP_201_CREATED)
def selar_revalidacao(
    request: Request, pedido: PedidoDeAjuste
) -> dict[str, Any]:
    """Sela o periodo posterior reservado. **Antes do ajuste.**

    Idempotente: chamar de novo devolve a janela que ja existe, e nunca uma
    nova. Uma segunda janela seria uma segunda chance de escolher a fronteira.
    """
    conn = _conn(request)
    try:
        janela = ajuste.selar_revalidacao(
            conn, contrato=pedido.contrato, venue=pedido.venue,
            symbol=pedido.symbol,
        )
    except ajuste.PilotoAberto as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    return {"janela_de_revalidacao": janela}


@router.post("/ajustar", status_code=status.HTTP_201_CREATED)
def ajustar(request: Request, pedido: PedidoDeAjuste) -> dict[str, Any]:
    """Estima sobre o piloto, deriva o ajuste e reexecuta o B1 negativo.

    **O B1 negativo vem junto, e nao numa rota a parte** (criterio 7 do
    incremento 18): §14.4 o torna portao, e o que ele responde e "operar ao
    acaso continua perdendo depois do ajuste?". Se passasse a dar lucro, a
    calibracao estaria errada e nenhum numero medido nela significaria coisa
    alguma - entao a conferencia pertence ao mesmo ato que produz o ajuste.
    """
    conn = _conn(request)
    versao = config_service.versao_atual(conn)
    if versao is None:
        raise HTTPException(status_code=503, detail="sem config_version")

    try:
        r = ajuste.aplicar(
            conn, contrato=pedido.contrato, venue=pedido.venue,
            symbol=pedido.symbol, config=versao.config,
            config_version_id=versao.id, autor=pedido.author,
            settings=request.app.state.settings, lado=pedido.lado,
        )
    except ajuste.PilotoAberto as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except ajuste.RevalidacaoNaoSelada as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except ajuste.NadaACalibrar as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e

    r["b1_negativo"] = _b1_negativo(conn, r)
    r["limite"] = LIMITE_DECLARADO
    return r


def _b1_negativo(conn, resultado: dict[str, Any]) -> dict[str, Any]:
    """A2 do Portao A, reexecutado sob a versao CALIBRADA (R67).

    Sem ajuste aplicado nao ha o que reexecutar, e dizer "passou" nesse caso
    seria afirmar uma conferencia que nao aconteceu - `None` com o motivo
    escrito, como o Portao A ja faz com criterio nao medido.
    """
    nova = resultado.get("config_version_nova")
    if nova is None:
        return {
            "reexecutado": False,
            "motivo": "nenhum ajuste foi aplicado, entao nao ha versao nova "
                      "sob a qual reexecutar. O B1 sob a config vigente "
                      "continua valendo, e e o do Portao A",
        }

    nova_config = config_service.versao_por_id(conn, int(nova))
    meta = dataset_loader.dataset_vigente(conn)
    if nova_config is None or meta is None:
        return {
            "reexecutado": False,
            "motivo": "nao ha dataset ingerido: o B1 roda sobre a janela "
                      "historica, e sem ela nao ha o que sortear",
        }

    baselines.rodar_comparacao(
        conn, dataset_id=meta.id, config=nova_config.config,
        config_version_id=int(nova), semente=42,
    )
    corridas = baselines.todos_os_b1(conn, int(nova))
    semente_cents = nova_config.config.seed_capital_usd_cents
    perdas = [
        {
            "operacoes_alvo": c["operacoes_alvo"],
            "p50_cents": c["p50"],
            "perda_cents": semente_cents - c["p50"],
        }
        for c in corridas
    ]
    negativo = bool(perdas) and all(p["perda_cents"] > 0 for p in perdas)
    return {
        "reexecutado": True,
        "config_version": int(nova),
        "corridas": perdas,
        "negativo": negativo,
        "criterio": "operar ao acaso perde, depois do ajuste (R67, A2)",
        "consequencia_se_falhar": "se operar ao acaso passasse a dar lucro, a "
                                  "calibracao estaria errada e nenhum numero "
                                  "medido nela significaria coisa alguma",
    }


@router.post("/revalidar", status_code=status.HTTP_201_CREATED)
def revalidar(request: Request, pedido: PedidoDeAjuste) -> dict[str, Any]:
    """Confirma na janela selada, **sem novo ajuste**, uma unica vez."""
    conn = _conn(request)
    versao = config_service.versao_atual(conn)
    if versao is None:
        raise HTTPException(status_code=503, detail="sem config_version")

    ultima = conn.execute(
        "SELECT id FROM calibracao_versao"
        " WHERE contrato = ? AND venue = ? AND symbol = ?"
        " ORDER BY id DESC LIMIT 1",
        (pedido.contrato, pedido.venue, pedido.symbol),
    ).fetchone()
    if ultima is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="nao ha calibracao para revalidar: ajuste antes",
        )

    try:
        return ajuste.revalidar(
            conn, contrato=pedido.contrato, venue=pedido.venue,
            symbol=pedido.symbol, config=versao.config,
            config_version_id=versao.id, calibracao_id=int(ultima["id"]),
            lado=pedido.lado,
        )
    except ajuste.RevalidacaoJaConsumida as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except (ajuste.RevalidacaoNaoSelada, ajuste.NadaACalibrar) as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
