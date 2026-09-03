"""O que o modelo tem permissao de responder. Nada alem disto.

Duas camadas, e as duas existem por motivos diferentes:

1. **O schema enviado ao provedor** (`SCHEMA_*`), em JSON Schema. E uma dica
   forte: o provedor tenta obedecer. Nao e garantia de nada.

2. **A validacao em Python** (`Interpretacao`, `PropostaBruta`). E o portao.
   Resposta que nao passa aqui e rejeitada, registrada como rejeicao, e a
   regra ativa anterior permanece (criterio 2).

Nunca confiar na camada 1 para o que a camada 2 tem de garantir. Um provedor
que hoje respeita o schema pode parar de respeitar amanha, e o segundo
provedor (secao 3.9) tem implementacao propria de saida estruturada - se a
validacao morasse no schema enviado, trocar de provedor trocaria a regra de
validade em silencio.

O schema e **plano de proposito**: `oneOf` com discriminador e o ponto em que
implementacoes de saida estruturada mais divergem entre provedores. Um objeto
plano com campos nulaveis atravessa os dois, e a coerencia entre familia e
parametros e conferida aqui, onde da para explicar o motivo da recusa.

**`condicoes_validade` nao esta no contrato de saida.** Mercado, instrumento,
timeframe e fidelidade sao fato do experimento, nao opiniao do modelo: deixa-lo
declarar sob que condicoes a propria regra vale seria deixa-lo carimbar a
propria procedencia.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config.schema import ExperimentConfig
from ..hipotese.schema import SCHEMA_PRE_REGISTRO, PreRegistroBruto
from ..regra.schema import (
    BandaDesvio,
    BreakoutCanal,
    CondicoesValidade,
    CruzamentoMedias,
    Regra,
)

FAMILIAS = ("cruzamento_medias", "banda_desvio", "breakout_canal")

# Limites de texto livre. Existem como GUARDA contra resposta desgovernada,
# nao como regra de estilo - e a diferenca importa: a primeira chamada real
# foi recusada por um diagnostico de 812 caracteres, um texto perfeitamente
# bom descartado por doze caracteres.
#
# `maxLength` no schema enviado e conselho, nao imposicao: o provedor nao
# garante que respeita. Quem decide e a validacao daqui - entao o numero
# precisa ser folgado o bastante para que passar dele signifique mesmo que
# algo saiu do lugar. O teto duro de verdade e `max_tokens`.
#
# **Um numero so, usado nos tres lugares**: no modelo que valida, no schema
# que vai ao provedor, e no texto que o modelo le. Dois numeros para o mesmo
# campo e como este projeto ja se enganou cinco vezes - antes o prompt pedia
# "no maximo 5 frases" e o schema exigia 800 caracteres, que nao sao a mesma
# afirmacao.
MAX_CHARS_DIAGNOSTICO = 2_000
MAX_CHARS_EXPECTATIVA = 1_200

# Campos que cada familia usa. O que nao esta na lista precisa vir NULO -
# nao ignorado. Um parametro de outra familia chegando junto significa que o
# modelo nao decidiu qual familia esta propondo, e adivinhar por ele seria
# escolher a estrategia no lugar dele.
CAMPOS_DA_FAMILIA: dict[str, tuple[str, ...]] = {
    "cruzamento_medias": ("rapida", "lenta"),
    "banda_desvio": ("periodo", "desvios_milesimos"),
    "breakout_canal": ("periodo",),
}

TODOS_OS_PARAMETROS = ("rapida", "lenta", "periodo", "desvios_milesimos")


class RespostaInvalida(Exception):
    """A resposta do modelo nao vira regra. Vira registro de rejeicao."""


# ---------------------------------------------------------------------------
# No `interpretar`
# ---------------------------------------------------------------------------


class Interpretacao(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    regime: Literal["tendencia", "reversao", "indefinido"]
    diagnostico: str = Field(min_length=1, max_length=MAX_CHARS_DIAGNOSTICO)
    familia_recomendada: Literal[
        "cruzamento_medias", "banda_desvio", "breakout_canal", "nenhuma"
    ]


SCHEMA_INTERPRETACAO: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["regime", "diagnostico", "familia_recomendada"],
    "properties": {
        "regime": {
            "type": "string",
            "enum": ["tendencia", "reversao", "indefinido"],
        },
        "diagnostico": {
            "type": "string",
            "maxLength": MAX_CHARS_DIAGNOSTICO,
            "description": (
                "Leitura do periodo, citando os numeros do resumo que a"
                f" sustentam. No maximo {MAX_CHARS_DIAGNOSTICO} caracteres -"
                " passar disso faz a resposta inteira ser rejeitada."
            ),
        },
        "familia_recomendada": {
            "type": "string",
            "enum": [*FAMILIAS, "nenhuma"],
        },
    },
}


# ---------------------------------------------------------------------------
# No `propor_regra`
# ---------------------------------------------------------------------------


class PropostaBruta(BaseModel):
    """A resposta do modelo, ainda sem procedencia."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    familia: Literal["cruzamento_medias", "banda_desvio", "breakout_canal"]

    rapida: int | None = None
    lenta: int | None = None
    periodo: int | None = None
    desvios_milesimos: int | None = None

    position_fraction_bps: int
    stop_loss_bps: int | None = None

    # O pre-registro da secao 8.2, que na 0B substitui a frase de expectativa.
    #
    # Na 0A este campo era `expectativa: str` - texto livre. Ele cumpria a
    # regra 17 e nada mais: julgar se a expectativa se realizou exigia leitura
    # humana, e foi por isso que a avaliacao posterior fechou a 0A com
    # `veredito_da_expectativa = None`. R33 e R51 tornaram isso insuficiente.
    #
    # A frase nao sumiu: virou `pre_registro.enunciado`, que continua sendo o
    # que vai para `rule_proposal.expectation` e para o evento da decisao. O
    # que mudou e que agora ela vem acompanhada do que torna o veredito uma
    # conta - `efeito_minimo` e `condicoes_falseamento`.
    pre_registro: PreRegistroBruto
    confianca_ppm: int

    @property
    def expectativa(self) -> str:
        """A metade em texto do pre-registro.

        Existe como propriedade, e nao como campo, porque tudo que ja lia
        `bruta.expectativa` na 0A continua lendo a mesma coisa: a afirmacao
        que o agente declarou antes de executar. Trocar o nome em cinco
        lugares para dizer o mesmo seria churn; o que mudou de verdade e o que
        ACOMPANHA a frase, nao a frase.
        """
        return self.pre_registro.enunciado

    @model_validator(mode="after")
    def _coerencia(self) -> "PropostaBruta":
        usados = CAMPOS_DA_FAMILIA[self.familia]
        for campo in TODOS_OS_PARAMETROS:
            valor = getattr(self, campo)
            if campo in usados and valor is None:
                raise ValueError(
                    f"familia '{self.familia}' exige o parametro '{campo}'"
                )
            if campo not in usados and valor is not None:
                raise ValueError(
                    f"familia '{self.familia}' nao usa o parametro '{campo}';"
                    f" veio {valor!r}"
                )
        if not 0 <= self.confianca_ppm <= 1_000_000:
            raise ValueError("confianca_ppm precisa estar entre 0 e 1000000")
        return self

    def parametros(self) -> CruzamentoMedias | BandaDesvio | BreakoutCanal:
        """Converte para o catalogo fechado da D5, que valida as faixas."""
        if self.familia == "cruzamento_medias":
            return CruzamentoMedias(rapida=self.rapida, lenta=self.lenta)
        if self.familia == "banda_desvio":
            return BandaDesvio(
                periodo=self.periodo, desvios_milesimos=self.desvios_milesimos
            )
        return BreakoutCanal(periodo=self.periodo)


SCHEMA_PROPOSTA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "familia",
        "rapida",
        "lenta",
        "periodo",
        "desvios_milesimos",
        "position_fraction_bps",
        "stop_loss_bps",
        "pre_registro",
        "confianca_ppm",
    ],
    "properties": {
        "familia": {"type": "string", "enum": list(FAMILIAS)},
        "rapida": {
            "type": ["integer", "null"],
            "description": "cruzamento_medias: janela rapida, 2 a 200."
            " Nulo nas outras familias.",
        },
        "lenta": {
            "type": ["integer", "null"],
            "description": "cruzamento_medias: janela lenta, 3 a 400, maior"
            " que a rapida. Nulo nas outras familias.",
        },
        "periodo": {
            "type": ["integer", "null"],
            "description": "banda_desvio e breakout_canal: periodo, 5 a 200."
            " Nulo em cruzamento_medias.",
        },
        "desvios_milesimos": {
            "type": ["integer", "null"],
            "description": "banda_desvio: desvios padrao em MILESIMOS"
            " (2,0 desvios = 2000), de 100 a 10000. Nulo nas outras.",
        },
        "position_fraction_bps": {
            "type": "integer",
            "description": "fracao do caixa por operacao em bps;"
            " 10000 = todo o caixa.",
        },
        "stop_loss_bps": {
            "type": ["integer", "null"],
            "description": "limite de perda por operacao em bps, ou nulo."
            " Em fidelidade 1 funciona como SINAL DE SAIDA na barra seguinte,"
            " nao como preenchimento no preco do limite.",
        },
        "pre_registro": SCHEMA_PRE_REGISTRO,
        "confianca_ppm": {
            "type": "integer",
            "description": "confianca no enunciado do pre-registro, em partes"
            " por milhao. 500000 = 50%.",
        },
    },
}


def condicoes_da_config(config: ExperimentConfig) -> CondicoesValidade:
    """Procedencia da regra: vem do experimento, nunca do modelo."""
    return CondicoesValidade(
        venue=config.market_venue,
        symbol=config.market_symbol,
        timeframe=config.timeframe,
        fidelity_level=config.fidelity_level,
    )


def montar_regra(bruta: PropostaBruta, config: ExperimentConfig) -> Regra:
    """Da proposta validada a regra executavel, com procedencia carimbada.

    Levanta `pydantic.ValidationError` se um parametro estiver fora da faixa
    do catalogo. Faixa nao e sugestao: parametro fora dela nao e regra ruim,
    e regra invalida, e nunca chega a ser executada.
    """
    return Regra(
        params=bruta.parametros(),
        position_fraction_bps=bruta.position_fraction_bps,
        stop_loss_bps=bruta.stop_loss_bps,
        condicoes_validade=condicoes_da_config(config),
    )
