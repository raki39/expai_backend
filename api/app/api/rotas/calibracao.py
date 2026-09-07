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
from ...calibracao import bootstrap, observacao, piloto, shadow
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
