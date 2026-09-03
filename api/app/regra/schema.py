"""Formato da estrategia: JSON declarativo, catalogo FECHADO (D5, ADR 0006).

Por que declarativo e nao codigo: a secao 11.10 e explicita em que o agente
nao entrega codigo que vai para producao, e a 10.3.2 rejeita abrir superficie
de execucao dentro do processo que administra o ledger. Um schema fechado e a
versao segura da mesma capacidade - as maos rapidas avaliam um objeto
validado, nunca interpretam texto.

Tres familias, e so tres. O catalogo ser fechado e o ponto: com ele aberto,
"uma regra nova" e indistinguivel de "codigo arbitrario com outro nome".

**Nenhum ponto flutuante.** Multiplicador de desvio vai em milesimos, fracao
de posicao em bps. Um parametro de estrategia acaba multiplicando dinheiro, e
a regra 5 nao abre excecao para o caminho indireto.

O `hash` e do CONTEUDO: duas regras iguais escritas em ordem diferente tem o
mesmo id. E o que torna o criterio 7 possivel - de uma execucao se chega a
regra, e a regra e identificavel pelo que ela diz, nao por quando foi criada.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CondicoesValidade(BaseModel):
    """Sob que condicoes a regra vale (D5, procedencia embrionaria).

    Uma regra sem isso e uma afirmacao sem escopo: aplica-la a outro mercado,
    outro timeframe ou outra fidelidade seria usar um resultado fora das
    condicoes em que ele foi obtido, que e a forma mais comum de se enganar.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    venue: str
    symbol: str
    timeframe: str
    fidelity_level: int = Field(ge=1)


def condicoes_da_config(config) -> CondicoesValidade:
    """As condicoes de validade da config vigente. **Uma definicao.**

    Havia duas, identicas linha por linha: `baselines.condicoes` e
    `contrato.condicoes_da_config`. Nenhuma consequencia hoje - as duas
    devolvem o mesmo objeto -, e o problema e o de sempre: um campo novo em
    `CondicoesValidade` que entre numa e nao na outra faria a MESMA regra
    carregar escopos diferentes conforme o caminho que a criou.

    E este e o campo cuja unica funcao e declarar sob que condicoes um
    resultado vale. O CLAUDE.md ja registra ele mentindo uma vez, quando a
    D20 mudou o modelo de execucao e o texto nao acompanhou. Duas
    construcoes livres para divergir sao a mesma armadilha, um nivel acima.

    Fica aqui, ao lado do modelo, e nao num dos tres modulos que a usam:
    `cerebro`, `maos_rapidas` e `b4` nao se importam entre si por desenho, e
    a funcao nao pode morar em nenhum deles sem furar essa fronteira.

    Tipo do parametro deixado solto de proposito - anotar `ExperimentConfig`
    aqui faria `app/regra` importar `app/config`, e o schema da regra nao
    depende do schema do experimento.
    """
    return CondicoesValidade(
        venue=config.market_venue,
        symbol=config.market_symbol,
        timeframe=config.timeframe,
        fidelity_level=config.fidelity_level,
    )


# ---------------------------------------------------------------------------
# As tres familias do catalogo. Faixas validadas: parametro fora de faixa nao
# e "regra ruim", e regra invalida - e nunca chega a ser executada.
# ---------------------------------------------------------------------------


class CruzamentoMedias(BaseModel):
    """Media rapida cruza a lenta. E o exemplo literal da secao 14.3."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    familia: Literal["cruzamento_medias"] = "cruzamento_medias"
    rapida: int = Field(ge=2, le=200)
    lenta: int = Field(ge=3, le=400)

    @model_validator(mode="after")
    def _ordem(self) -> "CruzamentoMedias":
        if self.rapida >= self.lenta:
            raise ValueError("a media rapida precisa ser menor que a lenta")
        return self

    @property
    def janela_minima(self) -> int:
        return self.lenta


class BandaDesvio(BaseModel):
    """Reversao a media: entra abaixo da banda inferior, sai ao voltar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    familia: Literal["banda_desvio"] = "banda_desvio"
    periodo: int = Field(ge=5, le=200)
    # Milesimos, nao float: 2,000 desvios -> 2000.
    desvios_milesimos: int = Field(ge=100, le=10_000)

    @property
    def janela_minima(self) -> int:
        return self.periodo


class BreakoutCanal(BaseModel):
    """Rompimento: entra acima da maxima do canal, sai abaixo da minima."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    familia: Literal["breakout_canal"] = "breakout_canal"
    periodo: int = Field(ge=5, le=200)

    @property
    def janela_minima(self) -> int:
        return self.periodo


Params = Annotated[
    Union[CruzamentoMedias, BandaDesvio, BreakoutCanal],
    Field(discriminator="familia"),
]


class Regra(BaseModel):
    """Uma estrategia completa e executavel."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    params: Params

    # Fracao do caixa por operacao, em bps. 10000 = todo o caixa.
    position_fraction_bps: int = Field(default=10_000, ge=1, le=10_000)

    # Limite de perda por operacao, em bps (D5). `None` = sem limite.
    #
    # ATENCAO ao que isto NAO e: em fidelidade 1 nao ha como afirmar que a
    # ordem teria sido preenchida no preco do limite. O limite funciona como
    # SINAL DE SAIDA - a barra fechada cujo fundo rompeu o nivel dispara a
    # saida, que executa pelo caminho pessimista normal, com latencia. Supor
    # preenchimento no preco exato seria inventar fidelidade que o dado nao
    # tem (secao 8.4.1.2).
    stop_loss_bps: int | None = Field(default=None, ge=1, le=10_000)

    condicoes_validade: CondicoesValidade

    def canonico(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def hash(self) -> str:
        """Id de conteudo. Mesma regra, mesmo hash, sempre."""
        return hashlib.sha256(self.canonico().encode("utf-8")).hexdigest()

    @property
    def familia(self) -> str:
        return self.params.familia

    @property
    def janela_minima(self) -> int:
        """Barras necessarias antes de a regra poder opinar.

        Antes disso ela nao emite sinal - e nao emite sinal neutro tampouco:
        simplesmente nao ha indicador ainda. Tratar essas barras como "sem
        sinal" seria diferente de tratar como "sinal de ficar de fora".
        """
        return self.params.janela_minima
