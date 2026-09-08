"""Os seis requisitos de saida da quarentena. §8.5, incremento 19.

## Puro de proposito

`avaliar` nao toca banco, nao le config e nao decide se algo pode ser admitido.
Ela recebe **medicoes** e **limiares** e devolve um veredito.

E isso e a garantia 3 do ADR 0034: o caminho positivo - o de promover - e
exercitado por **controle positivo sintetico, so na suite**, e a unica forma de
fazer isso sem fraudar o experimento e a funcao que julga ser separada da que
admite. `admitir` recusa em producao; `avaliar` aceita dados de qualquer
procedencia porque nao sabe de onde eles vem.

## Tres resultados, e eles continuam tres (R78)

    aprovado      os SEIS cumpridos
    rejeitado     um requisito violado DEFINITIVAMENTE
    inconclusivo  ainda acumulando, ou reserva encerrada sem cumprir

**`inconclusivo` nunca vira `aprovado`.** E reserva encerrada sem candidata
aprovada e `inconclusivo`, nunca aprovacao nem regua flexibilizada (garantia 6
do ADR 0034).

## Nenhum limiar de dias em codigo de decisao (R73)

Todo numero vem em `Limiares`, lido de `quarentena_limiar` - congelado antes do
primeiro tick. Nao ha constante de dias neste arquivo, e ha teste varrendo por
uma.
"""

from __future__ import annotations

from dataclasses import dataclass, field

APROVADO = "aprovado"
REJEITADO = "rejeitado"
INCONCLUSIVO = "inconclusivo"

# Os seis, na ordem de §8.5. A ordem importa no relatorio: ela conta a
# historia de fora para dentro - primeiro se houve tempo, depois se houve
# amostra, depois se houve variedade de condicao, e so no fim se o resultado
# se manteve.
REQUISITOS = (
    "periodo_tecnico_minimo",
    "n_efetivo_alcancado",
    "regimes_distintos",
    "direcao_prevista",
    "magnitude_minima",
    "positivo_apos_custos",
)


@dataclass(frozen=True)
class Limiares:
    """Os limiares CONGELADOS. Nenhum default: eles vem do banco."""

    periodo_minimo_barras: int
    regimes_minimos: int
    magnitude_minima_ppm: int
    reserva_maxima_barras: int


@dataclass(frozen=True)
class Medicoes:
    """O que o forward mediu. Nada aqui e limiar."""

    barras_observadas: int
    n_efetivo: float
    n_minimo: int
    regimes_com_permanencia: tuple[str, ...]
    # A direcao declarada no pre-registro, e a observada. `None` na observada
    # significa "nao ha sinal", que e diferente de "sinal contrario".
    direcao_declarada: str | None
    direcao_observada: str | None
    # A metrica primaria no forward e no in-sample CONGELADO (R74). O
    # in-sample nunca e recalculado: ele vem do run que ficou gravado.
    metrica_forward_cents: int
    metrica_in_sample_cents: int
    liquido_apos_custos_cents: int


@dataclass(frozen=True)
class Veredito:
    resultado: str
    motivo: str
    requisitos: dict[str, bool | None] = field(default_factory=dict)
    faltando: tuple[str, ...] = ()
    detalhe: dict = field(default_factory=dict)

    @property
    def aprovado(self) -> bool:
        return self.resultado == APROVADO


def _magnitude_ppm(forward_cents: int, in_sample_cents: int) -> int | None:
    """`forward / in_sample` em ppm. `None` quando o in-sample e <= 0.

    In-sample nao positivo torna a razao sem sentido - e "50% de um numero
    negativo" seria uma comparacao que passa quando o forward piora. `None`
    aqui e "nao se pode dizer", e o requisito sai `None`, nao `True`.
    """
    if in_sample_cents <= 0:
        return None
    return round(forward_cents * 1_000_000 / in_sample_cents)


def avaliar(m: Medicoes, lim: Limiares) -> Veredito:
    """Os seis requisitos, cada um derivado das medicoes. NAO toca banco."""
    magnitude = _magnitude_ppm(m.metrica_forward_cents, m.metrica_in_sample_cents)

    req: dict[str, bool | None] = {
        "periodo_tecnico_minimo": m.barras_observadas >= lim.periodo_minimo_barras,
        "n_efetivo_alcancado": m.n_efetivo >= m.n_minimo,
        "regimes_distintos": (
            len(set(m.regimes_com_permanencia)) >= lim.regimes_minimos
        ),
        # `None` quando nao ha direcao declarada: as hipoteses 1 a 41 foram
        # pre-registradas antes de a taxonomia existir, e inventar uma direcao
        # para elas alteraria o pre-registro, que e imutavel.
        "direcao_prevista": (
            None if m.direcao_declarada is None
            else m.direcao_observada == m.direcao_declarada
        ),
        "magnitude_minima": (
            None if magnitude is None
            else magnitude >= lim.magnitude_minima_ppm
        ),
        "positivo_apos_custos": m.liquido_apos_custos_cents > 0,
    }

    detalhe = {
        "barras_observadas": m.barras_observadas,
        "periodo_minimo_barras": lim.periodo_minimo_barras,
        "n_efetivo": round(m.n_efetivo, 1),
        "n_minimo": m.n_minimo,
        "regimes": sorted(set(m.regimes_com_permanencia)),
        "regimes_minimos": lim.regimes_minimos,
        "magnitude_ppm": magnitude,
        "magnitude_minima_ppm": lim.magnitude_minima_ppm,
        "liquido_apos_custos_cents": m.liquido_apos_custos_cents,
        "reserva_maxima_barras": lim.reserva_maxima_barras,
        "reserva_encerrada": m.barras_observadas >= lim.reserva_maxima_barras,
    }

    # ------------------------------------------------------------ REJEITADO
    #
    # Violacao DEFINITIVA, e "definitiva" tem um teste: mais dado mudaria a
    # resposta? Direcao contraria com amostra suficiente nao muda de sinal por
    # esperar mais - e continuar esperando seria dar a hipotese um numero
    # ilimitado de tentativas.
    amostra_suficiente = m.n_efetivo >= m.n_minimo
    if amostra_suficiente and req["direcao_prevista"] is False:
        return Veredito(
            resultado=REJEITADO,
            motivo=(
                f"direcao OBSERVADA ({m.direcao_observada}) contraria a "
                f"DECLARADA ({m.direcao_declarada}), com amostra suficiente "
                f"({m.n_efetivo:.0f} >= {m.n_minimo}). Mais dado nao inverte "
                f"o sinal"
            ),
            requisitos=req, faltando=(), detalhe=detalhe,
        )
    if amostra_suficiente and req["positivo_apos_custos"] is False:
        return Veredito(
            resultado=REJEITADO,
            motivo=(
                f"liquido de {m.liquido_apos_custos_cents} centavos apos "
                f"custos, com amostra suficiente. §14.4 gateia por FATO do "
                f"ledger, e ele nao depende de esperar mais"
            ),
            requisitos=req, faltando=(), detalhe=detalhe,
        )

    faltando = tuple(k for k in REQUISITOS if req[k] is not True)

    # ------------------------------------------------------------- APROVADO
    if not faltando:
        return Veredito(
            resultado=APROVADO,
            motivo="os seis requisitos de §8.5 cumpridos, cada um derivado de "
                   "medicao",
            requisitos=req, faltando=(), detalhe=detalhe,
        )

    # --------------------------------------------------------- INCONCLUSIVO
    if detalhe["reserva_encerrada"]:
        return Veredito(
            resultado=INCONCLUSIVO,
            motivo=(
                f"RESERVA ENCERRADA sem cumprir: {', '.join(faltando)}. O "
                f"resultado e inconclusivo, NUNCA aprovacao - e o limiar nao "
                f"pode ser reduzido depois (ADR 0034, garantia 6)"
            ),
            requisitos=req, faltando=faltando, detalhe=detalhe,
        )
    return Veredito(
        resultado=INCONCLUSIVO,
        motivo=(
            f"ainda acumulando. Falta: {', '.join(faltando)}. "
            f"{lim.reserva_maxima_barras - m.barras_observadas} barras de "
            f"reserva restantes"
        ),
        requisitos=req, faltando=faltando, detalhe=detalhe,
    )


def sem_candidata() -> Veredito:
    """O veredito da 0C, e ele e nomeado.

    "Nao houve candidata" e diferente de "a candidata nao alcancou a amostra",
    e §14.4 exige que inconclusivo diga QUAL. Devolver o mesmo objeto dos
    outros casos, sem o motivo proprio, faria as duas situacoes ficarem
    indistinguiveis num relatorio.
    """
    return Veredito(
        resultado=INCONCLUSIVO,
        motivo=(
            "NENHUMA CANDIDATA ADMITIDA (D38, ADR 0034). O Portao B rejeitou a "
            "unica que existia, e §14.4 diz que a abordagem esta descartada. O "
            "B3 rodou apenas como CONTROLE NEGATIVO do encanamento - ele nao "
            "tem pre-registro, nao consome credito e nao entra em familia"
        ),
        requisitos={k: None for k in REQUISITOS},
        faltando=REQUISITOS,
        detalhe={"sem_candidata": True},
    )
