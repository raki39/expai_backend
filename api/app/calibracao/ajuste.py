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
from . import bootstrap, materializacao, observacao
from . import perfil as perfil_mod
from . import piloto

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

#: Os regimes que podem ser calibrados. `indefinido` nao pode ter fechamento,
#: override nem revalidacao - e nao por disciplina: os `CHECK` das tres
#: tabelas recusam.
REGIMES_CALIBRAVEIS = ("vol_baixa", "vol_media", "vol_alta")

#: AS REGRAS DA REVALIDACAO, congeladas no selamento da calibracao - antes,
#: portanto, do primeiro dado dela. O resultado da calibracao pode fornecer o
#: PERFIL a revalidar; ele nao pode mexer em tamanho, alvo, metodo ou duracao.
REGRAS_DA_REVALIDACAO = {
    "confianca_ppm": 950_000,
    "metodo_bootstrap": "estacionario_politis_white",
    "tratamento_dependencia": "hac_bartlett_newey_west",
    "alvo": "lb95_p10_e2_maior_igual_zero",
    "regra_encerramento": "n_efetivo_maior_igual_n_exigido",
}

#: Os CINCO estados que o relatorio mantem separados. Juntar `calibrado` com
#: `revalidado` faria um override sem confirmacao parecer confirmado, e e
#: exatamente a distincao que a D45 existe para preservar.
ESTADOS_DO_REGIME = (
    "nao_calibrado",        # amostra insuficiente, ou regime ausente
    "calibrado",            # ajuste calculado para o regime
    "em_revalidacao",       # confirmacao ainda acumulando
    "revalidado",           # LB95(p10(E2)) >= 0 confirmado
    "falhou_revalidacao",   # a confirmacao reprovou
)


class PilotoAberto(Exception):
    """Nao ha estimativa antes de o piloto encerrar. Garantia 1."""


class RegraNaoCongelada(Exception):
    """O regime nao tem `n_exigido` congelado no selamento da calibracao.

    Fixa-lo agora seria escolher o tamanho olhando o que ja se mediu.
    """


class AmostraInsuficienteNoRegime(Exception):
    """`n_efetivo < n_exigido`. O regime permanece NAO CALIBRADO.

    `delta` nao e relaxado e o calendario nao encerra - a decisao 4 do usuario
    e literal sobre as duas coisas.
    """


class CalibracaoDoRegimeNaoFechada(Exception):
    """O regime ainda nao alcancou o proprio `n_exigido`.

    Decisao 5 do usuario: a parada e POR REGIME, e quem nao alcanca permanece
    nao calibrado. Nao ha revalidacao antes do fechamento daquele regime.
    """


class SemJanelaDeCalibracao(Exception):
    """Nao ha janela de CALIBRACAO selada, e o piloto nao ocupa o lugar dela.

    ADR 0041: do piloto saem `sigma_e`, `tau` e a forma da cauda, e nada mais.
    Calibrar sobre ele seria o vazamento que o ADR 0027 par.5 proibe pela ponta
    que a implementacao anterior deixou aberta.
    """


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
    janela_cal = materializacao.ler_janela_de_calibracao(
        conn, contrato=contrato, venue=venue, symbol=symbol
    )
    if janela_cal is None:
        raise SemJanelaDeCalibracao(
            "nao ha janela de CALIBRACAO selada. O piloto nao ocupa o lugar "
            "dela: do piloto saem `sigma_e`, `tau` e a forma da cauda, e nada "
            "mais (ADR 0027 par.5, ADR 0041)"
        )
    de_ms = int(janela_cal["de_ms"])
    ate_ms_exclusive = _alcance_da_calibracao(
        conn, contrato=contrato, venue=venue, symbol=symbol, de_ms=de_ms
    )

    # `serie_e2_da_calibracao`, com a finalidade LITERAL no SQL. Nao existe
    # argumento que faca este caminho devolver observacao do piloto, e ha
    # guarda por AST conferindo que este modulo nao alcanca a do piloto.
    e2 = observacao.serie_e2_da_calibracao(
        conn, contrato=contrato, venue=venue, symbol=symbol, lado=lado,
        de_ms=de_ms, ate_ms_exclusive=ate_ms_exclusive,
    )
    if len(e2) < 10:
        raise NadaACalibrar(
            f"{len(e2)} observacoes de E2 na janela de CALIBRACAO - nao ha o "
            f"que estimar. A janela foi selada e o periodo ainda nao acumulou"
        )

    serie = [float(v) for v in e2]
    ic = bootstrap.limite_inferior_do_quantil(serie, 0.10)
    sigma = bootstrap.desvio_padrao(serie)
    necessario = bootstrap.tamanho_necessario(
        sigma_e=sigma, delta=float(DELTA_MILI_BPS), tau=ic.tau.tau
    )

    return Estimativa(
        de_ms=de_ms, ate_ms_exclusive=ate_ms_exclusive,
        n=ic.n, n_efetivo=ic.n_efetivo, tau=ic.tau.tau, sigma_e=sigma,
        p10_mili_bps=ic.ponto, n_necessario=necessario.n_efetivo,
        por_regime=estimar_por_regime(
            conn, contrato=contrato, venue=venue, symbol=symbol,
            config=config, config_version_id=config_version_id, lado=lado,
            de_ms=de_ms, ate_ms_exclusive=ate_ms_exclusive,
            n_necessario=necessario.n_efetivo,
        ),
    )


def _alcance_da_calibracao(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    de_ms: int,
) -> int:
    """Ate onde a janela de calibracao TEM dado, e nao ate onde alguem quer.

    A janela nao tem fim declarado - ela fecha POR REGIME, quando cada um
    alcanca o proprio `n_exigido` (decisao 5). Entao o fim da leitura e o
    alcance do dado materializado, derivado e nunca escolhido.
    """
    ultimo = conn.execute(
        "SELECT MAX(t_grid_ms) AS t FROM calibracao_observacao"
        " WHERE finalidade = 'calibracao' AND contrato = ? AND venue = ?"
        "   AND symbol = ? AND t_grid_ms >= ?",
        (contrato, venue, symbol, de_ms),
    ).fetchone()["t"]
    return de_ms if ultimo is None else int(ultimo) + 1


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
        # `finalidade = 'calibracao'` como LITERAL: o mesmo desenho de
        # `acesso = 'agente'` do incremento 9. Nenhum argumento faz este
        # caminho alcancar observacao do piloto.
        "SELECT t_grid_ms, e2_mili_bps FROM calibracao_observacao"
        " WHERE finalidade = 'calibracao'"
        "   AND contrato = ? AND venue = ? AND symbol = ? AND lado = ?"
        "   AND dentro_do_l1 = 1"
        "   AND t_grid_ms >= ? AND t_grid_ms < ?"
        " ORDER BY t_grid_ms",
        (contrato, venue, symbol, lado, de_ms, ate_ms_exclusive),
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
    overrides: list = None  # type: ignore[assignment]


def derivar(estimativa: Estimativa, config: ExperimentConfig) -> Ajuste:
    """Os overrides POR REGIME. **Nunca mais otimista.** Garantias 4 e 5.

    ## O que esta funcao deixou de fazer, e por que

    A primeira versao tomava o **maximo entre os regimes calibrados** e o
    aplicava num campo unico. O usuario a corrigiu, e o argumento e aritmetico
    antes de ser filosofico:

        "Usar o maior valor observado nao significa ser pessimista onde nada
         foi observado."

    O maximo sobre uma amostra de regimes nao e limite superior para os
    regimes fora dela - e `vol_alta` e, por construcao da taxonomia, o tercil
    de MAIOR volatilidade, onde o custo tende a ser pior. Aplicar ali um numero
    medido no tercil mais calmo e otimismo com cara de conservadorismo.

    Hoje cada regime calibrado ganha o **seu** override, e regime sem amostra
    fica **sem linha** - usando a base, que e hipotese declarada e nao medicao
    emprestada.

    A garantia 5 continua valendo, agora POR REGIME: cada override e comparado
    com a base por `max`, entao nenhum deles pode reduzir custo.
    """
    base = int(Decimal(str(config.spread_bps)) * 1000)
    overrides: list[perfil_mod.Override] = []
    for r in estimativa.calibrados:
        pedido = r.spread_bps_x1000 or base
        overrides.append(perfil_mod.Override(
            regime=r.regime,
            spread_bps_x1000=max(base, pedido),
            n=r.n,
            p10_mili_bps=int(round(r.p10_mili_bps or 0)),
        ))

    sem_override = [
        r.regime for r in estimativa.por_regime
        if r.regime != "indefinido" and r.estado != "calibrado"
    ]

    if not overrides:
        return Ajuste(
            spread_bps_x1000_antes=base, spread_bps_x1000_depois=base,
            aplicavel=False, overrides=[],
            motivo="nenhum regime alcancou a amostra necessaria para δ = 0,5 "
                   "bps. Nada e aplicado, e cada regime fica registrado como "
                   "nao_calibrado - que vira condicao de validade",
        )

    subiram = [o for o in overrides if o.spread_bps_x1000 > base]
    if not subiram:
        return Ajuste(
            spread_bps_x1000_antes=base, spread_bps_x1000_depois=base,
            aplicavel=False, overrides=overrides,
            motivo=f"a base ({base / 1000:.3f} bps) ja e ao menos tao "
                   f"pessimista quanto o alvo em todo regime calibrado. "
                   f"MANTIDA - reduzir custo tornaria o simulador mais "
                   f"otimista, e esse e o unico erro que nao se pode cometer",
        )

    detalhe = ", ".join(
        f"{o.regime} -> {o.spread_bps_x1000 / 1000:.3f}" for o in overrides
    )
    resto = (
        f" Sem override, na base de {base / 1000:.3f}: "
        f"{', '.join(sem_override)}." if sem_override else ""
    )
    return Ajuste(
        spread_bps_x1000_antes=base,
        spread_bps_x1000_depois=max(o.spread_bps_x1000 for o in overrides),
        aplicavel=True, overrides=overrides,
        motivo=f"override POR REGIME: {detalhe}.{resto} A base nao muda, e "
               f"`slippage_bps` e `penalty_bps` ficam INTACTOS: eles modelam "
               f"impacto e atraso, e o topo de livro nao os observa",
    )


# ===========================================================================
# A janela de revalidacao: selada ANTES, consumida UMA vez
# ===========================================================================


def selar_calibracao(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    config: ExperimentConfig, agora_ms: int, grade_ms: int,
) -> dict:
    """Sela a janela de CALIBRACAO. Ela comeca DEPOIS do proprio selamento.

    Decisao 3 do usuario (ADR 0041):

    > *"A futura janela de calibracao deve comecar numa fronteira de grade
    > posterior ao seu proprio selamento. Os dados de 18/09 ate esse instante
    > ja existem e ja foram inspecionados; nao entram em calibracao nem em
    > revalidacao."*

    O inicio e a PROXIMA fronteira de grade estritamente depois de agora, e o
    CHECK da tabela impoe as tres coisas: posterior ao piloto, posterior ao
    selamento, e alinhada a grade.

    **Nao ha fim.** Ela fecha por regime, quando cada um alcanca o proprio
    `n_exigido` - e um fim de calendario aqui seria parar porque o calendario
    acabou, que a decisao 4 proibe.

    Idempotente: chamar de novo devolve a que existe. Uma segunda janela seria
    uma segunda chance de escolher a fronteira.
    """
    serie_bbo = bbo.Serie(venue=venue, symbol=symbol,
                          price_scale_exp=0, volume_scale_exp=0)
    janela_p = piloto.ler(conn, serie_bbo, contrato)
    if janela_p is None:
        raise PilotoAberto(
            "a calibracao comeca depois do piloto, e o piloto ainda nao fechou"
        )

    ja = materializacao.ler_janela_de_calibracao(
        conn, contrato=contrato, venue=venue, symbol=symbol
    )
    if ja is not None:
        return dict(ja)

    # A PROXIMA fronteira, estritamente depois de agora - e nunca a corrente: a
    # barra em curso ja comecou, e inclui-la e incluir dado que ja existe.
    proxima = (agora_ms // grade_ms + 1) * grade_ms
    de = max(proxima, janela_p.ate_ms_exclusive)

    # AS REGRAS, ANTES DO PRIMEIRO DADO. `n_exigido` vem do PILOTO - e a
    # unica coisa que sai dele, junto com `tau` e a forma da cauda -, e por
    # isso pode ser fixado agora, sem olhar nenhuma observacao de calibracao.
    from . import dimensionamento as dim

    d = dim.dimensionar(conn, contrato=contrato, venue=venue, symbol=symbol,
                        config=config)
    regras = [
        r for r in d.por_regime
        if r.n_exigido is not None and r.sigma_e_mili_bps is not None
    ]

    with bloco_atomico(conn, "selar_calibracao"):
        conn.execute(
            "INSERT INTO janela_calibracao (contrato, venue, symbol, de_ms,"
            " selada_em_ms, selada_em, piloto_ate_ms_exclusive, grade_ms,"
            " confianca_ppm, metodo_bootstrap, tratamento_dependencia, alvo,"
            " regra_encerramento, duracao_minima_ms, delta_mili_bps)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (contrato, venue, symbol, de, agora_ms, _agora(),
             janela_p.ate_ms_exclusive, grade_ms,
             REGRAS_DA_REVALIDACAO["confianca_ppm"],
             REGRAS_DA_REVALIDACAO["metodo_bootstrap"],
             REGRAS_DA_REVALIDACAO["tratamento_dependencia"],
             REGRAS_DA_REVALIDACAO["alvo"],
             REGRAS_DA_REVALIDACAO["regra_encerramento"],
             DIAS_REVALIDACAO * piloto.MS_POR_DIA, DELTA_MILI_BPS),
        )
        janela_id = int(conn.execute(
            "SELECT id FROM janela_calibracao WHERE contrato = ? AND venue = ?"
            "   AND symbol = ?", (contrato, venue, symbol),
        ).fetchone()[0])
        for r in regras:
            conn.execute(
                "INSERT INTO calibracao_regime_regra (janela_calibracao_id,"
                " regime, n_exigido, sigma_e_x1000, tau_x1000, piloto_n)"
                " VALUES (?,?,?,?,?,?)",
                (janela_id, r.regime, r.n_exigido,
                 int(round((r.sigma_e_mili_bps or 0) * 1000)),
                 int(round((r.tau or 1.0) * 1000)), r.n),
            )
    log.info("calibracao.janela_selada", extra={
        "contrato": contrato, "symbol": symbol, "de_ms": de,
        "selada_em_ms": agora_ms,
    })
    linha = materializacao.ler_janela_de_calibracao(
        conn, contrato=contrato, venue=venue, symbol=symbol
    )
    assert linha is not None  # acabou de ser gravada
    return dict(linha)


def fechar_regime(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    regime: str, n_efetivo: float, ultimo_t_grid_ms: int, grade_ms: int,
) -> dict:
    """Fecha a calibracao DE UM regime, quando ele alcanca o proprio `n_exigido`.

    **Idempotente:** chamar de novo devolve o fechamento que existe. Um segundo
    fechamento seria uma segunda chance de escolher onde parar.

    A parada e por regime (decisao 5), `delta` nao se move, e calendario nao
    entra: o unico criterio e `n_efetivo >= n_exigido`, com o `n_exigido`
    congelado no selamento e lido de `calibracao_regime_regra`.
    """
    if regime not in REGIMES_CALIBRAVEIS:
        raise ValueError(
            f"{regime!r} nao e regime calibravel; `indefinido` nao conta"
        )
    janela = materializacao.ler_janela_de_calibracao(
        conn, contrato=contrato, venue=venue, symbol=symbol
    )
    if janela is None:
        raise SemJanelaDeCalibracao(
            "nao ha janela de calibracao selada: nao ha o que fechar"
        )
    janela_id = int(janela["id"])

    ja = ler_fechamento(conn, contrato=contrato, venue=venue, symbol=symbol,
                        regime=regime)
    if ja is not None:
        return ja

    regra = conn.execute(
        "SELECT n_exigido FROM calibracao_regime_regra"
        " WHERE janela_calibracao_id = ? AND regime = ?",
        (janela_id, regime),
    ).fetchone()
    if regra is None:
        raise RegraNaoCongelada(
            f"o regime {regime!r} nao tem `n_exigido` congelado no selamento."
            f" Sem regra anterior ao dado nao ha parada: fixa-la agora seria"
            f" escolher o tamanho olhando o que ja se mediu"
        )
    n_exigido = int(regra["n_exigido"])
    if n_efetivo < n_exigido:
        raise AmostraInsuficienteNoRegime(
            f"n_efetivo {n_efetivo:.1f} < {n_exigido} exigido para o regime"
            f" {regime!r}. **delta NAO e relaxado** e o calendario nao encerra:"
            f" o regime permanece NAO CALIBRADO ate alcancar"
        )

    with bloco_atomico(conn, "fechar_regime"):
        conn.execute(
            "INSERT INTO calibracao_regime_fechamento (janela_calibracao_id,"
            " regime, n_exigido, n_efetivo_x1000, fim_ms_exclusive,"
            " delta_mili_bps, fechado_em) VALUES (?,?,?,?,?,?,?)",
            (janela_id, regime, n_exigido, int(round(n_efetivo * 1000)),
             ultimo_t_grid_ms + grade_ms, DELTA_MILI_BPS, _agora()),
        )
    log.info("calibracao.regime_fechado", extra={
        "contrato": contrato, "symbol": symbol, "regime": regime,
        "n_exigido": n_exigido, "n_efetivo": n_efetivo,
    })
    return ler_fechamento(  # type: ignore[return-value]
        conn, contrato=contrato, venue=venue, symbol=symbol, regime=regime
    )


def ler_fechamento(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    regime: str,
) -> dict | None:
    linha = conn.execute(
        "SELECT f.id, f.regime, f.n_exigido, f.n_efetivo_x1000,"
        "       f.fim_ms_exclusive, f.delta_mili_bps, f.fechado_em"
        "  FROM calibracao_regime_fechamento f"
        "  JOIN janela_calibracao j ON j.id = f.janela_calibracao_id"
        " WHERE j.contrato = ? AND j.venue = ? AND j.symbol = ? AND f.regime = ?",
        (contrato, venue, symbol, regime),
    ).fetchone()
    return None if linha is None else dict(linha)


def selar_revalidacao(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    regime: str, agora_ms: int, dias: int | None = None,
) -> dict:
    """Sela o periodo posterior reservado. Garantia 7, primeira metade.

    **Antes do ajuste, e nao depois.** A fronteira entre "dentro" e "fora" do
    periodo de calibracao e uma data que alguem escolhe, e selar depois de ver
    `p10` seria escolher o periodo que confirma.

    Comeca onde o piloto termina - disjunta por construcao, e o `CHECK` da
    tabela impoe.
    """
    if regime not in REGIMES or regime == "indefinido":
        raise ValueError(
            f"{regime!r} nao e regime da taxonomia; `indefinido` nao conta"
        )
    fechamento = conn.execute(
        "SELECT f.id, f.fim_ms_exclusive FROM calibracao_regime_fechamento f"
        "  JOIN janela_calibracao j ON j.id = f.janela_calibracao_id"
        " WHERE j.contrato = ? AND j.venue = ? AND j.symbol = ?"
        "   AND f.regime = ?",
        (contrato, venue, symbol, regime),
    ).fetchone()
    if fechamento is None:
        raise CalibracaoDoRegimeNaoFechada(
            f"o regime {regime!r} ainda nao alcancou o proprio `n_exigido`. A"
            f" revalidacao dele comeca onde a calibracao DELE termina, e sem o"
            f" fechamento nao ha fronteira - selar antes seria escolher o"
            f" periodo que confirma"
        )

    ja = ler_revalidacao(conn, contrato=contrato, venue=venue, symbol=symbol,
                         regime=regime)
    if ja is not None:
        return ja

    janela_cal = materializacao.ler_janela_de_calibracao(
        conn, contrato=contrato, venue=venue, symbol=symbol
    )
    assert janela_cal is not None  # o fechamento so existe com ela
    grade_ms = int(janela_cal["grade_ms"])
    duracao = (
        int(janela_cal["duracao_minima_ms"]) if dias is None
        else dias * piloto.MS_POR_DIA
    )

    # O INICIO E MECANICO, e o relogio e do SISTEMA - nunca do corpo do
    # pedido. Ele e a primeira grade estritamente posterior ao selamento E
    # posterior ao ultimo instante usado na calibracao, e os dois `CHECK` da
    # tabela impoem as duas condicoes.
    #
    # Dado que ja existia quando a janela foi selada NAO entra: e por isso que
    # a comparacao e com `agora_ms`, e nao com o fim da calibracao apenas.
    fim_cal = int(fechamento["fim_ms_exclusive"])
    proxima = (agora_ms // grade_ms + 1) * grade_ms
    de = max(proxima, fim_cal)
    ate = de + duracao

    with bloco_atomico(conn, "selar_revalidacao"):
        conn.execute(
            "INSERT INTO janela_revalidacao (contrato, venue, symbol, regime,"
            " de_ms, ate_ms_exclusive, calibracao_fechamento_id,"
            " calibracao_ate_ms_exclusive, selada_em_ms, selada_em, grade_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (contrato, venue, symbol, regime, de, ate,
             int(fechamento["id"]), fim_cal, agora_ms, _agora(), grade_ms),
        )
    log.info("calibracao.revalidacao_selada", extra={
        "contrato": contrato, "symbol": symbol, "regime": regime,
        "de_ms": de, "ate_ms": ate,
    })
    return ler_revalidacao(  # type: ignore[return-value]
        conn, contrato=contrato, venue=venue, symbol=symbol, regime=regime
    )


def ler_revalidacao(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    regime: str,
) -> dict | None:
    """A janela de revalidacao DAQUELE regime. O `regime` nao tem default.

    Um default faria "a revalidacao" voltar a ser uma so, e ela e por regime
    desde o ADR 0041 - cada regime fecha a calibracao em instante proprio.
    """
    # `SELECT *`: a lista escrita a mao envelheceu no primeiro campo novo -
    # `selada_em_ms` e `grade_ms` entraram com o ADR 0041 e ficaram de fora.
    linha = conn.execute(
        "SELECT * FROM janela_revalidacao"
        " WHERE contrato = ? AND venue = ? AND symbol = ? AND regime = ?",
        (contrato, venue, symbol, regime),
    ).fetchone()
    if linha is None:
        return None
    d = dict(linha)
    # O VAO entre o fim da calibracao e o inicio da revalidacao fica FORA das
    # duas, e vai DECLARADO. Ele nasce porque a revalidacao comeca no futuro:
    # o que aconteceu entre o fechamento e o selo ja existia quando a janela
    # foi selada, e um periodo ja visto nao confirma nada.
    d["excluido"] = {
        "de_ms": int(linha["calibracao_ate_ms_exclusive"]),
        "ate_ms_exclusive": int(linha["de_ms"]),
        "instantes": max(
            0,
            (int(linha["de_ms"]) - int(linha["calibracao_ate_ms_exclusive"]))
            // int(linha["grade_ms"]),
        ),
        "por_que": (
            "esta entre o ultimo instante usado na calibracao e o inicio"
            " FUTURO da revalidacao. Ja existia no selamento, entao nao entra"
            " em nenhuma das duas - e fica declarado em vez de sumir"
        ),
    }
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

    janela_cal = materializacao.ler_janela_de_calibracao(
        conn, contrato=contrato, venue=venue, symbol=symbol
    )
    if janela_cal is None:
        raise SemJanelaDeCalibracao(
            "nao ha janela de CALIBRACAO selada: o piloto nao ocupa o lugar "
            "dela (ADR 0041)"
        )

    estimativa = estimar(
        conn, contrato=contrato, venue=venue, symbol=symbol, config=config,
        config_version_id=config_version_id, lado=lado,
    )
    ajuste = derivar(estimativa, config)

    # GARANTIA 7, agora POR REGIME. A revalidacao de cada regime tem de estar
    # selada ANTES de o override dele ser aplicado - e ela so pode ser selada
    # depois de aquele regime fechar a propria calibracao. A ordem inteira fica
    # imposta: fecha o regime -> sela a revalidacao dele -> aplica o override.
    #
    # Exigir uma revalidacao GLOBAL, como antes, era mais fraco e mais frouxo
    # ao mesmo tempo: uma janela so nao confirma tres regimes que fecharam em
    # instantes diferentes.
    faltando = [
        o.regime for o in (ajuste.overrides or [])
        if ler_revalidacao(conn, contrato=contrato, venue=venue,
                           symbol=symbol, regime=o.regime) is None
    ]
    if faltando:
        raise RevalidacaoNaoSelada(
            f"sele a janela de revalidacao ANTES de ajustar, e ela e POR "
            f"REGIME: faltam {faltando}. §8.4.1.2 exige periodo posterior "
            f"reservado, e reserva-lo depois de ver o resultado e escolher o "
            f"periodo que confirma"
        )

    nova_versao_id: int | None = None
    perfil_hash: str | None = None
    if ajuste.aplicavel:
        # O PERFIL, e nao o `spread_bps` da config (ADR 0033).
        #
        # A base fica INTOCADA: ela e a hipotese original, e continua
        # descrevendo o que descrevia. O que nasce e um perfil imutavel com
        # override por regime, e a `config_version` nova o referencia POR
        # HASH - numa coluna, fora do payload, para que o `config_hash` das
        # versoes ja gravadas fique exatamente como sempre foi.
        p = perfil_mod.gravar(
            conn,
            spread_bps_base_x1000=ajuste.spread_bps_x1000_antes,
            overrides=list(ajuste.overrides or []),
        )
        perfil_hash = p.hash
        versao = config_service.criar_versao(
            conn, settings, {}, author=autor,
            note=f"calibracao por regime (ADR 0033): {ajuste.motivo}",
            calibracao_perfil_hash=perfil_hash,
        )
        nova_versao_id = versao.id

    with bloco_atomico(conn, "calibracao_aplicar"):
        cur = conn.execute(
            # As colunas dizem CALIBRACAO, e nao piloto. Elas se chamavam
            # `piloto_*` porque a estimativa rodava sobre o piloto; manter o
            # nome guardando a janela de calibracao seria o mesmo defeito com
            # outra roupa (ADR 0041 par.5 da migracao 33).
            "INSERT INTO calibracao_versao ("
            " contrato, venue, symbol, config_version_origem,"
            " config_version_nova, janela_calibracao_id, calibracao_de_ms,"
            " calibracao_ate_ms_exclusive, delta_mili_bps, n,"
            " n_efetivo_x1000, tau_x1000, sigma_e_x1000, p10_mili_bps,"
            " n_necessario, aplicado, motivo, criado_em)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (contrato, venue, symbol, config_version_id, nova_versao_id,
             int(janela_cal["id"]),
             estimativa.de_ms, estimativa.ate_ms_exclusive,
             DELTA_MILI_BPS, estimativa.n,
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
        "calibracao_perfil_hash": perfil_hash,
        "spread_bps_base": ajuste.spread_bps_x1000_antes / 1000,
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
    calibracao_id: int, regime: str, lado: str = "compra",
) -> dict:
    """Confirma na janela DAQUELE regime, **sem novo ajuste**. Uso UNICO.

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
    if regime not in REGIMES_CALIBRAVEIS:
        raise ValueError(f"{regime!r} nao e regime calibravel")
    janela = ler_revalidacao(
        conn, contrato=contrato, venue=venue, symbol=symbol, regime=regime
    )
    if janela is None:
        raise RevalidacaoNaoSelada(
            f"nao ha janela de revalidacao selada para o regime {regime!r}"
        )
    if janela["consumida"]:
        raise RevalidacaoJaConsumida(
            f"a janela {janela['id']} ja foi consumida em "
            f"{janela['uso']['usada_em']}. Revalidar de novo na mesma janela e "
            f"repetir o teste ate passar - o holdout tem a mesma trava"
        )

    # `serie_e2_da_revalidacao`, com a finalidade LITERAL. Nenhuma observacao
    # pode pertencer a calibracao E a revalidacao do mesmo regime: um instante
    # tem UMA linha, com UMA finalidade - a chave primaria garante.
    e2 = observacao.serie_e2_da_revalidacao(
        conn, contrato=contrato, venue=venue, symbol=symbol, lado=lado,
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


# ===========================================================================
# OS CINCO ESTADOS, separados
#
# Juntar `calibrado` com `revalidado` faria um override sem confirmacao
# parecer confirmado - e a D45 existe justamente para que as duas coisas nao se
# confundam. Um regime calibrado que nao conclua a revalidacao permanece
# INCONCLUSIVO, e o override dele nao ganha validade.
# ===========================================================================


def estado_por_regime(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
) -> dict:
    """O estado de cada regime calibravel, derivado do banco.

    `indefinido` nao aparece: ele nao e regime, e nao pode ter fechamento,
    override nem revalidacao - os `CHECK` das tres tabelas recusam.
    """
    saida = {}
    for regime in REGIMES_CALIBRAVEIS:
        fechamento = ler_fechamento(
            conn, contrato=contrato, venue=venue, symbol=symbol, regime=regime
        )
        if fechamento is None:
            saida[regime] = {
                "estado": "nao_calibrado",
                "por_que": (
                    "o regime nao alcancou o proprio `n_exigido`, ou nao tem"
                    " amostra nenhuma. `delta` nao e relaxado e o calendario"
                    " nao encerra"
                ),
                "override_vale": False,
            }
            continue

        janela = ler_revalidacao(
            conn, contrato=contrato, venue=venue, symbol=symbol, regime=regime
        )
        if janela is None:
            saida[regime] = {
                "estado": "calibrado",
                "fechamento": fechamento,
                "por_que": (
                    "o ajuste foi calculado para o regime e a revalidacao"
                    " ainda nao foi selada. O override existe e NAO tem"
                    " validade confirmada"
                ),
                "override_vale": False,
            }
            continue

        uso = janela.get("uso")
        if uso is None:
            saida[regime] = {
                "estado": "em_revalidacao",
                "fechamento": fechamento,
                "janela": janela,
                "por_que": "a confirmacao esta acumulando",
                "override_vale": False,
            }
            continue

        passou = bool(uso["passou"])
        saida[regime] = {
            "estado": "revalidado" if passou else "falhou_revalidacao",
            "fechamento": fechamento,
            "janela": janela,
            "por_que": (
                "LB95(p10(E2)) >= 0 confirmado" if passou
                else "a confirmacao reprovou: LB95(p10(E2)) < 0"
            ),
            "override_vale": passou,
        }

    revalidados = [r for r, v in saida.items() if v["estado"] == "revalidado"]
    return {
        "por_regime": saida,
        "revalidados": sorted(revalidados),
        "cobertura_minima": 2,
        "cobertura_cumprida": len(revalidados) >= 2,
        "o_que_a_cobertura_MEDE": (
            "regimes REVALIDADOS com sucesso, e nao apenas calibrados. Ela e"
            " gate da conclusao geral da fase - e nao bloqueia o trabalho"
            " individual de um regime: `vol_baixa` pode fechar, selar e"
            " revalidar sozinho"
        ),
        "indefinido_nao_aparece": (
            "`indefinido` nao e regime: nao pode ter fechamento, override nem"
            " revalidacao, e os CHECK das tres tabelas recusam"
        ),
        "estados_possiveis": list(ESTADOS_DO_REGIME),
    }


def medir_regime(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    config: ExperimentConfig, config_version_id: int, regime: str,
    lado: str = "compra",
) -> dict:
    """`n_efetivo` do regime na janela de CALIBRACAO. **Medido, nunca recebido.**

    A rota de fechamento nao aceita `n_efetivo` no corpo: um numero vindo de
    quem pede seria a parada escolhida por quem pede. Aqui ele sai do dado.
    """
    if regime not in REGIMES_CALIBRAVEIS:
        raise ValueError(f"{regime!r} nao e regime calibravel")
    janela = materializacao.ler_janela_de_calibracao(
        conn, contrato=contrato, venue=venue, symbol=symbol
    )
    if janela is None:
        raise SemJanelaDeCalibracao("nao ha janela de calibracao selada")

    por_instante = regimes_das_barras(
        conn, venue=venue, symbol=symbol, timeframe=config.timeframe
    )
    serie: list[float] = []
    ultimo = int(janela["de_ms"])
    for t, e2 in conn.execute(
        "SELECT t_grid_ms, e2_mili_bps FROM calibracao_observacao"
        " WHERE finalidade = 'calibracao' AND contrato = ? AND venue = ?"
        "   AND symbol = ? AND lado = ? AND dentro_do_l1 = 1"
        "   AND t_grid_ms >= ?"
        " ORDER BY t_grid_ms",
        (contrato, venue, symbol, lado, int(janela["de_ms"])),
    ):
        if por_instante.get(int(t)) == regime:
            serie.append(float(e2))
            ultimo = int(t)

    if len(serie) < 10:
        return {"regime": regime, "n": len(serie), "n_efetivo": float(len(serie)),
                "ultimo_t_grid_ms": ultimo,
                "motivo": "nem o bootstrap roda com menos de dez pontos"}
    ic = bootstrap.limite_inferior_do_quantil(serie, 0.10)
    return {
        "regime": regime, "n": ic.n, "n_efetivo": ic.n_efetivo,
        "tau": ic.tau.tau, "ultimo_t_grid_ms": ultimo, "motivo": None,
    }
