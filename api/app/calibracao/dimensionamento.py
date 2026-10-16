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

## O que mudou em 2026-10-16, e por que o nome nao era suficiente

O atributo ja se chamava `p10_diagnostico_mili_bps`, mas a CHAVE PUBLICADA era
`p10_mili_bps` - a mesma, byte a byte, que `ajuste` usa para o p10 que ajusta.
E `lb95_mili_bps` era a mesma de `revalidar`. Aninhar sob `diagnostico` nao
resolve: leitor que achata o json, procura a chave ou copia o campo le a
grandeza errada sem que nada acuse.

Agora o sufixo viaja com o valor - `p10_DIAGNOSTICO_mili_bps` - e ha guarda
estrutural em `tests/test_fronteira_das_finalidades.py`.

**E `pessimista` deixou de existir.** Ele era `lb95 >= 0`, que e literalmente
`REGRAS_DA_REVALIDACAO["alvo"]`, avaliado sobre a serie do PILOTO. Um booleano
nao se le como diagnostico: le-se como resposta. A pergunta "o simulador esta
confirmado?" e de `revalidar`, por regime, sobre a janela de revalidacao.

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

#: OS DOIS LADOS, sempre os dois. ADR 0042.
#:
#: Compra e venda sao populacoes separadas - precos diferentes, `ask` contra
#: `bid` - e o simulador aplica um `spread_bps` so. Medir um lado e aplicar
#: nos dois deixaria metade do parametro sem evidencia.
LADOS = ("compra", "venda")

#: O HORIZONTE DE COBERTURA: 90 dias a 15 minutos. ADR 0043.
#:
#: **Nao foi escolhido olhando o resultado atual.** Vem da D40 (ADR 0026),
#: medida sobre as 70.080 barras do dataset ANTES de existir qualquer forward,
#: onde 90 dias alcancaram 90,7% de cobertura historica de dois regimes - e e
#: a mesma janela que o ADR 0027 cita ao falar da "mediana de 1.114 barras em
#: 90 dias" na celula escassa.
HORIZONTE_DE_COBERTURA_BARRAS = 8_640

#: Quantis publicados como "forma da cauda". A esquerda e densa de proposito:
#: o alvo do ADR 0027 e `p10 >= 0`, e e a cauda inferior que decide.
QUANTIS = (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)


class PilotoNaoMaterializado(Exception):
    """Ha janela fechada e NAO ha serie E2. Fechar a janela nao mede nada."""


@dataclass(frozen=True)
class PorLado:
    """O dimensionamento de UM lado dentro de UM regime. ADR 0042.

    Compra e venda sao populacoes separadas: cada uma tem o proprio `sigma_e`
    e o proprio `n_exigido`. Nenhuma herda o da outra, pelo mesmo argumento do
    ADR 0033 - o maximo sobre uma amostra nao e limite superior para o que
    esta fora dela.
    """

    lado: str
    n: int
    n_efetivo: float
    sigma_e_mili_bps: float | None
    tau: float | None
    n_exigido: int | None
    n_bruto_exigido: int | None
    motivo: str | None
    fracao_do_piloto_ppm: int
    barras_de_calendario: int | None


@dataclass(frozen=True)
class PorRegime:
    """O regime, com os campos SEM sufixo ja agregados CONSERVADORAMENTE.

    ADR 0042: o simulador aplica um `spread_bps` so, entao os dois lados sao
    medidos separados e a decisao e **pelo pior**. Os campos sem sufixo sao o
    que vai para `calibracao_regime_regra`; `por_lado` guarda de onde vieram,
    porque publicar so o agregado esconderia a origem.

    | campo | agregacao | por que |
    |---|---|---|
    | `n_exigido` | **maximo** | o lado mais ruidoso decide quanta amostra |
    | `sigma_e` | do lado que venceu | e o sigma que produziu o `n_exigido` |
    | `n` | **minimo** | o lado com menos observacao e o gargalo |
    | `barras_de_calendario` | **maximo** | o lado mais lento decide o horizonte |
    """

    regime: str
    n: int
    n_efetivo: float
    sigma_e_mili_bps: float | None
    tau: float | None
    n_exigido: int | None
    n_bruto_exigido: int | None
    motivo: str | None

    #: A FRACAO DO PILOTO que este regime ocupou, em ppm. E ela que transforma
    #: `n_bruto_exigido` (observacoes) em CALENDARIO: um regime que ocupou
    #: metade do piloto precisa do dobro de calendario para acumular o mesmo
    #: numero de observacoes.
    fracao_do_piloto_ppm: int
    #: Barras de calendario para acumular `n_bruto_exigido` nesta fracao.
    #: `None` quando a fracao e ZERO - o regime nao apareceu no piloto, e nao
    #: ha horizonte derivavel. Inventar um seria escolher o numero.
    barras_de_calendario: int | None
    #: Os DOIS lados, sempre. Nunca um so, e nunca escolhido por quem chama.
    por_lado: tuple[PorLado, ...] = ()


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
            # A FINALIDADE VAI NO CORPO, e nao so no nome da rota. Quem copia
            # um pedaco deste json leva junto de qual serie ele saiu - e e a
            # serie, e nao a chave, que decide se o numero ajusta, confirma ou
            # so dimensiona.
            "finalidade_da_serie": "piloto",
            "o_que_isto_e": (
                "DIMENSIONAMENTO E DIAGNOSTICO, e nao calibracao. Do piloto"
                " saem `sigma_e`, `tau` e a forma da cauda (ADR 0027 par.5);"
                " nada aqui produz override de parametro nem confirma o"
                " simulador. Quem ajusta le a janela de CALIBRACAO e quem"
                " confirma le a de REVALIDACAO - as duas posteriores e"
                " disjuntas desta"
            ),
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
                # OS NOMES SAO PARTE DA GARANTIA, e nao enfeite.
                #
                # Enquanto estas chaves se chamavam `p10_mili_bps` e
                # `lb95_mili_bps`, elas eram IDENTICAS as que `ajuste` publica
                # para o p10 que ajusta e o LB95 que confirma. Aninhar sob
                # `diagnostico` nao basta: qualquer leitor que achate o json,
                # procure a chave ou copie um campo le a grandeza errada sem
                # que nada acuse. O sufixo viaja com o valor.
                "p10_DIAGNOSTICO_mili_bps": round(
                    self.p10_diagnostico_mili_bps, 1),
                "lb95_DIAGNOSTICO_mili_bps": round(
                    self.lb95_diagnostico_mili_bps, 1),
                "o_que_isto_NAO_e": (
                    "NAO aprova o simulador e NAO produz override de"
                    " parametro. Do piloto saem `sigma_e`, `tau` e a forma da"
                    " cauda (ADR 0027 par.5); quem calibra le a janela de"
                    " CALIBRACAO, que e posterior e disjunta"
                ),
                # `pessimista` MORREU AQUI, e a ausencia e a correcao.
                #
                # Ele era `lb95 >= 0`, que e literalmente
                # `REGRAS_DA_REVALIDACAO["alvo"]` - o criterio de revalidacao -
                # avaliado sobre a serie do PILOTO e publicado como veredito
                # booleano. Um booleano nao se le como diagnostico: ele se le
                # como resposta. Quem responde essa pergunta e `revalidar`,
                # sobre a janela de revalidacao daquele regime, e so ela.
                "onde_mora_a_resposta": {
                    "p10_que_AJUSTA": "finalidade `calibracao`, via"
                                      " `ajuste.estimar_por_regime`",
                    "lb95_que_CONFIRMA": "finalidade `revalidacao`, via"
                                         " `ajuste.revalidar`, por regime e"
                                         " uma vez so",
                },
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
                    # OS DOIS LADOS, de onde o conservador veio. Publicar so
                    # o agregado esconderia a origem - e o ADR 0042 existe
                    # porque a origem estava escondida num default de Python.
                    "por_lado": [
                        {
                            "lado": x.lado,
                            "n": x.n,
                            "n_efetivo": round(x.n_efetivo, 1),
                            "sigma_e_mili_bps": (
                                None if x.sigma_e_mili_bps is None
                                else round(x.sigma_e_mili_bps, 3)
                            ),
                            "tau": None if x.tau is None else round(x.tau, 4),
                            "n_exigido": x.n_exigido,
                            "n_bruto_exigido": x.n_bruto_exigido,
                            "fracao_do_piloto_ppm": x.fracao_do_piloto_ppm,
                            "barras_de_calendario": x.barras_de_calendario,
                            "motivo": x.motivo,
                        }
                        for x in r.por_lado
                    ],
                    "o_n_exigido_gravado_e": (
                        "o MAIOR entre os dois lados (ADR 0042). Fechar o"
                        " regime exige compra E venda alcancarem este mesmo"
                        " numero, cada um por si"
                    ),
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


def _serie_do_regime(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    lado: str, janela: piloto.Janela, por_instante: dict,
) -> tuple[dict[str, list[float]], int]:
    """A serie de E2 por regime, de UM lado, mais o total daquele lado.

    O total inclui `indefinido`, porque ele OCUPA CALENDARIO. Deixa-lo fora
    faria a fracao de cada regime parecer maior do que e, e o horizonte
    derivado sairia curto - na direcao de parar antes.
    """
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

    total = sum(len(s) for s in series.values())
    total += int(conn.execute(
        "SELECT COUNT(*) FROM calibracao_observacao"
        " WHERE finalidade = 'piloto' AND contrato = ? AND venue = ?"
        "   AND symbol = ? AND lado = ? AND dentro_do_l1 = 1"
        "   AND regime = 'indefinido'"
        "   AND t_grid_ms >= ? AND t_grid_ms < ?",
        (contrato, venue, symbol, lado, janela.de_ms,
         janela.ate_ms_exclusive),
    ).fetchone()[0])
    return series, total


def _dimensionar_lado(
    serie: list[float], *, lado: str, total_do_lado: int,
) -> PorLado:
    """`sigma_e` e `n_exigido` de UM lado. Nenhum herda do outro."""
    from .ajuste import DELTA_MILI_BPS

    fracao_ppm = (
        (len(serie) * 1_000_000 // total_do_lado) if total_do_lado else 0
    )
    if len(serie) < bootstrap.MINIMO_PARA_O_QUANTIL:
        return PorLado(
            lado=lado, n=len(serie), n_efetivo=float(len(serie)),
            sigma_e_mili_bps=None, tau=None, n_exigido=None,
            n_bruto_exigido=None,
            motivo=f"{len(serie)} observacoes no piloto neste lado: nem o"
                   f" bootstrap roda. Sem `sigma_e` proprio nao ha"
                   f" `n_exigido`, e sem `n_exigido` nao ha criterio de"
                   f" parada",
            fracao_do_piloto_ppm=fracao_ppm, barras_de_calendario=None,
        )
    ic = bootstrap.limite_inferior_do_quantil(serie, 0.10)
    sigma = bootstrap.desvio_padrao(serie)
    preciso = bootstrap.tamanho_necessario(
        sigma_e=sigma, delta=float(DELTA_MILI_BPS), tau=ic.tau.tau
    )
    # O CHAO E O PISO DO ESTIMADOR, e nao um numero escolhido: abaixo de
    # `MINIMO_PARA_O_QUANTIL` o bootstrap NAO RODA. Nao e relaxar delta -
    # delta diz quantas observacoes sao precisas, e este diz a partir de
    # quantas o estimador existe.
    bruto = max(preciso.n_bruto, bootstrap.MINIMO_PARA_O_QUANTIL)
    calendario = (
        None if fracao_ppm <= 0 else -(-bruto * 1_000_000 // fracao_ppm)
    )
    return PorLado(
        lado=lado, n=ic.n, n_efetivo=ic.n_efetivo, sigma_e_mili_bps=sigma,
        tau=ic.tau.tau, n_exigido=preciso.n_efetivo,
        n_bruto_exigido=preciso.n_bruto, motivo=None,
        fracao_do_piloto_ppm=fracao_ppm, barras_de_calendario=calendario,
    )


def _agregar_conservador(regime: str, lados: tuple[PorLado, ...]) -> PorRegime:
    """A regra do ADR 0042: separado para medir, PELO PIOR para decidir.

    Basta UM lado sem `sigma_e` para o regime nao ter `n_exigido`: fechar
    exige os dois alcancarem, e um limiar que ignorasse o lado nao medido
    autorizaria fechar medindo metade.
    """
    completos = [x for x in lados if x.n_exigido is not None]
    if len(completos) != len(lados):
        faltam = [x.lado for x in lados if x.n_exigido is None]
        return PorRegime(
            regime=regime,
            n=min(x.n for x in lados),
            n_efetivo=float(min(x.n for x in lados)),
            sigma_e_mili_bps=None, tau=None, n_exigido=None,
            n_bruto_exigido=None,
            motivo=(
                f"sem `sigma_e` proprio em {faltam}. O ADR 0042 exige os DOIS"
                f" lados: um `n_exigido` que ignorasse o lado nao medido"
                f" autorizaria fechar o regime medindo metade dele"
            ),
            fracao_do_piloto_ppm=min(x.fracao_do_piloto_ppm for x in lados),
            barras_de_calendario=None,
            por_lado=lados,
        )
    # O MAIOR `n_exigido`, e o `sigma_e`/`tau` DO LADO QUE VENCEU - e nao o
    # maior sigma de um lado com o tau do outro, que descreveria um lado que
    # nao existe.
    pior = max(completos, key=lambda x: (x.n_exigido or 0, x.lado))
    calendarios = [
        x.barras_de_calendario for x in completos
        if x.barras_de_calendario is not None
    ]
    return PorRegime(
        regime=regime,
        n=min(x.n for x in completos),
        n_efetivo=min(x.n_efetivo for x in completos),
        sigma_e_mili_bps=pior.sigma_e_mili_bps,
        tau=pior.tau,
        n_exigido=pior.n_exigido,
        n_bruto_exigido=pior.n_bruto_exigido,
        motivo=None,
        fracao_do_piloto_ppm=min(x.fracao_do_piloto_ppm for x in completos),
        barras_de_calendario=max(calendarios) if calendarios else None,
        por_lado=lados,
    )


def _por_regime(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    config: ExperimentConfig, lado: str, janela: piloto.Janela,
) -> tuple[PorRegime, ...]:
    """Um `n_exigido` por regime, MEDIDO nos dois lados e agregado pelo pior.

    O parametro `lado` continua na assinatura porque `Dimensionamento` declara
    de qual lado sao os campos GLOBAIS (`n`, `sigma_e`, a forma da cauda). Ele
    **nao** escolhe os lados do `por_regime`: estes sao sempre os dois (ADR
    0042), e e por isso que o selamento deixou de poder herdar um default.
    """
    por_instante = _regime_por_instante(
        conn, venue=venue, symbol=symbol, timeframe=config.timeframe
    )
    series_por_lado: dict[str, tuple[dict[str, list[float]], int]] = {
        x: _serie_do_regime(
            conn, contrato=contrato, venue=venue, symbol=symbol, lado=x,
            janela=janela, por_instante=por_instante,
        )
        for x in LADOS
    }

    saida = []
    for regime in REGIMES_QUE_CONTAM:
        lados = tuple(
            _dimensionar_lado(
                series_por_lado[x][0][regime], lado=x,
                total_do_lado=series_por_lado[x][1],
            )
            for x in LADOS
        )
        saida.append(_agregar_conservador(regime, lados))
    return tuple(saida)



class SemHorizonteDerivavel(Exception):
    """Nenhum regime calibravel apareceu no piloto o bastante.

    Sem fracao medida nao ha como transformar `n_bruto` em calendario, e
    escolher uma duracao seria escolher o numero - o mesmo que a D45 recusou ao
    nao fixar `sigma_e`.
    """


def duracao_da_calibracao_ms(
    d: Dimensionamento, *, grade_ms: int,
) -> tuple[int, dict]:
    """O FIM PRE-DECLARADO da janela de calibracao. ADR 0043.

        duracao = max( estatistica, PERMANENCIA_BARRAS, 8.640 barras )

    As tres reguas medem coisas DIFERENTES, e nenhuma domina as outras:

    | regua | o que ela garante |
    |---|---|
    | estatistica | que o regime acumule o proprio `n_exigido` |
    | `PERMANENCIA_BARRAS` | que um episodio possa **existir** - abaixo dela a taxonomia nao classifica nada |
    | 90 dias | que haja chance razoavel de **dois** regimes aparecerem |

    Usar so a primeira produzia a janela de **19 barras** que a previa de
    2026-10-16 revelou: sem episodio possivel, sem cobertura possivel, e com
    `vol_media` e `vol_alta` sem linha nenhuma.

    **Nao ha extensao.** Estender depois de ver quanto falta e escolher o
    horizonte olhando o resultado - a mesma familia que a D45 fechou ao
    recusar relaxar delta "porque nao cabe na reserva".

    Devolve `(duracao_ms, a conta publicada)`. A conta sai junto porque este e
    o numero que fixa quando a calibracao termina, e um numero assim nao pode
    ser so um inteiro no banco.
    """
    from ..regime import deteccao

    com_horizonte = [
        r for r in d.por_regime if r.barras_de_calendario is not None
    ]
    if not com_horizonte:
        # NAO e mais sobre a duracao - com o piso de 90 dias ela sempre
        # existe. E sobre nao haver regime nenhum com regra: a janela nasceria
        # sem criterio de parada para ninguem, e nenhum regime poderia fechar.
        raise SemHorizonteDerivavel(
            "nenhum regime calibravel tem `sigma_e` proprio nos DOIS lados"
            " (ADR 0042), entao nao ha regra a congelar. Uma janela sem regra"
            " nenhuma nao tem criterio de parada para regime algum"
        )
    mais_lento = max(com_horizonte, key=lambda r: r.barras_de_calendario or 0)
    estatistica = int(mais_lento.barras_de_calendario or 0)
    permanencia = int(deteccao.PERMANENCIA_BARRAS)
    barras = max(estatistica, permanencia, HORIZONTE_DE_COBERTURA_BARRAS)
    venceu = (
        "estatistica" if barras == estatistica
        else "permanencia" if barras == permanencia
        else "horizonte_de_cobertura"
    )
    return barras * grade_ms, {
        "barras": barras,
        "grade_ms": grade_ms,
        "regime_mais_lento": mais_lento.regime,
        "como": (
            "max(estatistica, permanencia, horizonte de cobertura) - ADR 0043."
            " As tres medem coisas diferentes e nenhuma domina as outras"
        ),
        "as_tres_reguas": {
            "estatistica_barras": estatistica,
            "permanencia_barras": permanencia,
            "horizonte_de_cobertura_barras": HORIZONTE_DE_COBERTURA_BARRAS,
            "venceu": venceu,
            "o_que_cada_uma_garante": {
                "estatistica": "que o regime acumule o proprio `n_exigido`,"
                               " pela fracao do piloto que ele ocupou",
                "permanencia": "que um EPISODIO possa existir: abaixo de"
                               f" {permanencia} barras a taxonomia da D40 nao"
                               " classifica regime nenhum",
                "horizonte_de_cobertura": "90 dias, da D40 - medidos sobre as"
                                          " 70.080 barras ANTES de existir"
                                          " forward, com 90,7% de cobertura"
                                          " historica de dois regimes",
            },
        },
        "nao_ha_extensao": (
            "a janela termina exatamente aqui. Estender depois de ver quanto"
            " falta seria escolher o horizonte olhando o resultado; regime que"
            " nao alcancar permanece NAO CALIBRADO e usa a base como hipotese"
        ),
        "por_regime": [
            {
                "regime": r.regime,
                "n_bruto_exigido": r.n_bruto_exigido,
                "fracao_do_piloto_ppm": r.fracao_do_piloto_ppm,
                "barras_de_calendario": r.barras_de_calendario,
                "sem_horizonte_por_que": (
                    None if r.barras_de_calendario is not None
                    else "nao apareceu no piloto o bastante: sem fracao medida"
                         " nao ha calendario derivavel, e o regime fica SEM"
                         " OVERRIDE"
                ),
            }
            for r in d.por_regime
        ],
    }
