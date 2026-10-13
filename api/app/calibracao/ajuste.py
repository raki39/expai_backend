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
from ..store import bloco_atomico
from . import bootstrap, materializacao, observacao
from .regimes import regimes_das_barras  # noqa: F401  (reexportado)
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


class JanelaDeCalibracaoNaoTerminou(Exception):
    """A finalizacao so acontece depois do fim DECLARADO da janela.

    Consolidar antes seria consolidar sobre serie que ainda cresce - e a
    proxima leitura daria outra lista de overrides, outra `config_version` e
    outra recertificacao.
    """


class RevalidacaoNaoSelada(Exception):
    """A janela de revalidacao tem de ser selada ANTES do ajuste. Garantia 7."""


class RevalidacaoJaConsumida(Exception):
    """Uso unico. Revalidar de novo e repetir o teste ate passar."""


class NadaACalibrar(Exception):
    """Nenhum regime alcancou a amostra. Nao e falha - e o desfecho honesto."""


#: OS DOIS ALVOS DE AMOSTRA SAO IGUAIS, E A IGUALDADE E DECLARADA.
#:
#: Elas medem a mesma grandeza - `E2` daquele regime -, com o mesmo `delta` e
#: o mesmo `sigma_e` do piloto, entao `K(z sigma_e/delta)^2` devolve o mesmo
#: numero. O que nao se pode e INFERIR isso de uma coluna reusada: o dia em que
#: a revalidacao passasse a exigir mais, uma coluna so levaria os dois juntos
#: sem que nada avisasse.
ALVOS_IGUAIS_POR_DECISAO = True
POR_QUE_OS_ALVOS = (
    "os dois saem da MESMA formula `n_efetivo >= K(z sigma_e/delta)^2`, com o"
    " mesmo delta = 500 mili-bps e o mesmo sigma_e medido no piloto - entao"
    " valem o mesmo numero. A igualdade e DECISAO declarada aqui, e nao reuso"
    " silencioso de uma coluna: os dois alvos tem colunas separadas, e um"
    " CHECK impede que esta declaracao minta sobre eles"
)


#: A PRIMEIRA GRADE ADMISSIVEL, EM SQL. Ela e calculada DENTRO do `INSERT`,
#: e nao em Python.
#:
#: Tres razoes, e a terceira e a que obriga:
#:
#: 1. e o relogio do BANCO, que e o mesmo que os gatilhos conferem;
#: 2. nao ha segunda fonte: o numero que entra na coluna e o numero que o
#:    gatilho `..._de_ms_calculado_pelo_banco` recalcula;
#: 3. **nao ha corrida.** SQLite fixa `'now'` para a duracao de um
#:    `sqlite3_step`, entao todas as ocorrencias dentro do mesmo comando -
#:    inclusive as dos gatilhos e as dos `DEFAULT` - veem o MESMO instante.
#:    Com Python lendo o relogio antes e o gatilho lendo depois, uma virada de
#:    grade entre as duas leituras recusaria um selamento legitimo.
#:
#: Os dois `?` sao a grade; o terceiro e o fim da janela anterior.
PRIMEIRA_GRADE_SQL = (
    "MAX((CAST(strftime('%s', 'now') AS INTEGER) * 1000 / ? + 1) * ?, ?)"
)


# `_agora_ms_do_banco()` MORREU AQUI, e a morte e a prova.
#
# Ela lia o relogio do banco em Python para calcular a fronteira. Melhor que o
# relogio do processo, e ainda um SEGUNDO lugar calculando o mesmo numero - e
# com uma corrida: a leitura de Python e a do gatilho sao instantes diferentes.
#
# Com `PRIMEIRA_GRADE_SQL` dentro do `INSERT`, **nenhuma linha de Python decide
# a fronteira**, e nao sobrou para quem esta funcao trabalhar. Mante-la viva
# seria deixar o caminho antigo de pe esperando alguem chama-lo.

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
    # O FIM DECLARADO, e nao o alcance do dado. A janela passou a ter fim
    # (ADR 0041 par.9i): ler ate onde o dado chegou faria a serie crescer entre
    # duas leituras, e era isso que tornava o perfil progressivo.
    ate_ms_exclusive = int(janela_cal["ate_ms_exclusive"])

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
    series: dict[str, list[float]] = {r: [] for r in REGIMES}
    for linha in conn.execute(
        # `finalidade = 'calibracao'` como LITERAL: o mesmo desenho de
        # `acesso = 'agente'` do incremento 9. Nenhum argumento faz este
        # caminho alcancar observacao do piloto.
        #
        # E o REGIME vem da COLUNA, e nao do detector rodado de novo aqui.
        # Rodar o detector na leitura fazia o regime de um instante depender de
        # QUANDO se lia: uma barra nova adiante podia mudar a classificacao de
        # um instante ja medido, e a serie que calibrou deixaria de ser a serie
        # que se le. Gravado, o instante fica com o regime que tinha.
        "SELECT regime, e2_mili_bps FROM calibracao_observacao"
        " WHERE finalidade = 'calibracao'"
        "   AND contrato = ? AND venue = ? AND symbol = ? AND lado = ?"
        "   AND dentro_do_l1 = 1"
        "   AND t_grid_ms >= ? AND t_grid_ms < ?"
        " ORDER BY t_grid_ms",
        (contrato, venue, symbol, lado, de_ms, ate_ms_exclusive),
    ):
        series[str(linha["regime"])].append(float(linha["e2_mili_bps"]))

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

        # A REGUA E O FECHAMENTO, E NAO UM NUMERO RECOMPUTADO AQUI.
        #
        # Esta linha era `if ic.n_efetivo < n_necessario`, com `n_necessario`
        # recalculado do `sigma_e` GLOBAL da janela a cada chamada. Eram DUAS
        # reguas para o mesmo fato: esta, e o `n_exigido` congelado em
        # `calibracao_regime_regra` que `fechar_regime` usa. Duas fontes do
        # mesmo fato divergem, e as consequencias eram as piores possiveis -
        # um regime podia receber override SEM ter fechado, um regime fechado
        # podia NAO receber, e a lista mudava a cada leitura porque
        # `n_necessario` andava com a janela.
        #
        # `n_necessario` continua publicado, como o DIAGNOSTICO que ele e.
        fechou = ler_fechamento(
            conn, contrato=contrato, venue=venue, symbol=symbol, regime=regime
        )
        if fechou is None:
            saida.append(EstimativaDeRegime(
                regime=regime, n=ic.n, n_efetivo=ic.n_efetivo,
                p10_mili_bps=ic.ponto, n_necessario=n_necessario,
                estado="nao_calibrado",
                motivo=f"o regime nao FECHOU: sem linha em"
                       f" `calibracao_regime_fechamento` ele nao alcancou o"
                       f" proprio `n_exigido` congelado. **δ NAO e relaxado**,"
                       f" e o diagnostico desta leitura e"
                       f" n_efetivo {ic.n_efetivo:.1f} contra {n_necessario}",
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
    config: ExperimentConfig,
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

    **Nao recebe data nem grade.** O instante vem do relogio do banco e a
    grade vem do contrato - os dois derivados, nenhum escolhido.
    """
    grade_ms = bbo.ler_contrato(conn, contrato).grade_ms
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

    # O INICIO NAO E CALCULADO AQUI. Ele e calculado no `INSERT`, pelo
    # relogio do banco, com `PRIMEIRA_GRADE_SQL` - e o gatilho
    # `janela_calibracao_de_ms_calculado_pelo_banco` recalcula a mesma conta e
    # recusa qualquer outro valor. Nao ha numero escolhido por este codigo.

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
    # O FIM PRE-DECLARADO, derivado do piloto. `SemHorizonteDerivavel` sobe:
    # sem fracao medida nao ha como transformar `n_bruto` em calendario, e
    # escolher uma duracao seria escolher o numero.
    duracao_cal, conta_do_horizonte = dim.duracao_da_calibracao_ms(
        d, grade_ms=grade_ms
    )

    with bloco_atomico(conn, "selar_calibracao"):
        # `selada_em_ms` e `selada_em` NAO sao nomeados: o `DEFAULT` das duas
        # colunas le o relogio do banco, e no mesmo comando ele e o mesmo
        # instante que `PRIMEIRA_GRADE_SQL` usa.
        conn.execute(
            "INSERT INTO janela_calibracao (contrato, venue, symbol, de_ms,"
            " piloto_ate_ms_exclusive, grade_ms,"
            " confianca_ppm, metodo_bootstrap, tratamento_dependencia, alvo,"
            " regra_encerramento, duracao_minima_ms, delta_mili_bps,"
            " duracao_calibracao_ms, ate_ms_exclusive)"
            f" VALUES (?,?,?,{PRIMEIRA_GRADE_SQL},?,?,?,?,?,?,?,?,?,?,"
            f" {PRIMEIRA_GRADE_SQL} + ?)",
            (contrato, venue, symbol,
             grade_ms, grade_ms, janela_p.ate_ms_exclusive,
             janela_p.ate_ms_exclusive, grade_ms,
             REGRAS_DA_REVALIDACAO["confianca_ppm"],
             REGRAS_DA_REVALIDACAO["metodo_bootstrap"],
             REGRAS_DA_REVALIDACAO["tratamento_dependencia"],
             REGRAS_DA_REVALIDACAO["alvo"],
             REGRAS_DA_REVALIDACAO["regra_encerramento"],
             DIAS_REVALIDACAO * piloto.MS_POR_DIA, DELTA_MILI_BPS,
             duracao_cal,
             grade_ms, grade_ms, janela_p.ate_ms_exclusive, duracao_cal),
        )
        janela_id = int(conn.execute(
            "SELECT id FROM janela_calibracao WHERE contrato = ? AND venue = ?"
            "   AND symbol = ?", (contrato, venue, symbol),
        ).fetchone()[0])
        for r in regras:
            # OS DOIS ALVOS, em colunas separadas, com a igualdade declarada.
            conn.execute(
                "INSERT INTO calibracao_regime_regra (janela_calibracao_id,"
                " regime, n_exigido_calibracao, n_exigido_revalidacao,"
                " alvos_iguais_por_decisao, por_que_os_alvos,"
                " sigma_e_x1000, tau_x1000, piloto_n)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (janela_id, r.regime, r.n_exigido, r.n_exigido,
                 1 if ALVOS_IGUAIS_POR_DECISAO else 0, POR_QUE_OS_ALVOS,
                 int(round((r.sigma_e_mili_bps or 0) * 1000)),
                 int(round((r.tau or 1.0) * 1000)), r.n),
            )
    linha = materializacao.ler_janela_de_calibracao(
        conn, contrato=contrato, venue=venue, symbol=symbol
    )
    assert linha is not None  # acabou de ser gravada
    log.info("calibracao.janela_selada", extra={
        "contrato": contrato, "symbol": symbol,
        "de_ms": int(linha["de_ms"]), "selada_em_ms": int(linha["selada_em_ms"]),
        "ate_ms_exclusive": int(linha["ate_ms_exclusive"]),
    })
    return {**dict(linha), "horizonte_derivado": conta_do_horizonte}


def fechar_regime(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    regime: str, n_efetivo: float, ultimo_t_grid_ms: int,
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

    grade_ms = bbo.ler_contrato(conn, contrato).grade_ms
    regra = conn.execute(
        "SELECT n_exigido_calibracao FROM calibracao_regime_regra"
        " WHERE janela_calibracao_id = ? AND regime = ?",
        (janela_id, regime),
    ).fetchone()
    if regra is None:
        raise RegraNaoCongelada(
            f"o regime {regime!r} nao tem `n_exigido` congelado no selamento."
            f" Sem regra anterior ao dado nao ha parada: fixa-la agora seria"
            f" escolher o tamanho olhando o que ja se mediu"
        )
    n_exigido = int(regra["n_exigido_calibracao"])
    if n_efetivo < n_exigido:
        raise AmostraInsuficienteNoRegime(
            f"n_efetivo {n_efetivo:.1f} < {n_exigido} exigido para o regime"
            f" {regime!r}. **delta NAO e relaxado** e o calendario nao encerra:"
            f" o regime permanece NAO CALIBRADO ate alcancar"
        )

    with bloco_atomico(conn, "fechar_regime"):
        conn.execute(
            "INSERT INTO calibracao_regime_fechamento (janela_calibracao_id,"
            " regime, n_exigido_calibracao, n_efetivo_x1000, fim_ms_exclusive,"
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
        # OS DOIS ALVOS SAEM JUNTOS E NOMEADOS. Publicar so o da calibracao
        # deixaria quem le inferir que o da revalidacao e o mesmo - e inferir
        # e exatamente o que a separacao das colunas existe para nao pedir.
        "SELECT f.id, f.regime, f.n_exigido_calibracao, f.n_efetivo_x1000,"
        "       f.fim_ms_exclusive, f.delta_mili_bps, f.fechado_em,"
        "       r.n_exigido_revalidacao, r.alvos_iguais_por_decisao,"
        "       r.por_que_os_alvos"
        "  FROM calibracao_regime_fechamento f"
        "  JOIN janela_calibracao j ON j.id = f.janela_calibracao_id"
        "  JOIN calibracao_regime_regra r"
        "    ON r.janela_calibracao_id = f.janela_calibracao_id"
        "   AND r.regime = f.regime"
        " WHERE j.contrato = ? AND j.venue = ? AND j.symbol = ? AND f.regime = ?",
        (contrato, venue, symbol, regime),
    ).fetchone()
    if linha is None:
        return None
    d = dict(linha)
    d["alvos_de_amostra"] = {
        "calibracao": int(linha["n_exigido_calibracao"]),
        "revalidacao": int(linha["n_exigido_revalidacao"]),
        "iguais_por_decisao": bool(linha["alvos_iguais_por_decisao"]),
        "por_que": linha["por_que_os_alvos"],
    }
    return d


def selar_revalidacao(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    regime: str,
) -> dict:
    """Sela o periodo posterior reservado DAQUELE regime. Garantia 7.

    **A funcao recebe IDENTIDADE, e nada mais** - serie e regime. Nao recebe
    data, nem `n_exigido`, nem `delta`, nem metodo, nem confianca, nem
    duracao. Tudo isso foi congelado em `janela_calibracao` e
    `calibracao_regime_regra` no selamento da CALIBRACAO, portanto antes do
    primeiro dado dela, e e de la que sai:

    | o que | de onde |
    |---|---|
    | instante do selo | relogio do BANCO |
    | inicio | primeira grade > selo e >= fim da calibracao DELE |
    | duracao | `janela_calibracao.duracao_minima_ms` |
    | `n_exigido` da revalidacao | `calibracao_regime_regra` |
    | confianca, metodo, dependencia, alvo, encerramento | `janela_calibracao` |
    | `delta` | `DELTA_MILI_BPS`, sem parametro |

    Um parametro `dias` aqui - que existia - permitia encurtar a confirmacao no
    momento de sela-la, e encurtar a confirmacao e escolher quanta evidencia
    ela vai exigir. **A janela nasce MECANICA**, e nao ha o que escolher.

    **Antes do ajuste, e nao depois.** Selar depois de ver `p10` seria escolher
    o periodo que confirma.
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
    # A DURACAO E CONGELADA, e vem da janela de calibracao - selada antes do
    # primeiro dado. Nao ha argumento que a encurte.
    duracao = int(janela_cal["duracao_minima_ms"])

    # O INICIO E MECANICO, e o relogio e do SISTEMA - nunca do corpo do
    # pedido. Ele e a primeira grade estritamente posterior ao selamento E
    # posterior ao ultimo instante usado na calibracao, e os dois `CHECK` da
    # tabela impoem as duas condicoes.
    #
    # Dado que ja existia quando a janela foi selada NAO entra: e por isso que
    # a comparacao e com `agora_ms`, e nao com o fim da calibracao apenas.
    # A FRONTEIRA E O FIM DA JANELA, e nao o do regime.
    #
    # O perfil e CONSOLIDADO no fim da janela de calibracao, entao e dali que a
    # confirmacao tem de comecar. Usar o fim do regime deixaria a revalidacao
    # comecar antes de o perfil que ela confirma existir - e o gatilho
    # `janela_revalidacao_fronteiras_conferidas` recusa.
    fim_cal = int(janela_cal["ate_ms_exclusive"])

    with bloco_atomico(conn, "selar_revalidacao"):
        # `de_ms` e `ate_ms_exclusive` saem da MESMA expressao, avaliada duas
        # vezes no mesmo comando - e `'now'` e fixo dentro dele, entao as duas
        # veem o mesmo instante. `ate` e `de + duracao congelada`, e o gatilho
        # `janela_revalidacao_duracao_congelada` recusa qualquer outra.
        conn.execute(
            "INSERT INTO janela_revalidacao (contrato, venue, symbol, regime,"
            " de_ms, ate_ms_exclusive, calibracao_fechamento_id,"
            " calibracao_ate_ms_exclusive, grade_ms)"
            f" VALUES (?,?,?,?,{PRIMEIRA_GRADE_SQL},"
            f" {PRIMEIRA_GRADE_SQL} + ?,?,?,?)",
            (contrato, venue, symbol, regime,
             grade_ms, grade_ms, fim_cal,
             grade_ms, grade_ms, fim_cal, duracao,
             int(fechamento["id"]), fim_cal, grade_ms),
        )
    janela = ler_revalidacao(
        conn, contrato=contrato, venue=venue, symbol=symbol, regime=regime
    )
    assert janela is not None  # acabou de ser gravada
    log.info("calibracao.revalidacao_selada", extra={
        "contrato": contrato, "symbol": symbol, "regime": regime,
        "de_ms": janela["de_ms"], "ate_ms": janela["ate_ms_exclusive"],
    })
    return janela


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

    ## A ORDEM INVERTEU, e a garantia 7 nao foi perdida - foi substituida

    Ate 2026-10-07 esta funcao EXIGIA a revalidacao selada antes, e o motivo
    era bom: a fronteira da janela de revalidacao era escolhivel, e reserva-la
    depois de ver `p10` seria escolher o periodo que confirma.

    **A fronteira deixou de ser escolhivel.** Ela e calculada pelo banco -
    `MAX(primeira grade > agora, fim da janela de calibracao)` -, a duracao vem
    congelada de `janela_calibracao`, e o regime vem do fechamento. Nao sobrou
    nada para escolher, e um gatilho recalcula cada uma das tres.

    Com isso a ordem pode ser a correta: **finalizar o perfil, certificar a
    identidade nova, e so entao reservar o periodo que vai confirma-la.** A
    ordem antiga produzia observacao de revalidacao sob identidade nao
    certificada - era o que o usuario apontou em 2026-10-07.

    ## E ela e A FINALIZACAO, no singular

    `UNIQUE (janela_calibracao_id)` em `calibracao_versao`: **uma** por janela
    de calibracao. Chamar de novo devolve a que existe, sem escrever. Antes,
    cada chamada criava um perfil novo com a lista de overrides daquele
    momento - e o numero de identidades calibradas, logo de recertificacoes,
    era escolhido por quem chamava.
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

    # IDEMPOTENTE. O `UNIQUE (janela_calibracao_id)` ja recusaria a segunda;
    # devolver a que existe e o que faz a rota poder ser chamada de novo sem
    # que alguem precise saber se ela ja rodou.
    ja = ler_finalizacao(conn, contrato=contrato, venue=venue, symbol=symbol)
    if ja is not None:
        return ja

    # O FIM E FATO DO DADO: o watermark do rele tem de ter passado dele.
    # Passar da HORA nao significa que o dado chegou - o backfill pode estar
    # pendente -, e consolidar ali seria consolidar sobre serie incompleta.
    fim = int(janela_cal["ate_ms_exclusive"])
    watermark = conn.execute(
        "SELECT MAX(b.open_time_ms) AS t FROM stream_bar b"
        "  JOIN bbo_contrato c ON c.contrato = ?"
        " WHERE b.venue = ? AND b.symbol = ? AND b.timeframe = c.timeframe",
        (contrato, venue, symbol),
    ).fetchone()["t"]
    if watermark is None or int(watermark) <= fim:
        raise JanelaDeCalibracaoNaoTerminou(
            f"a janela de calibracao termina em {fim} e o rele alcancou"
            f" {watermark}. Consolidar antes de o dado da janela ter chegado"
            f" inteiro seria consolidar sobre serie que ainda cresce, e a"
            f" proxima leitura daria outra lista de overrides"
        )

    estimativa = estimar(
        conn, contrato=contrato, venue=venue, symbol=symbol, config=config,
        config_version_id=config_version_id, lado=lado,
    )
    ajuste = derivar(estimativa, config)

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
        "finalizacao": "UNICA por janela de calibracao, e consolidada",
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


def ler_finalizacao(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
) -> dict | None:
    """A finalizacao consolidada daquela janela de calibracao, se existir.

    `UNIQUE (janela_calibracao_id)` garante que ha no maximo uma - e e por isso
    que esta funcao pode devolver UM dicionario em vez de uma lista.
    """
    linha = conn.execute(
        "SELECT cv.* FROM calibracao_versao cv"
        "  JOIN janela_calibracao j ON j.id = cv.janela_calibracao_id"
        " WHERE j.contrato = ? AND j.venue = ? AND j.symbol = ?",
        (contrato, venue, symbol),
    ).fetchone()
    if linha is None:
        return None
    d = dict(linha)
    perfil = None
    if d.get("config_version_nova") is not None:
        perfil = conn.execute(
            "SELECT calibracao_perfil_hash FROM config_version WHERE id = ?",
            (int(d["config_version_nova"]),),
        ).fetchone()
    return {
        "calibracao_id": int(d["id"]),
        "finalizacao": "UNICA por janela de calibracao, e consolidada",
        "aplicado": bool(d["aplicado"]),
        "config_version_origem": int(d["config_version_origem"]),
        "config_version_nova": d["config_version_nova"],
        "calibracao_perfil_hash": (
            None if perfil is None else perfil["calibracao_perfil_hash"]
        ),
        "motivo": d["motivo"],
        "ja_existia": True,
        "regimes": [
            dict(r) for r in conn.execute(
                "SELECT regime, estado, n, n_efetivo_x1000, p10_mili_bps,"
                "       spread_bps_x1000, motivo FROM calibracao_regime"
                " WHERE calibracao_versao_id = ? ORDER BY regime",
                (int(d["id"]),),
            )
        ],
    }


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
    #
    # E `regime=regime`: a janela e por regime, mas a OBSERVACAO e por
    # instante. Sem o filtro, um instante de `vol_alta` caido dentro da janela
    # de `vol_baixa` confirmaria o override de `vol_baixa` - um regime
    # confirmando outro, que e o que o ADR 0027 proibe ao vedar heranca.
    e2 = observacao.serie_e2_da_revalidacao(
        conn, contrato=contrato, venue=venue, symbol=symbol, lado=lado,
        de_ms=int(janela["de_ms"]),
        ate_ms_exclusive=int(janela["ate_ms_exclusive"]),
        regime=regime,
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
        "regime": regime,
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

    serie: list[float] = []
    # `regime = ?` no SQL, contra a COLUNA gravada. A versao anterior lia a
    # janela inteira e filtrava em Python pelo detector rodado de novo - duas
    # derivacoes do mesmo fato, e a que decide a PARADA era a mais nova.
    for e2 in conn.execute(
        "SELECT e2_mili_bps FROM calibracao_observacao"
        " WHERE finalidade = 'calibracao' AND contrato = ? AND venue = ?"
        "   AND symbol = ? AND lado = ? AND dentro_do_l1 = 1"
        "   AND regime = ? AND t_grid_ms >= ?"
        " ORDER BY t_grid_ms",
        (contrato, venue, symbol, lado, regime, int(janela["de_ms"])),
    ):
        serie.append(float(e2["e2_mili_bps"]))

    # O ULTIMO INSTANTE DO REGIME, e nao o ultimo que ENTROU NA ESTATISTICA.
    #
    # Sao perguntas diferentes, e cada uma tem o seu SELECT. A serie exclui o
    # que passa do tamanho cotado no topo (`dentro_do_l1 = 0`); o ALCANCE da
    # calibracao, nao: aquele instante foi consumido, esta gravado, e a janela
    # de revalidacao tem de comecar depois dele. Usar o ultimo da estatistica
    # deixaria uma observacao deste regime cair dentro da janela reservada
    # para confirma-lo - e o gatilho `janela_revalidacao_sem_dado_preexistente`
    # recusaria o selo, corretamente, sem que a causa fosse obvia.
    linha = conn.execute(
        "SELECT MAX(t_grid_ms) AS t FROM calibracao_observacao"
        " WHERE finalidade = 'calibracao' AND contrato = ? AND venue = ?"
        "   AND symbol = ? AND regime = ? AND t_grid_ms >= ?",
        (contrato, venue, symbol, regime, int(janela["de_ms"])),
    ).fetchone()
    ultimo = int(janela["de_ms"]) if linha["t"] is None else int(linha["t"])

    if len(serie) < 10:
        return {"regime": regime, "n": len(serie), "n_efetivo": float(len(serie)),
                "ultimo_t_grid_ms": ultimo,
                "motivo": "nem o bootstrap roda com menos de dez pontos"}
    ic = bootstrap.limite_inferior_do_quantil(serie, 0.10)
    return {
        "regime": regime, "n": ic.n, "n_efetivo": ic.n_efetivo,
        "tau": ic.tau.tau, "ultimo_t_grid_ms": ultimo, "motivo": None,
    }
