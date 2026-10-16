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
        # DELEGADO a `dimensionamento`, e nao recalculado aqui.
        #
        # Este bloco reimplementava a estimativa do piloto e a publicava com as
        # chaves `p10_mili_bps`, `limite_inferior_95_mili_bps` e `pessimista` -
        # **byte a byte as mesmas** que `ajuste` usa para o p10 que AJUSTA
        # (ajuste.py, `estimar_por_regime`) e para o LB95 que CONFIRMA
        # (`revalidar`). Duas grandezas de finalidades diferentes sob um nome
        # so: quem lesse o diagnostico do piloto leria um ajuste.
        #
        # `pessimista` era o pior dos tres. Ele e `LB95 >= 0`, que e literal e
        # exatamente `REGRAS_DA_REVALIDACAO["alvo"]` - o criterio de
        # revalidacao -, calculado sobre a janela do PILOTO e publicado como
        # veredito booleano. O ADR 0027 par.5 e o ADR 0041 sao explicitos em
        # que os dados do piloto ficam fora da revalidacao; um campo que
        # responde a pergunta dela sobre a serie errada e a forma mais direta
        # de atravessar essa fronteira.
        #
        # `dimensionamento.como_dict()` ja era o contrato correto - nomes
        # `*_diagnostico`, aninhados, com o que eles NAO sao escrito ao lado.
        # Havia duas implementacoes da mesma coisa, e elas divergiram.
        try:
            d = dimensionamento.dimensionar(
                conn, contrato=contrato, venue=venue, symbol=symbol,
                config=versao.config, lado=lado,
            )
            erro = {"estado": "medido", **d.como_dict()}
        except (dimensionamento.PilotoNaoMaterializado,
                bootstrap.SerieCurtaDemais) as e:
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
    # `lado` SAIU daqui. ADR 0042: o chamador nao escolhe o lado, e nenhum
    # default de Python decide por ele - selar, fechar e revalidar percorrem
    # sempre os dois. Com `extra="forbid"`, mandar `lado` agora e 422.


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


@router.get("/previa-selamento")
def previa_selamento(
    request: Request,
    venue: str = "binance",
    symbol: str = "BTCUSDT",
    contrato: str = CONTRATO_PADRAO,
) -> dict[str, Any]:
    """O que `selar-calibracao` GRAVARIA, sem gravar. Somente leitura.

    Existe porque selar e **irreversivel e uma vez so** - `ler_janela_de_
    calibracao` devolve a que existe, e uma segunda seria uma segunda chance
    de escolher a fronteira. Conferir depois de gravar e conferir tarde.

    **Os DOIS lados sao calculados**, e nao so o `compra` que `selar_calibracao`
    usa por default. A regra que decide qual lado determina `n_exigido` **nao
    esta declarada em lugar nenhum** - e um default de Python, herdado em
    silencio -, entao publicar um lado so esconderia a escolha.
    """
    conn = _conn(request)
    versao = config_service.versao_atual(conn)
    if versao is None:
        raise HTTPException(status_code=503, detail="sem config_version")

    grade_ms = int(bbo.ler_contrato(conn, contrato).grade_ms)
    serie_bbo = bbo.Serie(venue=venue, symbol=symbol,
                          price_scale_exp=0, volume_scale_exp=0)
    janela_p = piloto.ler(conn, serie_bbo, contrato)
    if janela_p is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="o piloto nao fechou; nao ha o que prever",
        )

    ja = materializacao.ler_janela_de_calibracao(
        conn, contrato=contrato, venue=venue, symbol=symbol
    )

    # A MESMA EXPRESSAO do `INSERT`, como SELECT. Reimplementa-la em Python
    # daria um segundo lugar calculando a fronteira - foi por isso que
    # `_agora_ms_do_banco` foi removida.
    de_ms = int(conn.execute(
        f"SELECT {ajuste.PRIMEIRA_GRADE_SQL}",
        (grade_ms, grade_ms, janela_p.ate_ms_exclusive),
    ).fetchone()[0])

    por_lado: dict[str, Any] = {}
    for lado in ("compra", "venda"):
        try:
            d = dimensionamento.dimensionar(
                conn, contrato=contrato, venue=venue, symbol=symbol,
                config=versao.config, lado=lado,
            )
        except (dimensionamento.PilotoNaoMaterializado,
                bootstrap.SerieCurtaDemais) as e:
            por_lado[lado] = {"estado": "sem_amostra", "motivo": str(e)}
            continue
        try:
            duracao_ms, conta = dimensionamento.duracao_da_calibracao_ms(
                d, grade_ms=grade_ms
            )
        except dimensionamento.SemHorizonteDerivavel as e:
            duracao_ms, conta = None, {"erro": str(e)}
        por_lado[lado] = {
            "estado": "medido",
            "n": d.n,
            "sigma_e_mili_bps": round(d.sigma_e_mili_bps, 3),
            "tau": round(d.tau, 4),
            "n_exigido_global": d.n_exigido,
            "n_bruto_exigido_global": d.n_bruto_exigido,
            "regras_que_seriam_gravadas": [
                {
                    "regime": r.regime,
                    "n_exigido_calibracao": r.n_exigido,
                    "n_exigido_revalidacao": r.n_exigido,
                    "sigma_e_mili_bps": (
                        None if r.sigma_e_mili_bps is None
                        else round(r.sigma_e_mili_bps, 3)
                    ),
                    "tau": None if r.tau is None else round(r.tau, 4),
                    "piloto_n": r.n,
                    "fracao_do_piloto_ppm": r.fracao_do_piloto_ppm,
                    "barras_de_calendario": r.barras_de_calendario,
                    "motivo": r.motivo,
                    "entraria_na_tabela": r.n_exigido is not None,
                }
                for r in d.por_regime
            ],
            "duracao_calibracao_ms": duracao_ms,
            "duracao_barras": (
                None if duracao_ms is None else duracao_ms // grade_ms
            ),
            "duracao_horas_milesimos": (
                None if duracao_ms is None
                else round(duracao_ms / 3_600_000, 3)
            ),
            "ate_ms_exclusive": (
                None if duracao_ms is None else de_ms + duracao_ms
            ),
            "horizonte_derivado": conta,
        }

    return {
        "o_que_isto_e": (
            "PREVIA. Nada e gravado. Mostra exatamente o que"
            " `POST /api/calibracao/selar-calibracao` escreveria em"
            " `janela_calibracao` e `calibracao_regime_regra` se fosse"
            " chamado agora"
        ),
        "ja_existe_janela_selada": ja is not None,
        "alvo_e_identidade": _alvo_e_identidade(conn, versao),
        "janela_que_seria_gravada": {
            "de_ms": de_ms,
            "primeira_grade_admissivel": (
                "a PROXIMA fronteira de grade estritamente depois do"
                " selamento, e nunca antes do fim do piloto. Calculada pelo"
                " relogio do BANCO dentro do INSERT; o gatilho"
                " `janela_calibracao_de_ms_calculado_pelo_banco` recalcula a"
                " mesma conta e recusa qualquer outro valor"
            ),
            "expressao": ajuste.PRIMEIRA_GRADE_SQL,
            "piloto_ate_ms_exclusive": janela_p.ate_ms_exclusive,
            "grade_ms": grade_ms,
            "delta_mili_bps": ajuste.DELTA_MILI_BPS,
            "regras_da_revalidacao": dict(ajuste.REGRAS_DA_REVALIDACAO),
            "dias_revalidacao": ajuste.DIAS_REVALIDACAO,
            "de_ms_anda_com_o_relogio": (
                "este `de_ms` vale para o instante desta leitura. Selar mais"
                " tarde empurra a fronteira, porque ela e a proxima grade"
                " depois do selamento - e so ela"
            ),
        },
        "por_lado": por_lado,
        "a_regra_dos_dois_lados": _a_regra_dos_dois_lados(),
        "o_piso_de_10_NAO_e_autorizacao": _o_piso_de_10(),
        "permanencia_e_cobertura": _permanencia_e_cobertura(),
    }


def _alvo_e_identidade(conn, versao) -> dict[str, Any]:
    from ...certificacao import alvo as alvo_mod
    from ...certificacao import escopos as escopos_mod

    meta = dataset_loader.dataset_vigente(conn)
    fonte = None if meta is None else (
        getattr(meta, "sha256", None) or getattr(meta, "hash", None)
    )
    perfil_vigente = _perfil_da_versao(conn, versao.id)
    if fonte is None:
        # DECLARA em vez de levantar. A previa e leitura, e um 500 aqui
        # esconderia tudo o mais que ela tem a dizer - `n_exigido` por regime,
        # o piso, a cobertura - por causa de um componente do alvo que nao
        # existe neste ambiente.
        return {
            "alvo_de_certificacao_hash": None,
            "por_que_nao": (
                "nao ha dataset vigente, e o alvo tem a fonte de dados como"
                " componente. Sem ela o alvo nao e derivavel - e inventar um"
                " seria publicar um hash que nao descreve laboratorio nenhum"
            ),
            "config_version_id": versao.id,
            "calibracao_perfil_hash": perfil_vigente,
            "e_a_identidade_BASE": perfil_vigente == SEM_PERFIL,
        }
    o_alvo = alvo_mod.montar(conn, dataset_hash=fonte)
    alvo_hash = o_alvo["alvo_de_certificacao_hash"]
    comp = escopos_mod.composicao(conn, alvo_hash)
    return {
        "alvo_de_certificacao_hash": alvo_hash,
        "identidade_executavel": o_alvo["componentes"]["identidade_executavel"],
        "config_version_id": versao.id,
        "calibracao_perfil_hash": perfil_vigente,
        "e_a_identidade_BASE": perfil_vigente == SEM_PERFIL,
        "certificacao_do_alvo": {
            "passa": comp["passa"],
            "certificados": comp["certificados"],
            "pendentes": comp["pendentes"],
        },
        "o_que_selar_NAO_confere": (
            "`selar-calibracao` NAO exige alvo certificado - quem exige e"
            " `selar-revalidacao`, por gatilho. Selar a calibracao sob"
            " laboratorio nao certificado produziria observacao que o passo 3"
            " do runbook existe para preceder"
        ),
    }


def _a_regra_dos_dois_lados() -> dict[str, Any]:
    """ADR 0042. O que era defeito aberto virou regra declarada."""
    return {
        "adr": "0042",
        "premissa": (
            "compra e venda sao populacoes separadas, e o simulador aplica um"
            " `spread_bps` so. Medir separado, decidir PELO PIOR"
        ),
        "dimensionar": "separado, por lado, dentro de cada regime",
        "n_exigido_gravado": (
            "o MAIOR entre os dois lados. A media deixaria o lado mais"
            " ruidoso ser diluido pelo mais limpo, e e o ruidoso que"
            " determina quanta amostra e precisa"
        ),
        "fechar": (
            "compra E venda, cada um por si, contra o mesmo `n_exigido`"
            " conservador. Nao e a soma: 2x observacoes de um lado so nao"
            " medem o outro"
        ),
        "ajustar": (
            "a MAIOR correcao exigida entre os lados. Nunca media, nunca"
            " combinacao que permita um lado bom esconder o ruim"
        ),
        "revalidar": "LB95(p10(E2)) >= 0 em CADA lado, e nao no agregado",
        "o_corpo_nao_escolhe": (
            "`lado` saiu de `PedidoDeAjuste` e de `PedidoPorRegime`. Com"
            " `extra=forbid`, manda-lo agora e 422 - e nenhum default de"
            " Python decide por ninguem"
        ),
        "por_que_e_seguro_ter_sido_decidido_depois_de_medir": (
            "as cinco regras andam na MESMA direcao - mais amostra, mais"
            " tarde, mais pessimista, confirma menos facil, menos escolha."
            " Nenhuma delas pode ser usada para fazer um regime passar"
        ),
    }


def _o_piso_de_10() -> dict[str, Any]:
    return {
        "o_que_o_10_e": (
            f"`MINIMO_PARA_O_QUANTIL = {bootstrap.MINIMO_PARA_O_QUANTIL}` e o"
            f" tamanho a partir do qual o bootstrap de quantil EXISTE. Abaixo"
            f" dele `limite_inferior_do_quantil` levanta `SerieCurtaDemais`"
        ),
        "o_que_o_10_NAO_e": (
            "NAO e alvo, NAO e autorizacao para fechar um regime, e NAO"
            " substitui `n_exigido`. Fechar com 10 observacoes so seria"
            " legitimo se `n_exigido` daquele regime fosse 10 por derivacao"
            " de `sigma_e` - e mesmo ai o numero sai da formula, nao do piso"
        ),
        "onde_o_piso_entra": (
            "em `_por_regime`, como `max(n_bruto, MINIMO_PARA_O_QUANTIL)`, e"
            " so para converter observacoes em CALENDARIO. Ele impede a"
            " janela de ser dimensionada para uma medicao que nao acontece;"
            " nao autoriza parar nela"
        ),
        "o_risco_concreto": (
            "quando `sigma_e` e pequeno, a formula devolve menos que 10 e o"
            " piso vence. A duracao derivada fica MUITO curta, e uma janela"
            " curta nao da tempo de os outros regimes aparecerem - ver"
            " `permanencia_e_cobertura`"
        ),
        "formula": "n_exigido = ceil(K * (z * sigma_e / delta)^2)",
        "K": bootstrap.K_QUANTIL_P10,
        "z": bootstrap.Z_95_BILATERAL,
        "delta_mili_bps": ajuste.DELTA_MILI_BPS,
    }


def _permanencia_e_cobertura() -> dict[str, Any]:
    from ...regime import deteccao

    return {
        "janela_causal_barras": deteccao.JANELA_BARRAS,
        "permanencia_barras": deteccao.PERMANENCIA_BARRAS,
        "permanencia_dias": deteccao.PERMANENCIA_BARRAS * 15 / 60 / 24,
        "regimes_minimos": deteccao.REGIMES_MINIMOS,
        "como_eles_interagem_com_o_piso": (
            "sao reguas DIFERENTES e nenhuma domina a outra."
            " `n_exigido` conta OBSERVACOES de um regime;"
            f" `PERMANENCIA_BARRAS = {deteccao.PERMANENCIA_BARRAS}` exige"
            " barras CONSECUTIVAS no mesmo regime para que exista EPISODIO; e"
            f" `REGIMES_MINIMOS = {deteccao.REGIMES_MINIMOS}` exige episodios"
            " de dois regimes distintos"
        ),
        "o_vao_que_isto_revela": (
            "`duracao_da_calibracao_ms` deriva a duracao do regime mais lento"
            " ENTRE OS QUE APARECERAM NO PILOTO, e nao sabe de permanencia nem"
            " de cobertura. Se so um regime apareceu, a duracao sai"
            " dimensionada para ele sozinho - e pode terminar antes de"
            " qualquer outro regime ter um episodio"
        ),
        "consequencia_declarada": (
            "regime que nao acumular o proprio `n_exigido` dentro da janela"
            " permanece NAO CALIBRADO, sem override e sem revalidacao (ADR"
            " 0033 e ADR 0041). Isso e §8.3 ao pe da letra, e nao uma falha -"
            " mas e uma consequencia a aceitar DE OLHOS ABERTOS antes de selar"
        ),
    }


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
        config_version_id=versao.id, regime=pedido.regime,
    )
    try:
        fechamento = ajuste.fechar_regime(
            conn, contrato=pedido.contrato, venue=pedido.venue,
            symbol=pedido.symbol, regime=pedido.regime,
            n_efetivo=conta["n_efetivo"],
            ultimo_t_grid_ms=conta["ultimo_t_grid_ms"],
            por_lado=conta.get("por_lado"),
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

    **E nem `target_hash` nem `execution_id`.** `PedidoPorRegime` herda
    `extra="forbid"`, entao um corpo que os traga responde **422** - nao ha
    como o chamador escolher qual certificacao autoriza a janela. Os dois sao
    DERIVADOS: a identidade sai da finalizacao, o alvo sai de `alvo.montar`
    recalculado agora, e tres gatilhos da migracao 33 recusam a combinacao que
    nao corresponda.

    ## A recusa por certificacao, e por que sao DUAS

    | HTTP | quando | o que fazer |
    |---|---|---|
    | 409 `o_que_falta: certificacao` | os cinco escopos nao tem manifesto que passa para o alvo da identidade calibrada | rodar o passo 9 do runbook |
    | 409 `o_que_falta: identidade_vigente` | alguem criou `config_version` depois da certificacao | a vigente tem de voltar a ser a calibrada |

    Um 409 so, com uma frase generica, mandaria procurar em vez de dizer.
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
    except ajuste.IdentidadeCalibradaNaoCertificada as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "o_que_falta": "certificacao",
                "motivo": str(e),
                "composicao": e.composicao,
                "passo_do_runbook": (
                    "9 - segunda certificacao: laboratorio novo + identidade"
                    " CALIBRADA. Ela vem ANTES de selar qualquer janela de"
                    " revalidacao"
                ),
            },
        ) from e
    except ajuste.IdentidadeVigenteNaoEADaFinalizacao as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "o_que_falta": "identidade_vigente",
                "motivo": str(e),
                "passo_do_runbook": (
                    "a config vigente precisa voltar a ser a que a finalizacao"
                    " criou; qualquer versao posterior muda o alvo, e os"
                    " certificados passam a descrever outro laboratorio"
                ),
            },
        ) from e
    except (ajuste.SemJanelaDeCalibracao, ajuste.NadaACalibrar) as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    return {"janela_de_revalidacao": janela}


@router.post("/ajustar", status_code=status.HTTP_201_CREATED)
def ajustar(request: Request, pedido: PedidoDeAjuste) -> dict[str, Any]:
    """FINALIZA a calibracao: consolida os fechamentos num perfil so.

    **Uma por janela de calibracao.** `UNIQUE (janela_calibracao_id)` em
    `calibracao_versao` garante, e chamar de novo devolve a que existe sem
    escrever. Antes de 2026-10-07 cada chamada criava um perfil novo com a
    lista de overrides daquele momento - e o numero de identidades calibradas,
    logo de recertificacoes, era escolhido por quem chamava.

    Ela so roda depois de o rele ter passado do fim DECLARADO da janela, e vem
    ANTES de selar qualquer revalidacao: a confirmacao confirma este perfil.

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
            settings=request.app.state.settings,
        )
    except ajuste.PilotoAberto as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except ajuste.JanelaDeCalibracaoNaoTerminou as e:
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
            regime=pedido.regime,
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
