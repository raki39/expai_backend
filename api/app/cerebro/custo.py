"""Tokens em dinheiro. **Exato, nunca estimado** (secao 5.2, R13).

Quatro componentes, cada um com seu preco na tabela versionada:

    entrada cheia      tokens_in            x input_usd_per_mtok
    leitura de cache   tokens_cache_read    x cache_read_usd_per_mtok
    escrita de cache   tokens_cache_write   x cache_write_usd_per_mtok
    saida              tokens_out           x output_usd_per_mtok

A conta e feita em **micros de USD** com `Decimal`, e so entao arredondada.
Micros porque uma reflexao custa fracoes de centavo: arredondar cada chamada
para centavo antes de somar transformaria toda chamada em "1 centavo" e
apagaria a diferenca entre um prompt de 2.000 tokens e um de 20.000 - que e
precisamente o que o incremento 5 existe para tornar visivel.

**Faltar dado nao vira zero.** Se ha preco para um componente mas o provedor
nao informou o token, a conta e impossivel e isto levanta excecao: completar
com zero seria estimar, e estimar e o que a secao 5.2 proibe. Se nao ha preco
verificado para o componente, ele nao entra e fica REGISTRADO que nao entrou -
um custo incompleto que se anuncia e diferente de um custo errado que nao se
anuncia.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from ..config.schema import ExperimentConfig, PrecoModelo
from ..ledger.livro import Uso

MICRO = 1_000_000
MTOK = Decimal(1_000_000)


class UsoIncompleto(Exception):
    """Ha preco para o componente e o provedor nao informou os tokens."""


class SemPreco(Exception):
    """O modelo usado nao tem linha datada na tabela de precos."""


@dataclass(frozen=True)
class Custo:
    """O custo de uma chamada, decomposto. Nunca um numero agregado sozinho.

    O criterio 3 do incremento 3 recusou "um campo custo agregado" para a
    execucao pelo mesmo motivo que vale aqui: sem separar, e impossivel saber
    depois qual componente comeu o orcamento.
    """

    entrada_micro: int
    cache_read_micro: int
    cache_write_micro: int
    saida_micro: int
    componentes_sem_preco: tuple[str, ...]

    @property
    def total_micro(self) -> int:
        return (
            self.entrada_micro
            + self.cache_read_micro
            + self.cache_write_micro
            + self.saida_micro
        )

    @property
    def total_cents(self) -> int:
        """O que vai para o ledger: teto em centavos.

        Para cima, como todo custo neste projeto (regra do arredondamento
        assimetrico): o centavo de arredondamento vai contra o experimento.
        """
        return -(-self.total_micro // 10_000)

    @property
    def completo(self) -> bool:
        return not self.componentes_sem_preco

    def como_dict(self) -> dict:
        return {
            "entrada_micro": self.entrada_micro,
            "cache_read_micro": self.cache_read_micro,
            "cache_write_micro": self.cache_write_micro,
            "saida_micro": self.saida_micro,
            "total_micro": self.total_micro,
            "total_cents": self.total_cents,
            "completo": self.completo,
            "componentes_sem_preco": list(self.componentes_sem_preco),
        }


def preco_de(config: ExperimentConfig, provider: str, model: str) -> PrecoModelo:
    for linha in config.price_table:
        if linha.provider == provider and linha.model == model:
            if not linha.verified_at:
                raise SemPreco(
                    f"{provider}/{model} tem linha de preco sem data de"
                    " verificacao; custo por decisao viraria estimativa"
                )
            return linha
    raise SemPreco(f"{provider}/{model} nao tem linha na tabela de precos")


def _componente(
    nome: str, tokens: int | None, preco_por_mtok: Decimal | None, sem_preco: list[str]
) -> int:
    """Um componente em micros de USD, exato."""
    if preco_por_mtok is None:
        # Sem preco verificado: o componente nao entra, e isso fica dito.
        # Nao ha diferenca pratica quando o provedor tambem nao reporta o
        # token (e o caso da escrita de cache na OpenAI), mas ha quando
        # reporta - e ai o custo esta incompleto e precisa se anunciar.
        if tokens:
            sem_preco.append(nome)
        return 0
    if tokens is None:
        raise UsoIncompleto(
            f"ha preco para '{nome}' e o provedor nao informou os tokens;"
            " completar com zero seria estimar"
        )
    exato = Decimal(tokens) * Decimal(preco_por_mtok) / MTOK * MICRO
    return int(exato.to_integral_value(rounding=ROUND_CEILING))


def calcular(uso: Uso, preco: PrecoModelo) -> Custo:
    sem_preco: list[str] = []
    return Custo(
        entrada_micro=_componente(
            "entrada", uso.tokens_in, preco.input_usd_per_mtok, sem_preco
        ),
        cache_read_micro=_componente(
            "cache_read", uso.tokens_cache_read, preco.cache_read_usd_per_mtok,
            sem_preco,
        ),
        cache_write_micro=_componente(
            "cache_write", uso.tokens_cache_write, preco.cache_write_usd_per_mtok,
            sem_preco,
        ),
        saida_micro=_componente(
            "saida", uso.tokens_out, preco.output_usd_per_mtok, sem_preco
        ),
        componentes_sem_preco=tuple(sem_preco),
    )
