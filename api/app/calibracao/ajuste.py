"""O ajuste do simulador. §8.4.1.2 passos 3 a 6, ADR 0027.

As oito garantias que o usuario fixou ao aprovar a 2c, e onde cada uma vive:

| # | garantia | onde |
|---|---|---|
| 1 | piloto ENCERRADO antes de qualquer estimativa | `estimar`, primeira linha |
| 2 | so `E2`, nunca P&L nem desempenho do B3 | guarda de codigo varre este arquivo |
| 3 | δ = 0,5 bps fixo, nao relaxavel | `DELTA_MILI_BPS`, sem parametro |
| 4 | regime sem amostra fica `nao_calibrado` | `estimar_por_regime` |
| 5 | nunca mais otimista | `derivar`, com `max` |
| 6 | versao NOVA, sem alterar run anterior | `aplicar` cria `config_version` |
| 7 | revalidacao selada ANTES do ajuste, uso unico | `aplicar` exige a janela |
| 8 | B1 negativo reexecutado depois | rota, apos `aplicar` |

## O que o ajuste move, e por que so isso

`E2 = p_exec_previsto - ask` mede a distancia entre o que o simulador preve e
o topo de livro **no instante**. Isso e exatamente o que `spread_bps` modela -
a meia distancia que quem atravessa paga.

`slippage_bps` e `penalty_bps` modelam **impacto** e **atraso**, e o topo de
livro nao os observa: o preco do L1 nao diz nada sobre andar o livro. Atribuir
a eles um deficit medido no topo seria afirmar o que a medicao nao viu.

Entao **so `spread_bps` absorve**, e os outros dois ficam intactos. E como o
valor configurado e o spread CHEIO, aplicado pela metade em cada lado (ha
teste do simulador afirmando isso), um deficit de `d` por lado exige `2d` no
campo.

## O ajuste e de UMA DIRECAO SO

Se a config vigente ja for mais pessimista que o alvo, ela e MANTIDA. Nunca se
reduz custo. E a garantia 5, e ela e mais forte que "calibrar": um simulador
pessimista demais reprova coisa boa, o que e caro; um otimista demais aprova
coisa ruim, o que e o unico erro que este projeto nao pode cometer.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from ..aovivo import bbo
from ..config.schema import ExperimentConfig
from ..regime import deteccao
from ..store import bloco_atomico
from . import bootstrap, observacao, piloto

log = logging.getLogger(__name__)

# δ = 0,5 bps por lado, em mili-bps. **FIXO.**
#
# Nao ha parametro, nao ha argumento, nao ha variavel de ambiente. O usuario
# retirou a saida de relaxa-lo ao fechar a D45, e o motivo esta no ADR 0027:
# relaxar "porque nao cabe na reserva" e ajustar a regua ao CALENDARIO, e §8.3
# ja resolveu essa familia - o que nao cabe no horizonte e arquivado, nao
# testado mal.
DELTA_MILI_BPS = 500

# Duracao da janela de revalidacao. Mesma regra do piloto (ADR 0027): o mais
# tarde entre 14 dias e 1.000 observacoes validas. Aqui so a parte de
# calendario, porque a janela e SELADA antes de existir dado nela - selar por
# contagem exigiria esperar o dado, e esperar o dado para escolher a fronteira
# e escolher a fronteira olhando o dado.
DIAS_REVALIDACAO = 14

REGIMES = ("vol_baixa", "vol_media", "vol_alta", "indefinido")


class PilotoAberto(Exception):
    """Nao ha estimativa antes de o piloto encerrar. Garantia 1."""


class RevalidacaoNaoSelada(Exception):
    """A janela de revalidacao tem de ser selada ANTES do ajuste. Garantia 7."""


class RevalidacaoJaConsumida(Exception):
    """Uso unico. Revalidar de novo e repetir o teste ate passar."""


class NadaACalibrar(Exception):
    """Nenhum regime alcancou a amostra. Nao e falha - e o desfecho honesto."""


def _agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class EstimativaDeRegime:
    regime: str
    n: int
    n_efetivo: float
    p10_mili_bps: float | None
    n_necessario: int | None
    estado: str
    motivo: str | None
    spread_bps_x1000: int | None


@dataclass(frozen=True)
class Estimativa:
    de_ms: int
    ate_ms_exclusive: int
    n: int
    n_efetivo: float
    tau: float
    sigma_e: float
    p10_mili_bps: float
    n_necessario: int
    por_regime: list[EstimativaDeRegime]

    @property
    def calibrados(self) -> list[EstimativaDeRegime]:
        return [r for r in self.por_regime if r.estado == "calibrado"]


def regimes_das_barras(
    conn: sqlite3.Connection, *, venue: str, symbol: str, timeframe: str,
) -> dict[int, str | None]:
    """O regime de cada instante do fluxo, pelo detector congelado do ADR 0026.

    **Classificar exige 672 barras ANTERIORES** (7 dias). Enquanto o fluxo nao
    tiver esse historico antes do piloto, todo instante sai `INDEFINIDO` - e
    `indefinido` NAO e um regime: e a declaracao de que ainda nao da para
    dizer. Trata-lo como quarto regime seria calibrar sobre uma classe que nao
    existe na taxonomia.
    """
    barras = [
        (int(r["open_time_ms"]), int(r["close"]), int(r["open"]))
        for r in conn.execute(
            "SELECT open_time_ms, open, close FROM stream_bar"
            " WHERE venue = ? AND symbol = ? AND timeframe = ?"
            " ORDER BY open_time_ms",
            (venue, symbol, timeframe),
        )
    ]
    if len(barras) < 2:
        return {}

    # Retorno que TERMINA em cada barra, em mili-bps, e `None` quando os
    # extremos nao sao temporalmente adjacentes - a subsecao 3b do ADR 0026.
    passo = barras[1][0] - barras[0][0]
    retornos: list[tuple[int, int | None]] = [(barras[0][0], None)]
    for anterior, atual in zip(barras, barras[1:]):
        if atual[0] - anterior[0] != passo:
            retornos.append((atual[0], None))
            continue
        r = (Decimal(atual[1] - anterior[1]) * 10_000 * 1_000
             / Decimal(anterior[1]))
        retornos.append((atual[0], int(r.to_integral_value())))

    return {
        c.open_time_ms: c.regime
        for c in deteccao.classificar_serie(retornos)
    }


def _spread_necessario_x1000(p10_mili_bps: float, config: ExperimentConfig) -> int:
    """O `spread_bps` que este regime pediria, em bps x 1000.

    O alvo da CALIBRACAO e `p10(E2) >= δ` (ponto). O deficit por lado e
    `δ - p10`; como `spread_bps` e o spread CHEIO aplicado pela metade em cada
    lado, o campo sobe `2 x deficit`.

    Deficit negativo (ja passa do alvo) devolve o valor VIGENTE: nunca se
    reduz custo.
    """
    vigente_x1000 = int(Decimal(str(config.spread_bps)) * 1000)
    deficit = DELTA_MILI_BPS - p10_mili_bps
    if deficit <= 0:
        return vigente_x1000
    return vigente_x1000 + int(round(2 * deficit))


def estimar(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    config: ExperimentConfig, config_version_id: int, lado: str = "compra",
) -> Estimativa:
    """A estimativa sobre a janela do piloto. **Exige o piloto ENCERRADO.**

    Garantia 1. Estimar com o piloto aberto seria olhar o resultado antes de o
    periodo declarado terminar, e a janela ainda poderia crescer ate o numero
    ficar agradavel.
    """
    serie_bbo = bbo.Serie(venue=venue, symbol=symbol,
                          price_scale_exp=0, volume_scale_exp=0)
    janela = piloto.ler(conn, serie_bbo, contrato)
    if janela is None:
        raise PilotoAberto(
            "a janela do piloto nao foi fechada. A regra e anterior ao dado "
            "(ADR 0027: o mais tarde entre 14 dias e 1.000 observacoes "
            "validas), e estimar antes dela encerrar deixaria a janela crescer "
            "ate o numero ficar agradavel"
        )

    e2 = observacao.serie_e2(
        conn, contrato=contrato, venue=venue, symbol=symbol, lado=lado,
        config_version_id=config_version_id,
        de_ms=janela.de_ms, ate_ms_exclusive=janela.ate_ms_exclusive,
    )
    if len(e2) < 10:
        raise NadaACalibrar(
            f"{len(e2)} observacoes de E2 na janela do piloto - nao ha o que "
            f"estimar. O piloto fechou por calendario com cobertura baixa"
        )

    serie = [float(v) for v in e2]
    ic = bootstrap.limite_inferior_do_quantil(serie, 0.10)
    sigma = bootstrap.desvio_padrao(serie)
    necessario = bootstrap.tamanho_necessario(
        sigma_e=sigma, delta=float(DELTA_MILI_BPS), tau=ic.tau.tau
    )

    return Estimativa(
        de_ms=janela.de_ms, ate_ms_exclusive=janela.ate_ms_exclusive,
        n=ic.n, n_efetivo=ic.n_efetivo, tau=ic.tau.tau, sigma_e=sigma,
        p10_mili_bps=ic.ponto, n_necessario=necessario.n_efetivo,
        por_regime=estimar_por_regime(
            conn, contrato=contrato, venue=venue, symbol=symbol,
            config=config, config_version_id=config_version_id, lado=lado,
            de_ms=janela.de_ms, ate_ms_exclusive=janela.ate_ms_exclusive,
            n_necessario=necessario.n_efetivo,
        ),
    )


def estimar_por_regime(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    config: ExperimentConfig, config_version_id: int, lado: str,
    de_ms: int, ate_ms_exclusive: int, n_necessario: int,
) -> list[EstimativaDeRegime]:
    """Um resultado por regime. Quem nao alcanca fica `nao_calibrado`.

    Garantia 4, e ela e literal no ADR 0027: **sem herdar parametro de outro
    regime, sem agrupamento, sem interpolacao**. Um regime que nunca acumular
    amostra propria permanece nao calibrado nesta fase, e isso vira condicao
    de validade de todo resultado obtido nele.
    """
    por_regime = regimes_das_barras(
        conn, venue=venue, symbol=symbol, timeframe=config.timeframe
    )

    series: dict[str, list[float]] = {r: [] for r in REGIMES}
    for linha in conn.execute(
        "SELECT t_grid_ms, e2_mili_bps FROM calibracao_observacao"
        " WHERE contrato = ? AND venue = ? AND symbol = ? AND lado = ?"
        "   AND config_version_id = ? AND dentro_do_l1 = 1"
        "   AND t_grid_ms >= ? AND t_grid_ms < ?"
        " ORDER BY t_grid_ms",
        (contrato, venue, symbol, lado, config_version_id,
         de_ms, ate_ms_exclusive),
    ):
        regime = por_regime.get(int(linha["t_grid_ms"]))
        series[regime or "indefinido"].append(float(linha["e2_mili_bps"]))

    saida: list[EstimativaDeRegime] = []
    for regime in REGIMES:
        s = series[regime]

        if regime == "indefinido":
            # NAO e um regime da taxonomia. Ele existe na tabela para que a
            # contagem feche e para que "quantas observacoes nao puderam ser
            # atribuidas" seja visivel - e nao para ser calibrado.
            saida.append(EstimativaDeRegime(
                regime=regime, n=len(s), n_efetivo=float(len(s)),
                p10_mili_bps=None, n_necessario=None,
                estado="nao_calibrado",
                motivo="`indefinido` nao e regime: classificar exige 672 "
                       "barras anteriores (ADR 0026), e o fluxo ainda nao as "
                       "tem antes deste periodo",
                spread_bps_x1000=None,
            ))
            continue

        if len(s) < 10:
            saida.append(EstimativaDeRegime(
                regime=regime, n=len(s), n_efetivo=float(len(s)),
                p10_mili_bps=None, n_necessario=n_necessario,
                estado="nao_calibrado",
                motivo=f"{len(s)} observacoes: nem o bootstrap roda. Sem "
                       f"herdar parametro de outro regime",
                spread_bps_x1000=None,
            ))
            continue

        ic = bootstrap.limite_inferior_do_quantil(s, 0.10)
        if ic.n_efetivo < n_necessario:
            saida.append(EstimativaDeRegime(
                regime=regime, n=ic.n, n_efetivo=ic.n_efetivo,
                p10_mili_bps=ic.ponto, n_necessario=n_necessario,
                estado="nao_calibrado",
                motivo=f"n_efetivo {ic.n_efetivo:.1f} < {n_necessario} "
                       f"necessario para δ = 0,5 bps. **δ NAO e relaxado** - "
                       f"o regime permanece nao calibrado",
                spread_bps_x1000=None,
            ))
            continue

        saida.append(EstimativaDeRegime(
            regime=regime, n=ic.n, n_efetivo=ic.n_efetivo,
            p10_mili_bps=ic.ponto, n_necessario=n_necessario,
            estado="calibrado", motivo=None,
            spread_bps_x1000=_spread_necessario_x1000(ic.ponto, config),
        ))
    return saida


@dataclass(frozen=True)
class Ajuste:
    spread_bps_x1000_antes: int
    spread_bps_x1000_depois: int
    aplicavel: bool
    motivo: str


def derivar(estimativa: Estimativa, config: ExperimentConfig) -> Ajuste:
    """O novo `spread_bps`. **Nunca mais otimista.** Garantia 5.

    Entre os regimes CALIBRADOS toma-se o mais pessimista, e o resultado ainda
    e comparado com a config vigente por `max`. As duas coisas juntas tornam
    impossivel que este caminho reduza custo - e nao por disciplina: nao ha
    ramo que devolva valor menor.
    """
    vigente = int(Decimal(str(config.spread_bps)) * 1000)
    calibrados = estimativa.calibrados

    if not calibrados:
        return Ajuste(
            spread_bps_x1000_antes=vigente,
            spread_bps_x1000_depois=vigente,
            aplicavel=False,
            motivo="nenhum regime alcancou a amostra necessaria para δ = 0,5 "
                   "bps. Nada e aplicado, e cada regime fica registrado como "
                   "nao_calibrado - que vira condicao de validade",
        )

    pedido = max(r.spread_bps_x1000 or vigente for r in calibrados)
    novo = max(vigente, pedido)

    if novo == vigente:
        return Ajuste(
            spread_bps_x1000_antes=vigente,
            spread_bps_x1000_depois=vigente,
            aplicavel=False,
            motivo=f"a config vigente ({vigente / 1000:.3f} bps) ja e ao menos "
                   f"tao pessimista quanto o alvo ({pedido / 1000:.3f} bps). "
                   f"MANTIDA - reduzir custo tornaria o simulador mais "
                   f"otimista, e esse e o unico erro que nao se pode cometer",
        )

    nomes = ", ".join(r.regime for r in calibrados)
    return Ajuste(
        spread_bps_x1000_antes=vigente,
        spread_bps_x1000_depois=novo,
        aplicavel=True,
        motivo=f"spread_bps sobe de {vigente / 1000:.3f} para "
               f"{novo / 1000:.3f} bps, pelo regime mais pessimista entre os "
               f"calibrados ({nomes}). `slippage_bps` e `penalty_bps` ficam "
               f"INTACTOS: eles modelam impacto e atraso, e o topo de livro "
               f"nao os observa",
    )


# ===========================================================================
# A janela de revalidacao: selada ANTES, consumida UMA vez
# ===========================================================================


def selar_revalidacao(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    dias: int = DIAS_REVALIDACAO,
) -> dict:
    """Sela o periodo posterior reservado. Garantia 7, primeira metade.

    **Antes do ajuste, e nao depois.** A fronteira entre "dentro" e "fora" do
    periodo de calibracao e uma data que alguem escolhe, e selar depois de ver
    `p10` seria escolher o periodo que confirma.

    Comeca onde o piloto termina - disjunta por construcao, e o `CHECK` da
    tabela impoe.
    """
    serie_bbo = bbo.Serie(venue=venue, symbol=symbol,
                          price_scale_exp=0, volume_scale_exp=0)
    janela = piloto.ler(conn, serie_bbo, contrato)
    if janela is None:
        raise PilotoAberto(
            "a revalidacao comeca onde o piloto termina, e o piloto ainda nao "
            "fechou"
        )

    ja = ler_revalidacao(conn, contrato=contrato, venue=venue, symbol=symbol)
    if ja is not None:
        return ja

    de = janela.ate_ms_exclusive
    ate = de + dias * piloto.MS_POR_DIA
    with bloco_atomico(conn, "selar_revalidacao"):
        conn.execute(
            "INSERT INTO janela_revalidacao (contrato, venue, symbol, de_ms,"
            " ate_ms_exclusive, piloto_ate_ms_exclusive, selada_em)"
            " VALUES (?,?,?,?,?,?,?)",
            (contrato, venue, symbol, de, ate, janela.ate_ms_exclusive,
             _agora()),
        )
    log.info("calibracao.revalidacao_selada", extra={
        "contrato": contrato, "symbol": symbol, "de_ms": de, "ate_ms": ate,
    })
    return ler_revalidacao(conn, contrato=contrato, venue=venue, symbol=symbol)  # type: ignore[return-value]


def ler_revalidacao(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
) -> dict | None:
    linha = conn.execute(
        "SELECT id, de_ms, ate_ms_exclusive, piloto_ate_ms_exclusive, selada_em"
        "  FROM janela_revalidacao"
        " WHERE contrato = ? AND venue = ? AND symbol = ?",
        (contrato, venue, symbol),
    ).fetchone()
    if linha is None:
        return None
    d = dict(linha)
    uso = conn.execute(
        "SELECT calibracao_versao_id, lb95_mili_bps, p10_mili_bps, n, passou,"
        "       usada_em FROM revalidacao_uso WHERE janela_id = ?",
        (int(linha["id"]),),
    ).fetchone()
    d["consumida"] = uso is not None
    d["uso"] = dict(uso) if uso is not None else None
    return d


# ===========================================================================
# Aplicar
# ===========================================================================


def aplicar(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    config: ExperimentConfig, config_version_id: int, autor: str,
    settings, lado: str = "compra",
) -> dict:
    """Estima, deriva e grava a versao de calibracao.

    **A versao e NOVA** (garantia 6): o ajuste nasce como `config_version`
    nova, e cada run anterior continua apontando para a config sob a qual foi
    aberto. Nenhum resultado ja publicado muda de valor.

    **A revalidacao tem de estar selada** (garantia 7): sem ela, `aplicar`
    recusa. Ajustar sem periodo reservado deixaria a confirmacao para depois,
    e "depois" e quando ja se sabe o que se quer confirmar.
    """
    from ..config import service as config_service

    janela_rev = ler_revalidacao(
        conn, contrato=contrato, venue=venue, symbol=symbol
    )
    if janela_rev is None:
        raise RevalidacaoNaoSelada(
            "sele a janela de revalidacao ANTES de ajustar. §8.4.1.2 exige "
            "periodo posterior reservado, e reserva-lo depois de ver o "
            "resultado e escolher o periodo que confirma"
        )

    estimativa = estimar(
        conn, contrato=contrato, venue=venue, symbol=symbol, config=config,
        config_version_id=config_version_id, lado=lado,
    )
    ajuste = derivar(estimativa, config)

    nova_versao_id: int | None = None
    if ajuste.aplicavel:
        # UMA alteracao, e so ela. `criar_versao` aplica o delta sobre a
        # vigente, entao nenhum outro campo e tocado por engano - e a
        # `config_version` nova e imutavel como todas, o que faz a garantia 6
        # sair de graca: cada run anterior continua apontando para a config
        # sob a qual foi aberto, e nenhum resultado ja publicado muda.
        versao = config_service.criar_versao(
            conn,
            settings,
            {"spread_bps": str(Decimal(ajuste.spread_bps_x1000_depois) / 1000)},
            author=autor,
            note=f"calibracao do simulador (ADR 0027): {ajuste.motivo}",
        )
        nova_versao_id = versao.id

    with bloco_atomico(conn, "calibracao_aplicar"):
        cur = conn.execute(
            "INSERT INTO calibracao_versao ("
            " contrato, venue, symbol, config_version_origem,"
            " config_version_nova, piloto_de_ms, piloto_ate_ms_exclusive,"
            " janela_revalidacao_id, delta_mili_bps, n, n_efetivo_x1000,"
            " tau_x1000, sigma_e_x1000, p10_mili_bps, n_necessario,"
            " aplicado, motivo, criado_em)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (contrato, venue, symbol, config_version_id, nova_versao_id,
             estimativa.de_ms, estimativa.ate_ms_exclusive,
             int(janela_rev["id"]), DELTA_MILI_BPS, estimativa.n,
             int(estimativa.n_efetivo * 1000), int(estimativa.tau * 1000),
             int(estimativa.sigma_e * 1000), int(round(estimativa.p10_mili_bps)),
             estimativa.n_necessario, 1 if ajuste.aplicavel else 0,
             ajuste.motivo, _agora()),
        )
        calibracao_id = int(cur.lastrowid)

        for r in estimativa.por_regime:
            conn.execute(
                "INSERT INTO calibracao_regime ("
                " calibracao_versao_id, regime, n, n_efetivo_x1000,"
                " p10_mili_bps, n_necessario, estado, motivo,"
                " spread_bps_x1000) VALUES (?,?,?,?,?,?,?,?,?)",
                (calibracao_id, r.regime, r.n, int(r.n_efetivo * 1000),
                 None if r.p10_mili_bps is None else int(round(r.p10_mili_bps)),
                 r.n_necessario, r.estado, r.motivo, r.spread_bps_x1000),
            )

    log.info("calibracao.aplicada", extra={
        "calibracao_id": calibracao_id, "aplicado": ajuste.aplicavel,
        "config_version_nova": nova_versao_id,
        "p10_mili_bps": round(estimativa.p10_mili_bps, 1),
        "n": estimativa.n, "n_necessario": estimativa.n_necessario,
    })
    return {
        "calibracao_id": calibracao_id,
        "aplicado": ajuste.aplicavel,
        "config_version_origem": config_version_id,
        "config_version_nova": nova_versao_id,
        "motivo": ajuste.motivo,
        "delta_mili_bps": DELTA_MILI_BPS,
        "estimativa": {
            "de_ms": estimativa.de_ms,
            "ate_ms_exclusive": estimativa.ate_ms_exclusive,
            "n": estimativa.n,
            "n_efetivo": round(estimativa.n_efetivo, 1),
            "tau": round(estimativa.tau, 3),
            "sigma_e_mili_bps": round(estimativa.sigma_e, 1),
            "p10_mili_bps": round(estimativa.p10_mili_bps, 1),
            "n_necessario": estimativa.n_necessario,
        },
        "por_regime": [
            {
                "regime": r.regime, "n": r.n,
                "n_efetivo": round(r.n_efetivo, 1),
                "p10_mili_bps": (
                    None if r.p10_mili_bps is None else round(r.p10_mili_bps, 1)
                ),
                "n_necessario": r.n_necessario,
                "estado": r.estado, "motivo": r.motivo,
                "spread_bps": (
                    None if r.spread_bps_x1000 is None
                    else r.spread_bps_x1000 / 1000
                ),
            }
            for r in estimativa.por_regime
        ],
        "condicao_de_validade": (
            "regimes NAO CALIBRADOS: "
            + ", ".join(
                r.regime for r in estimativa.por_regime
                if r.estado == "nao_calibrado" and r.regime != "indefinido"
            )
            + ". Todo resultado obtido neles carrega isso."
        ),
    }


# ===========================================================================
# Revalidar: o INTERVALO, e nao o ponto
# ===========================================================================


def revalidar(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    config: ExperimentConfig, config_version_id: int,
    calibracao_id: int, lado: str = "compra",
) -> dict:
    """Confirma na janela selada, **sem novo ajuste**. Uso UNICO.

    ## O criterio muda entre as duas janelas, e isso e do ADR 0027

        calibracao   p10(E2) >= δ        o PONTO
        revalidacao  LB95(p10(E2)) >= 0  o LIMITE INFERIOR

    O ponto nao basta aqui. `p10 >= 0` diz que o *melhor palpite* e que o
    modelo e pessimista em 90% dos instantes; `LB95 >= 0` diz que ha **95% de
    confianca** de que ele e. Confirmacao que aceita estimativa pontual nao
    confirma nada.

    E as duas exigencias se encaixam por construcao: a calibracao mira `δ`
    acima de zero justamente porque `δ` e a meia-largura do IC. Sem essa
    folga, a revalidacao reprovaria metade das vezes por RUIDO - e seria δ
    ligando as duas janelas em vez de ser um numero solto.

    ## Uso unico, imposto pela chave primaria

    Revalidar de novo na mesma janela e repetir o teste ate passar. O `PRIMARY
    KEY (janela_id)` de `revalidacao_uso` torna isso impossivel, e nao apenas
    desaconselhado - mesmo desenho do `UNIQUE (hypothesis_id)` do holdout.
    """
    janela = ler_revalidacao(
        conn, contrato=contrato, venue=venue, symbol=symbol
    )
    if janela is None:
        raise RevalidacaoNaoSelada("nao ha janela de revalidacao selada")
    if janela["consumida"]:
        raise RevalidacaoJaConsumida(
            f"a janela {janela['id']} ja foi consumida em "
            f"{janela['uso']['usada_em']}. Revalidar de novo na mesma janela e "
            f"repetir o teste ate passar - o holdout tem a mesma trava"
        )

    e2 = observacao.serie_e2(
        conn, contrato=contrato, venue=venue, symbol=symbol, lado=lado,
        config_version_id=config_version_id,
        de_ms=int(janela["de_ms"]),
        ate_ms_exclusive=int(janela["ate_ms_exclusive"]),
    )
    if len(e2) < 10:
        raise NadaACalibrar(
            f"{len(e2)} observacoes na janela de revalidacao. **A janela NAO e "
            f"consumida** - ela continua selada ate haver dado nela, e "
            f"consumi-la vazia seria gastar a confirmacao sem confirmar nada"
        )

    serie = [float(v) for v in e2]
    ic = bootstrap.limite_inferior_do_quantil(serie, 0.10)
    passou = ic.limite_inferior >= 0

    with bloco_atomico(conn, "revalidacao_uso"):
        conn.execute(
            "INSERT INTO revalidacao_uso (janela_id, calibracao_versao_id,"
            " lb95_mili_bps, p10_mili_bps, n, passou, usada_em)"
            " VALUES (?,?,?,?,?,?,?)",
            (int(janela["id"]), calibracao_id,
             int(round(ic.limite_inferior)), int(round(ic.ponto)),
             ic.n, 1 if passou else 0, _agora()),
        )

    log.info("calibracao.revalidada", extra={
        "janela_id": janela["id"], "calibracao_id": calibracao_id,
        "lb95": round(ic.limite_inferior, 1), "p10": round(ic.ponto, 1),
        "n": ic.n, "passou": passou,
    })
    return {
        "janela_id": janela["id"],
        "de_ms": janela["de_ms"],
        "ate_ms_exclusive": janela["ate_ms_exclusive"],
        "n": ic.n,
        "n_efetivo": round(ic.n_efetivo, 1),
        "p10_mili_bps": round(ic.ponto, 1),
        "limite_inferior_95_mili_bps": round(ic.limite_inferior, 1),
        "criterio": "LB95(p10(E2)) >= 0",
        "passou": passou,
        "sem_novo_ajuste": True,
        "uso_unico": "esta janela nao pode ser revalidada de novo",
    }
