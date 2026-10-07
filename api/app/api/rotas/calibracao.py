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
from ...calibracao import (
    ajuste, bootstrap, dimensionamento, manifesto, materializacao,
    observacao, piloto, regimes, shadow,
)
from ...dataset import loader as dataset_loader
from ...maos_rapidas import baselines
from ...config import service as config_service
from ...store import bloco_atomico
from ...calibracao.identidade import SEM_PERFIL
from ..comum import _conn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/calibracao", tags=["calibracao"])


# `_agora_ms()` MORREU AQUI, e a morte e a correcao.
#
# Ele lia o relogio do PROCESSO e o passava as funcoes de selamento. Isso era
# melhor que receber a data no corpo e ainda era escolha de quem chama: um
# segundo caminho ate o selo, e nada no banco recusava um valor errado. Hoje o
# instante sai do RELOGIO DO BANCO, dentro de `ajuste`, e os gatilhos
# `..._selo_do_relogio_do_banco` conferem o que foi gravado contra ele.

CONTRATO_PADRAO = bbo.CONTRATO_PADRAO

# O nocional de uma ordem entra na condicao de validade por tamanho (ADR
# 0027), e ele e o do B3 - o unico que roda em shadow na 0C. Quem o deriva e
# `_capital_por_ordem_cents`, abaixo; repetir o valor aqui num comentario o
# faria envelhecer separado da conta.


class PedidoDeCalibracao(BaseModel):
    """O corpo do POST.

    **Sem campo de regra, e a ausencia e o ponto.** Um campo `regra` aqui,
    ainda que validado, tornaria "roda apenas o B3" uma checagem em vez de uma
    propriedade - e checagem se remove.

    **E sem `de_ms` / `ate_ms_exclusive`, que e correcao do ADR 0041.** Eles
    existiam, e com eles a finalidade de uma observacao era, na pratica,
    escolhida por quem chamava: bastava mandar o intervalo do piloto para
    materializar dentro dele. A janela passou a ser da JANELA SELADA, e quem
    a deriva e `materializacao`, uma funcao por finalidade.
    """

    model_config = {"extra": "forbid"}

    venue: str = "binance"
    symbol: str = "BTCUSDT"
    contrato: str = CONTRATO_PADRAO
    finalidade: str = Field(pattern="^(piloto|calibracao|revalidacao)$")
    regime: str | None = Field(
        default=None, pattern="^(vol_baixa|vol_media|vol_alta)$"
    )


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
    """Materializa a janela SELADA da finalidade pedida, pelo caminho oficial.

    As duas metades da medicao continuam juntas: o shadow produz o CAMINHO
    (regra congelada -> sinal -> ordem no instante real), e a observacao produz
    a BASE ESTATISTICA (todo instante da grade, nos dois lados). `E2` e
    computavel sem ordem nenhuma ter existido, e e por isso que a base e maior
    que o caminho.

    **O periodo nao vem do corpo.** Cada finalidade tem a sua janela selada, e
    `materializacao` deriva o intervalo dela: `janela_piloto` para o piloto,
    `janela_calibracao` para a calibracao, e a `janela_revalidacao` DAQUELE
    regime para a revalidacao. Um `de_ms` no corpo faria a finalidade ser um
    rotulo escolhido por quem chama.
    """
    conn = _conn(request)
    versao = config_service.versao_atual(conn)
    if versao is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="nao ha config_version: o boot deveria ter criado a 1",
        )
    if pedido.finalidade == "revalidacao" and pedido.regime is None:
        raise HTTPException(
            status_code=422,
            detail="a revalidacao e POR REGIME: sem `regime` nao ha janela. O "
                   "caminho global deixou de existir (ADR 0041)",
        )

    comum = dict(contrato=pedido.contrato, venue=pedido.venue,
                 symbol=pedido.symbol)
    orcamento = _capital_por_ordem_cents(conn, versao.config)
    # TUDO OU NADA. `registrar` abre o proprio bloco e o shadow abre o dele;
    # sem um por fora, um erro no segundo deixaria o primeiro GRAVADO - e a
    # resposta de erro teria escrita por tras. `bloco_atomico` e aninhavel
    # (SAVEPOINT), entao os de dentro continuam valendo, e o `ROLLBACK TO`
    # desfaz tudo que este pedido escreveu.
    try:
        with bloco_atomico(conn, "calibracao_rodar"):
            if pedido.finalidade == "piloto":
                r_obs = materializacao.do_piloto(
                    conn, **comum, config=versao.config,
                    config_version_id=versao.id,
                    calibracao_perfil_hash=_perfil_da_versao(conn, versao.id),
                    orcamento_cents=orcamento,
                )
            elif pedido.finalidade == "calibracao":
                janela = materializacao.ler_janela_de_calibracao(conn, **comum)
                if janela is None:
                    raise materializacao.JanelaNaoSelada(
                        "nao ha janela de calibracao selada: sele antes, e ela"
                        " comeca numa fronteira de grade posterior ao selamento"
                    )
                r_obs = materializacao.da_calibracao(
                    conn, **comum, config=versao.config,
                    config_version_id=versao.id,
                    calibracao_perfil_hash=_perfil_da_versao(conn, versao.id),
                    orcamento_cents=orcamento,
                    ate_ms_exclusive=_alcance_do_dado(conn, pedido),
                )
            else:
                r_obs = materializacao.da_revalidacao(
                    conn, **comum, regime=str(pedido.regime),
                    orcamento_cents=orcamento,
                )
            # O shadow roda no MESMO intervalo que a observacao acabou de
            # materializar - o da janela selada, e nao um pedido a parte.
            #
            # **"Sem barras" e FATO DA JANELA, e nao erro do pedido.** Uma janela
            # selada comeca no futuro: entre o selo e a chegada do dado ela esta
            # legitimamente vazia. Devolver 409 ali seria pior que inutil - a
            # observacao JA foi gravada quando o shadow roda, entao um 409
            # relataria como falha uma chamada que escreveu.
            try:
                r_shadow = shadow.rodar(
                    conn, **comum, config=versao.config,
                    config_version_id=versao.id,
                    de_ms=int(r_obs["janela"]["de_ms"]),
                    ate_ms_exclusive=int(r_obs["janela"]["ate_ms_exclusive"]),
                )
            except shadow.SemBarras as e:
                r_shadow = {
                    "estado": "aguardando_dados",
                    "motivo": str(e),
                    "o_que_isto_e": (
                        "a janela esta selada e o dado ainda nao chegou. Nao e"
                        " erro: uma janela selada comeca no FUTURO, e o periodo"
                        " entre o selo e a primeira barra e vazio por construcao"
                    ),
                }
    except materializacao.JanelaNaoSelada as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except materializacao.IdentidadeDaCalibracaoAusente as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
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

    return {
        "config_version_id": versao.id,
        "shadow": r_shadow,
        "observacoes": r_obs,
        "limite": LIMITE_DECLARADO,
    }


def _perfil_da_versao(conn, config_version_id: int) -> str:
    """O perfil que a `config_version` referencia, ou o token canonico.

    `SEM_PERFIL` e literal, e nao `None` nem string vazia: a coluna
    `calibracao_observacao.calibracao_perfil_hash` e `NOT NULL`, e tres
    representacoes de "nenhum perfil" fariam o gatilho de identidade congelada
    acusar divergencia entre linhas que descrevem a mesma coisa.
    """
    linha = conn.execute(
        "SELECT calibracao_perfil_hash FROM config_version WHERE id = ?",
        (int(config_version_id),),
    ).fetchone()
    if linha is None or linha["calibracao_perfil_hash"] is None:
        return SEM_PERFIL
    return str(linha["calibracao_perfil_hash"])


def _alcance_do_dado(conn, pedido) -> int:
    """Ate onde o dado alcanca, e nao ate onde alguem quer.

    A janela de calibracao NAO tem fim declarado - ela fecha por regime -, e
    entao o fim da materializacao e o ultimo instante com amostra mais uma
    grade. Um fim escolhido aqui seria parar por calendario, que a decisao 4 do
    usuario proibe.
    """
    linha = conn.execute(
        "SELECT MAX(t_grid_ms) AS t, MAX(grade_ms) AS g FROM bbo_amostra"
        " WHERE contrato = ? AND venue = ? AND symbol = ?",
        (pedido.contrato, pedido.venue, pedido.symbol),
    ).fetchone()
    if linha is None or linha["t"] is None:
        return 0
    return int(linha["t"]) + int(linha["g"])


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
        # `serie_e2_do_piloto`, com a finalidade LITERAL no SQL. E o que este
        # bloco sempre quis dizer - ele le a janela do PILOTO -, e agora o nome
        # da funcao afirma isso. O `config_version_id` saiu: a identidade da
        # janela e congelada pelo gatilho, e filtrar por ela aqui deixaria a
        # serie depender da config vigente no momento da leitura.
        e2 = observacao.serie_e2_do_piloto(
            conn, contrato=contrato, venue=venue, symbol=symbol, lado=lado,
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
                "o_que_isto_e": (
                    "DIMENSIONAMENTO E DIAGNOSTICO, e nao calibracao. Do"
                    " piloto saem `sigma_e`, `tau` e a forma da cauda (ADR"
                    " 0027 par.5); o `p10` daqui NAO produz override de"
                    " parametro. Quem calibra le a janela de CALIBRACAO, que e"
                    " posterior e disjunta"
                ),
                "finalidade_da_serie": "piloto",
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
        # OS CINCO ESTADOS, separados. Juntar `calibrado` com `revalidado`
        # faria um override sem confirmacao parecer confirmado.
        "regimes": ajuste.estado_por_regime(
            conn, contrato=contrato, venue=venue, symbol=symbol
        ),
        # O REGIME GRAVADO contra o que o detector diria agora. Barra futura
        # nunca move um instante; dado que chega ATRASADO para dentro da
        # janela move - e o apagao de 19 a 21/09 foi preenchido por backfill.
        # O gravado e o que vale; a divergencia e fato a declarar.
        "regime_gravado_x_recalculado": regimes.divergencias_do_gravado(
            conn, contrato=contrato, venue=venue, symbol=symbol,
            timeframe=versao.config.timeframe,
        ),
        "observacoes_por_finalidade": {
            f: observacao.quantas(
                conn, finalidade=f, contrato=contrato, venue=venue,
                symbol=symbol,
            )
            for f in observacao.FINALIDADES
        },
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


class PedidoPorRegime(PedidoDeAjuste):
    """O `regime` e OBRIGATORIO e nao tem default.

    O caminho global deixou de existir (ADR 0041): nao ha como selar nem
    consumir uma revalidacao sem regime. Um default aqui reabriria a porta que
    a migracao 33 fechou no banco - `janela_revalidacao.regime` e `NOT NULL`, e
    `calibracao_fechamento_id` tambem.

    O `pattern` recusa `indefinido`: ele nao e regime, e nao pode ter
    fechamento, override nem revalidacao.
    """

    regime: str = Field(pattern="^(vol_baixa|vol_media|vol_alta)$")


@router.post("/selar-calibracao", status_code=status.HTTP_201_CREATED)
def selar_calibracao(request: Request, pedido: PedidoDeAjuste) -> dict[str, Any]:
    """Sela a janela de CALIBRACAO, que comeca DEPOIS do proprio selamento.

    O relogio e do SISTEMA: o corpo nao carrega data nenhuma. As regras da
    revalidacao - `n_exigido` por regime, duracao, metodo, confianca, alvo,
    tratamento de dependencia e regra de encerramento - sao congeladas aqui,
    antes do primeiro dado da calibracao.
    """
    conn = _conn(request)
    versao = config_service.versao_atual(conn)
    if versao is None:
        raise HTTPException(status_code=503, detail="sem config_version")
    try:
        janela = ajuste.selar_calibracao(
            conn, contrato=pedido.contrato, venue=pedido.venue,
            symbol=pedido.symbol, config=versao.config,
        )
    except ajuste.PilotoAberto as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except dimensionamento.PilotoNaoMaterializado as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    return {"janela_de_calibracao": janela}


@router.post("/fechar-regime", status_code=status.HTTP_201_CREATED)
def fechar_regime(request: Request, pedido: PedidoPorRegime) -> dict[str, Any]:
    """Fecha a calibracao DE UM regime, quando ele alcanca o `n_exigido`.

    Idempotente. O `n_efetivo` e MEDIDO aqui, e nao recebido: um numero vindo
    do corpo seria a parada escolhida por quem pede.
    """
    conn = _conn(request)
    versao = config_service.versao_atual(conn)
    if versao is None:
        raise HTTPException(status_code=503, detail="sem config_version")
    conta = ajuste.medir_regime(
        conn, contrato=pedido.contrato, venue=pedido.venue,
        symbol=pedido.symbol, config=versao.config,
        config_version_id=versao.id, regime=pedido.regime, lado=pedido.lado,
    )
    try:
        fechamento = ajuste.fechar_regime(
            conn, contrato=pedido.contrato, venue=pedido.venue,
            symbol=pedido.symbol, regime=pedido.regime,
            n_efetivo=conta["n_efetivo"],
            ultimo_t_grid_ms=conta["ultimo_t_grid_ms"],
        )
    except (ajuste.SemJanelaDeCalibracao, ajuste.RegraNaoCongelada,
            ajuste.AmostraInsuficienteNoRegime) as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    return {
        "fechamento": fechamento,
        "medido": conta,
        "alvos_de_amostra": fechamento.get("alvos_de_amostra"),
    }


@router.post("/selar-revalidacao", status_code=status.HTTP_201_CREATED)
def selar_revalidacao(
    request: Request, pedido: PedidoPorRegime
) -> dict[str, Any]:
    """Sela a revalidacao DAQUELE regime, depois do fechamento dele.

    **Nao existe caminho global.** O `regime` e obrigatorio, e a janela so
    nasce a partir de um `calibracao_regime_fechamento` - a segunda para o
    mesmo fechamento e recusada pelo BANCO, por `UNIQUE`.

    O horario do selo vem do RELOGIO DO BANCO, e o gatilho
    `janela_revalidacao_selo_do_relogio_do_banco` confere o gravado contra ele.
    A janela comeca na PRIMEIRA grade estritamente posterior ao selo e
    posterior ao ultimo instante usado na calibracao daquele regime - e um
    gatilho recusa selar sobre instante que ja tenha observacao deste regime.

    **O corpo nao carrega nada alem da identidade.** Nem data, nem
    `n_exigido`, nem delta, nem metodo, nem confianca, nem duracao: todos vem
    de `janela_calibracao` e `calibracao_regime_regra`, congelados antes do
    primeiro dado da calibracao.
    """
    conn = _conn(request)
    try:
        # IDENTIDADE, E NADA MAIS. Sem data, sem `n_exigido`, sem delta, sem
        # metodo, sem confianca e sem duracao - tudo isso foi congelado no
        # selamento da CALIBRACAO, antes do primeiro dado dela.
        janela = ajuste.selar_revalidacao(
            conn, contrato=pedido.contrato, venue=pedido.venue,
            symbol=pedido.symbol, regime=pedido.regime,
        )
    except ajuste.CalibracaoDoRegimeNaoFechada as e:
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
def revalidar(request: Request, pedido: PedidoPorRegime) -> dict[str, Any]:
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
            lado=pedido.lado, regime=pedido.regime,
        )
    except ajuste.RevalidacaoJaConsumida as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except (ajuste.RevalidacaoNaoSelada, ajuste.NadaACalibrar) as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e


# ===========================================================================
# O FECHAMENTO da janela do piloto, e a leitura dele
#
# `piloto.fechar` existia com teste desde o incremento 18 e **sem caminho em
# producao**: nenhuma rota o chamava. Sem ele a janela nunca era gravada, e
# `estimar` recusava para sempre com `PilotoAberto` - uma funcao sem caminho e
# a forma do defeito que este projeto ja registrou varias vezes.
#
# O que e GRAVADO e uma linha; o manifesto e DERIVADO. Ver
# `app/calibracao/manifesto.py`.
# ===========================================================================


class PedidoDeFechamentoDoPiloto(BaseModel):
    """Qual serie, e **nada mais**. Sem data, sem minimo, sem fronteira.

    A ausencia e a garantia. Com `extra="forbid"`, um corpo que tente mandar
    `de_ms`, `ate_ms_exclusive` ou um minimo proprio e recusado pelo MODELO, e
    nao por uma checagem que alguem possa remover depois. A janela sai do
    registro: primeira observacao valida mais as duas travas do ADR 0027.

    **E nao ha `author`, de proposito.** As rotas irmas levam um - `revalidar`
    grava o dela como autor da `config_version`, que e uma escolha de alguem.
    Aqui nao ha escolha a atribuir, e nao existe coluna onde guardar um nome:
    `janela_piloto.fechada_por` guarda a TRAVA que venceu. Aceitar um `author`
    que so aparece numa linha de log seria um campo com cara de registro e sem
    registro - e ele ficaria colado, na leitura, no campo que significa outra
    coisa.
    """

    model_config = {"extra": "forbid"}

    venue: str = "binance"
    symbol: str = "BTCUSDT"
    contrato: str = CONTRATO_PADRAO


def _serie_bbo(venue: str, symbol: str) -> bbo.Serie:
    return bbo.Serie(
        venue=venue, symbol=symbol, price_scale_exp=0, volume_scale_exp=0
    )


@router.get("/piloto")
def piloto_janela(
    request: Request,
    venue: str = "binance",
    symbol: str = "BTCUSDT",
    contrato: str = CONTRATO_PADRAO,
) -> dict[str, Any]:
    """A janela do piloto e o manifesto derivado dela. **Nao escreve nada.**

    Com a janela fechada, devolve o manifesto do intervalo fechado; sem ela,
    devolve a PREVIA - o manifesto que o fechamento gravaria - dizendo, no
    proprio corpo, que previa nao e fechamento.
    """
    conn = _conn(request)
    try:
        return manifesto.do_registro(
            conn, serie=_serie_bbo(venue, symbol), contrato=contrato
        )
    except manifesto.ManifestoDivergente as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        ) from e
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e


@router.post("/piloto/fechar", status_code=status.HTTP_200_OK)
def piloto_fechar(
    request: Request, pedido: PedidoDeFechamentoDoPiloto
) -> dict[str, Any]:
    """Grava a linha de fechamento. Repetir devolve a que existe, SEM escrever.

    **200, e nao 201, de proposito:** a segunda chamada nao cria nada, e um 201
    fixo afirmaria criacao toda vez. Quem diz qual foi o caso e `criado_agora`.

    O corpo devolvido e montado pela MESMA funcao que serve o `GET`, entao o
    manifesto sai byte a byte igual nos dois - por construcao, e nao por
    disciplina.

    **Nao dispara estimativa, calibracao nem revalidacao.**
    """
    conn = _conn(request)
    log.info(
        "piloto.fechamento.pedido",
        extra={"contrato": pedido.contrato, "symbol": pedido.symbol},
    )
    try:
        return manifesto.fechar(
            conn,
            serie=_serie_bbo(pedido.venue, pedido.symbol),
            contrato=pedido.contrato,
        )
    except (piloto.PilotoNaoFechaAinda, manifesto.GradeIncompleta) as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except manifesto.ManifestoDivergente as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        ) from e
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
