"""O que sai do PILOTO, e nada mais. ADR 0041, e o ADR 0027 par.5 ao pe da letra.

> - **Saidas, e so elas:** `sigma_e`, `tau`, e a forma da cauda. Nada mais sai
>   do piloto.
> - **Os dados do piloto ficam fora da calibracao E da revalidacao**, de
>   **todos** os regimes.

## Por que este modulo existe separado de `ajuste.py`

Porque a separacao e o que torna a regra estrutural. Enquanto a estimativa do
piloto morava dentro do modulo que calibra, bastava uma linha para o `p10` dela
virar override - e foi exatamente isso que aconteceu: `ajuste.estimar` rodava
na janela do piloto e `ajuste.derivar` transformava o resultado em
`spread_bps`.

Aqui nao ha para onde ir:

| garantia | forma |
|---|---|
| nao produz override | `Dimensionamento` **nao tem campo de `spread_bps`** |
| nao grava perfil | este modulo **nao importa** `perfil`, e ha guarda por AST |
| nao grava nada | nenhuma funcao daqui escreve; sao todas leitura |
| le so o piloto | usa `serie_e2_do_piloto`, com a finalidade LITERAL no SQL |

**O `p10` continua sendo calculado e publicado - como DIAGNOSTICO.** Retira-lo
seria esconder o numero que diz se o simulador esta pessimista; o que ele nao
pode e virar parametro. O campo se chama `p10_diagnostico_mili_bps` para que
ninguem precise saber disso de cor.

## `n_exigido`, e delta nao se move

    n_exigido = ceil( K * (z * sigma_e / delta)^2 )      K = 2,92 · z = 1,96
    n_bruto   = ceil( n_exigido * tau )

`delta` e `DELTA_MILI_BPS` de `ajuste`, importado e nunca copiado: dois
numeros iguais em modulos diferentes divergem, e este e o numero que a D45
proibiu relaxar.

**Calendario nao entra.** A funcao nao conhece data nenhuma, e e assim que ela
fica incapaz de negociar consigo mesma - "parar porque o calendario acabou" nao
tem onde ser escrito.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field

from ..aovivo import bbo
from ..config.schema import ExperimentConfig
from . import bootstrap, observacao, piloto

#: Os regimes da taxonomia (ADR 0026). `indefinido` **nao** esta aqui: ele nao
#: e regime, e a decisao 5 do usuario e literal - "`indefinido` nao conta".
REGIMES_QUE_CONTAM = ("vol_baixa", "vol_media", "vol_alta")

#: Quantis publicados como "forma da cauda". A esquerda e densa de proposito:
#: o alvo do ADR 0027 e `p10 >= 0`, e e a cauda inferior que decide.
QUANTIS = (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)


class PilotoNaoMaterializado(Exception):
    """Ha janela fechada e NAO ha serie E2. Fechar a janela nao mede nada."""


@dataclass(frozen=True)
class PorRegime:
    regime: str
    n: int
    n_efetivo: float
    sigma_e_mili_bps: float | None
    tau: float | None
    n_exigido: int | None
    n_bruto_exigido: int | None
    motivo: str | None


@dataclass(frozen=True)
class Dimensionamento:
    """As tres saidas do piloto, e o `n` que elas dimensionam.

    **Nao ha campo de `spread_bps` aqui, e a ausencia e a garantia.** Um
    dimensionamento nao vira parametro por engano quando o tipo nao tem onde
    guardar o parametro.
    """

    de_ms: int
    ate_ms_exclusive: int
    janela_hash: str
    lado: str
    n: int
    n_efetivo: float
    sigma_e_mili_bps: float
    tau: float
    tau_bruto: float
    ar1_plausivel: bool
    bloco: int
    forma_da_cauda: dict
    p10_diagnostico_mili_bps: float
    lb95_diagnostico_mili_bps: float
    delta_mili_bps: int
    n_exigido: int
    n_bruto_exigido: int
    por_regime: tuple[PorRegime, ...] = field(default_factory=tuple)

    def como_dict(self) -> dict:
        return {
            "janela_do_piloto": {
                "de_ms": self.de_ms,
                "ate_ms_exclusive": self.ate_ms_exclusive,
                "hash": self.janela_hash,
            },
            "lado": self.lado,
            "amostra": {
                "n": self.n,
                "n_efetivo": round(self.n_efetivo, 1),
                "bloco": self.bloco,
            },
            "sigma_e_mili_bps": round(self.sigma_e_mili_bps, 3),
            "dependencia": {
                "tau": round(self.tau, 4),
                "tau_bruto": round(self.tau_bruto, 4),
                "ar1_plausivel": self.ar1_plausivel,
                "o_que_e": (
                    "`tau` por HAC (Bartlett/Newey-West). A forma AR(1) so"
                    " aparece no dimensionamento, e a hipotese vai declarada"
                    " ao lado - ADR 0027 par.4"
                ),
            },
            "forma_da_cauda": self.forma_da_cauda,
            "diagnostico": {
                "p10_mili_bps": round(self.p10_diagnostico_mili_bps, 1),
                "lb95_mili_bps": round(self.lb95_diagnostico_mili_bps, 1),
                "pessimista": self.lb95_diagnostico_mili_bps >= 0,
                "o_que_isto_NAO_e": (
                    "NAO aprova o simulador e NAO produz override de"
                    " parametro. Do piloto saem `sigma_e`, `tau` e a forma da"
                    " cauda (ADR 0027 par.5); quem calibra le a janela de"
                    " CALIBRACAO, que e posterior e disjunta"
                ),
            },
            "dimensionamento": {
                "delta_mili_bps": self.delta_mili_bps,
                "delta_nao_e_relaxavel": (
                    "fixo em 0,5 bps por lado. A D45 retirou a saida de"
                    " relaxa-lo, e o calendario nao e criterio de parada"
                ),
                "formula": "n_exigido = ceil(K * (z * sigma_e / delta)^2)",
                "K": bootstrap.K_QUANTIL_P10,
                "z": bootstrap.Z_95_BILATERAL,
                "n_exigido": self.n_exigido,
                "n_bruto_exigido": self.n_bruto_exigido,
            },
            "por_regime": [
                {
                    "regime": r.regime,
                    "n": r.n,
                    "n_efetivo": round(r.n_efetivo, 1),
                    "sigma_e_mili_bps": (
                        None if r.sigma_e_mili_bps is None
                        else round(r.sigma_e_mili_bps, 3)
                    ),
                    "tau": None if r.tau is None else round(r.tau, 4),
                    "n_exigido": r.n_exigido,
                    "n_bruto_exigido": r.n_bruto_exigido,
                    "motivo": r.motivo,
                }
                for r in self.por_regime
            ],
            "indefinido_nao_conta": (
                "`indefinido` nao aparece em `por_regime` porque nao e regime:"
                " e a declaracao de que a janela causal de 672 barras ainda"
                " nao existe antes daquele instante (ADR 0026)"
            ),
        }


def _forma_da_cauda(serie: list[float]) -> dict:
    """Assimetria, curtose BRUTA e os quantis. A esquerda e a que decide."""
    n = len(serie)
    media = sum(serie) / n
    dp = statistics.stdev(serie) if n > 1 else 0.0
    if dp > 0:
        z = [(v - media) / dp for v in serie]
        assimetria = sum(t ** 3 for t in z) / n
        curtose = sum(t ** 4 for t in z) / n
    else:
        assimetria = curtose = 0.0
    return {
        "media_mili_bps": round(media, 2),
        "assimetria": round(assimetria, 4),
        "curtose_bruta": round(curtose, 4),
        "curtose_e_BRUTA": "normal = 3, e nao 0. Trocar de convencao no meio e"
                           " o modo de falha silencioso desta familia",
        "quantis_mili_bps": {
            f"p{int(p * 100):02d}": round(bootstrap.quantil(serie, p), 1)
            for p in QUANTIS
        },
        "minimo_mili_bps": round(min(serie), 1),
        "maximo_mili_bps": round(max(serie), 1),
    }


def _regime_por_instante(
    conn: sqlite3.Connection, *, venue: str, symbol: str, timeframe: str,
) -> dict[int, str | None]:
    # Importado do modulo dono do detector, e nunca reimplementado: o ADR 0026
    # congelou os cortes, e uma segunda classificacao divergiria no dia em que
    # alguem mexesse num lado so.
    from .regimes import regimes_das_barras

    return regimes_das_barras(
        conn, venue=venue, symbol=symbol, timeframe=timeframe
    )


def dimensionar(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    config: ExperimentConfig, lado: str = "compra",
) -> Dimensionamento:
    """`sigma_e`, `tau`, a forma da cauda e o `n_exigido`. **Nada mais.**

    Exige o piloto FECHADO e **materializado**. As duas coisas, porque fechar a
    janela nao produz observacao nenhuma - foi essa confusao que fez o criterio
    5 do incremento 18 afirmar medicao olhando uma data.
    """
    serie_bbo = bbo.Serie(venue=venue, symbol=symbol,
                          price_scale_exp=0, volume_scale_exp=0)
    janela = piloto.ler(conn, serie_bbo, contrato)
    if janela is None:
        raise piloto.PilotoNaoFechaAinda(
            "a janela do piloto nao foi fechada; nao ha o que dimensionar"
        )

    bruta = observacao.serie_e2_do_piloto(
        conn, contrato=contrato, venue=venue, symbol=symbol, lado=lado,
        de_ms=janela.de_ms, ate_ms_exclusive=janela.ate_ms_exclusive,
    )
    if len(bruta) < 10:
        raise PilotoNaoMaterializado(
            f"{len(bruta)} observacoes de E2 na janela do piloto. A janela"
            " esta fechada e a serie nao foi materializada - e fechar a janela"
            " nao mede nada"
        )

    serie = [float(v) for v in bruta]
    ic = bootstrap.limite_inferior_do_quantil(serie, 0.10)
    sigma = bootstrap.desvio_padrao(serie)
    from .ajuste import DELTA_MILI_BPS  # o numero mora num lugar so

    preciso = bootstrap.tamanho_necessario(
        sigma_e=sigma, delta=float(DELTA_MILI_BPS), tau=ic.tau.tau
    )

    janela_hash = _hash_do_piloto(conn, serie=serie_bbo, contrato=contrato)

    return Dimensionamento(
        de_ms=janela.de_ms, ate_ms_exclusive=janela.ate_ms_exclusive,
        janela_hash=janela_hash, lado=lado,
        n=ic.n, n_efetivo=ic.n_efetivo, sigma_e_mili_bps=sigma,
        tau=ic.tau.tau, tau_bruto=ic.tau.tau_bruto,
        ar1_plausivel=ic.tau.ar1_plausivel, bloco=ic.bloco,
        forma_da_cauda=_forma_da_cauda(serie),
        p10_diagnostico_mili_bps=ic.ponto,
        lb95_diagnostico_mili_bps=ic.limite_inferior,
        delta_mili_bps=DELTA_MILI_BPS,
        n_exigido=preciso.n_efetivo, n_bruto_exigido=preciso.n_bruto,
        por_regime=_por_regime(
            conn, contrato=contrato, venue=venue, symbol=symbol,
            config=config, lado=lado, janela=janela,
        ),
    )


def _hash_do_piloto(
    conn: sqlite3.Connection, *, serie: bbo.Serie, contrato: str
) -> str:
    from . import manifesto  # importado aqui para nao criar ciclo no topo

    return manifesto.do_registro(
        conn, serie=serie, contrato=contrato
    )["manifesto"]["hash_do_intervalo"]


def _por_regime(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    config: ExperimentConfig, lado: str, janela: piloto.Janela,
) -> tuple[PorRegime, ...]:
    """Um `n_exigido` por regime, com o `sigma_e` MEDIDO naquele regime.

    Usar o `sigma_e` global para todos seria o erro que o ADR 0033 corrigiu num
    lugar vizinho: o maximo sobre uma amostra de regimes nao e limite superior
    para os regimes fora dela, e a media tambem nao e.
    """
    from .ajuste import DELTA_MILI_BPS

    por_instante = _regime_por_instante(
        conn, venue=venue, symbol=symbol, timeframe=config.timeframe
    )
    series: dict[str, list[float]] = {r: [] for r in REGIMES_QUE_CONTAM}
    for t, e2 in conn.execute(
        "SELECT t_grid_ms, e2_mili_bps FROM calibracao_observacao"
        " WHERE finalidade = 'piloto' AND contrato = ? AND venue = ?"
        "   AND symbol = ? AND lado = ? AND dentro_do_l1 = 1"
        "   AND t_grid_ms >= ? AND t_grid_ms < ?"
        " ORDER BY t_grid_ms",
        (contrato, venue, symbol, lado, janela.de_ms,
         janela.ate_ms_exclusive),
    ):
        regime = por_instante.get(int(t))
        if regime in series:
            series[regime].append(float(e2))

    saida = []
    for regime in REGIMES_QUE_CONTAM:
        s = series[regime]
        if len(s) < 10:
            saida.append(PorRegime(
                regime=regime, n=len(s), n_efetivo=float(len(s)),
                sigma_e_mili_bps=None, tau=None, n_exigido=None,
                n_bruto_exigido=None,
                motivo=f"{len(s)} observacoes no piloto: nem o bootstrap roda."
                       f" Sem `sigma_e` proprio nao ha `n_exigido`, e sem"
                       f" `n_exigido` o regime nao tem criterio de parada",
            ))
            continue
        ic = bootstrap.limite_inferior_do_quantil(s, 0.10)
        sigma = bootstrap.desvio_padrao(s)
        preciso = bootstrap.tamanho_necessario(
            sigma_e=sigma, delta=float(DELTA_MILI_BPS), tau=ic.tau.tau
        )
        saida.append(PorRegime(
            regime=regime, n=ic.n, n_efetivo=ic.n_efetivo,
            sigma_e_mili_bps=sigma, tau=ic.tau.tau,
            n_exigido=preciso.n_efetivo, n_bruto_exigido=preciso.n_bruto,
            motivo=None,
        ))
    return tuple(saida)
