"""Parametros do experimento: o que vive no banco, versionado (ADR 0008).

Nada aqui e segredo e nada aqui e bootstrap. Estes campos sao editaveis pelo
painel, toda alteracao cria uma nova `config_version` com autor, data, valor
anterior e novo, e alteracao material invalida comparacao com runs anteriores
(secao 10.2.3).

Valores monetarios em **inteiros de centavos**; taxas e precos em `Decimal`.
Nunca ponto flutuante para dinheiro (secao 5).
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Campos que NAO entram no `config_hash` e nao contam como mudanca material.
# `default_seed` fica de fora porque trocar a semente e reexecucao legitima
# (secao 14.4.1): semente diferente, mesmo `config_hash`.
CAMPOS_NAO_MATERIAIS = frozenset({"default_seed", "note"})


class ModeloTier(BaseModel):
    """Um degrau do catalogo: o agente pede um tier, nao um modelo (secao 3.9)."""

    model_config = ConfigDict(frozen=True)

    provider: Literal["anthropic", "openai"]
    model: str = ""  # vazio = tier nao configurado ainda


class PrecoModelo(BaseModel):
    """Uma linha da tabela de precos.

    Secao 3.9: "Tabela de precos versionada e datada. Preco interno e dado de
    configuracao, com data de verificacao, nunca constante no codigo."
    """

    model_config = ConfigDict(frozen=True)

    provider: Literal["anthropic", "openai"]
    model: str
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    # Secao 3.3: contexto repetido custa 10% do preco de entrada.
    cache_read_usd_per_mtok: Decimal | None = None
    # Nao verificado nesta sessao; preencher e datar antes de usar.
    cache_write_usd_per_mtok: Decimal | None = None
    # None = nao verificado. "Nao sei" nao e a mesma coisa que zero.
    verified_at: str | None = None


class ExperimentConfig(BaseModel):
    """Configuracao completa do experimento da Fase 0A."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # ------------------------------------------------ mercado (ADR 0001)
    market_venue: str = "binance"
    market_symbol: str = "BTCUSDT"
    market_kind: Literal["spot"] = "spot"
    position_mode: Literal["long_flat"] = "long_flat"

    # ------------------------------------ dados e janela (ADR 0002, 0006)
    timeframe: str = "15m"
    data_start: str = "2024-09-01"
    data_end: str = "2026-09-01"
    # Ultimos ~20% reservados na ingestao, excluidos do loader do experimento.
    reserved_fraction: Decimal = Decimal("0.20")
    # Nivel 1 = candles OHLCV. Declarado e propagado a todo resultado
    # (secao 8.4.1.1). Nivel 2 nao cabe no volume Hobby em janela longa.
    fidelity_level: int = Field(default=1, ge=1, le=5)

    # ------------------------------- simulador pessimista (secao 8.4.1)
    # Sempre taker. Spread por cima, slippage sempre desfavoravel, atraso
    # entre decisao e execucao, penalidade adicional explicita.
    taker_fee_bps: Decimal = Decimal("10")     # Binance spot: 0,10%
    spread_bps: Decimal = Decimal("1")         # spread cheio, aplicado pela metade em cada lado
    slippage_bps: Decimal = Decimal("2")
    latency_bars: int = Field(default=1, ge=1)
    penalty_bps: Decimal = Decimal("1")

    # ------------------------------------------- economia (ADR 0003/0006)
    seed_capital_usd_cents: int = Field(default=100_000, gt=0)  # US$ 1.000

    # Tetos OPERACIONAIS. O limite inviolavel e LLM_MAX_USD_ABSOLUTE, no env,
    # e esta config nao pode exceder aquele valor (secao 12.1).
    max_llm_calls_per_run: int = Field(default=12, ge=0)
    max_llm_usd_per_run_cents: int = Field(default=200, ge=0)  # US$ 2,00

    # --------------------------------------- provedores (ADR 0003 e 0009)
    tiers: dict[str, ModeloTier] = Field(
        default_factory=lambda: {
            "padrao": ModeloTier(provider="anthropic", model="claude-sonnet-5"),
            "topo": ModeloTier(provider="anthropic", model="claude-opus-5"),
            # Segundo provedor exigido pela secao 3.9. Ids e precos da OpenAI
            # NAO foram verificados: preencher e datar antes de usar.
            "padrao_alt": ModeloTier(provider="openai", model=""),
            "topo_alt": ModeloTier(provider="openai", model=""),
        }
    )

    price_table_version: str = "2026-09-01"
    price_table: list[PrecoModelo] = Field(
        default_factory=lambda: [
            PrecoModelo(
                provider="anthropic",
                model="claude-sonnet-5",
                input_usd_per_mtok=Decimal("2.00"),
                output_usd_per_mtok=Decimal("10.00"),
                cache_read_usd_per_mtok=Decimal("0.20"),
                verified_at="2026-09-01",
            ),
            PrecoModelo(
                provider="anthropic",
                model="claude-opus-5",
                input_usd_per_mtok=Decimal("5.00"),
                output_usd_per_mtok=Decimal("25.00"),
                cache_read_usd_per_mtok=Decimal("0.50"),
                verified_at="2026-09-01",
            ),
        ]
    )

    # ------------------------------------- ponte de cambio (secao 5.1)
    # Fixada por periodo e registrada em cada evento, para que variacao
    # cambial nao seja confundida com desempenho do agente (secao 4.2).
    fx_brl_per_usd: Decimal = Decimal("5.40")
    fx_rate_date: str = "2026-09-01"

    # -------------------------------------------- baselines (secao 14.3)
    b1_repetitions: int = Field(default=1000, ge=1000)  # minimo do documento
    # Congelados antes do primeiro run do agente (ADR 0006). Alterar depois
    # de ver o resultado destroi o grupo de controle.
    b3_fast: int = Field(default=20, ge=1)
    b3_slow: int = Field(default=50, ge=2)

    # ------------------------------------------------------ nao materiais
    default_seed: int = 42
    note: str = ""

    @model_validator(mode="after")
    def _coerencia(self) -> "ExperimentConfig":
        if self.b3_fast >= self.b3_slow:
            raise ValueError("b3_fast precisa ser menor que b3_slow")
        if not (Decimal("0") < self.reserved_fraction < Decimal("1")):
            raise ValueError("reserved_fraction precisa estar entre 0 e 1")
        if self.data_start >= self.data_end:
            raise ValueError("data_start precisa ser anterior a data_end")
        # Um tier configurado precisa ter preco datado, senao o custo por
        # decisao vira estimativa - o que a secao 5.2 proibe.
        precos = {(p.provider, p.model) for p in self.price_table}
        for nome, tier in self.tiers.items():
            if tier.model and (tier.provider, tier.model) not in precos:
                raise ValueError(
                    f"tier '{nome}' aponta para {tier.provider}/{tier.model}, "
                    "que nao tem linha na tabela de precos"
                )
        return self

    # ------------------------------------------------------------ hashing
    def payload_material(self) -> dict:
        """Campos que definem o experimento, em forma canonica."""
        bruto = self.model_dump(mode="json")
        return {k: v for k, v in bruto.items() if k not in CAMPOS_NAO_MATERIAIS}

    def config_hash(self) -> str:
        canonico = json.dumps(
            self.payload_material(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonico.encode("utf-8")).hexdigest()

    def diff(self, outra: "ExperimentConfig") -> list[tuple[str, object, object]]:
        """Campos que mudam de `self` para `outra`, em ordem estavel."""
        a = self.model_dump(mode="json")
        b = outra.model_dump(mode="json")
        return [
            (campo, a[campo], b[campo])
            for campo in sorted(set(a) | set(b))
            if a.get(campo) != b.get(campo)
        ]


def campo_material(campo: str) -> bool:
    return campo not in CAMPOS_NAO_MATERIAIS
