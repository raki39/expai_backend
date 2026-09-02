"""Uma chamada de modelo, do teto ao lancamento. E o coracao do incremento 5.

A ordem importa e nao e negociavel:

1. **teto antes de tudo** - com reserva conservadora para o custo da chamada
   que ainda nao aconteceu. Um teto conferido so depois de gastar teria sido
   respeitado apenas na intencao.
2. **cache antes do provedor** - se a pergunta ja foi feita, nao se paga de
   novo, e a resposta e a mesma de antes.
3. **custo do `usage` real** - nunca estimado (secao 5.2, R13).
4. **evento e lancamento na mesma transacao** - o registro cognitivo e o
   dinheiro nascem amarrados (criterio 9), e nenhum dos dois pode existir
   sem o outro.

Nada aqui conhece nome de provedor nem id de modelo: pede-se um TIER, e a
configuracao versionada resolve (secao 3.9).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from ..config.schema import ExperimentConfig
from ..ledger.livro import Uso, fx_micro, registrar_custo_reflexao
from ..settings import Settings
from . import cache, custo as precificacao, tetos
from .provedores import Adaptador, adaptador_de, chave_do_provedor
from .provedores.base import ErroDoProvedor, Pedido, Resposta

log = logging.getLogger(__name__)

# Sobre-estimativa deliberada do numero de tokens de entrada, usada SO na
# reserva do teto. Um token tem cerca de 3,5 caracteres em portugues; contar
# um a cada dois exagera de proposito. A reserva nao e gravada como custo em
# lugar nenhum - so decide se a chamada pode acontecer, e exagerar nela faz o
# teto parar mais cedo, que e o unico lado seguro para errar.
CHARS_POR_TOKEN_PESSIMISTA = 2


class TetoAtingido(Exception):
    """O cerebro para aqui. As maos rapidas continuam (secao 3.6 regra 2)."""

    def __init__(self, veredito: tetos.Veredito) -> None:
        super().__init__(veredito.motivo)
        self.veredito = veredito


class TierNaoConfigurado(Exception):
    pass


@dataclass(frozen=True)
class Chamada:
    event_id: int
    texto: str
    origem: Literal["provedor", "cache"]
    tier: str
    provider: str
    model: str
    uso: Uso
    custo_micro: int
    custo_cents: int
    componentes_sem_preco: tuple[str, ...]

    def como_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "origem": self.origem,
            "tier": self.tier,
            "provider": self.provider,
            "model": self.model,
            "tokens_in": self.uso.tokens_in,
            "tokens_out": self.uso.tokens_out,
            "tokens_cache_read": self.uso.tokens_cache_read,
            "tokens_cache_write": self.uso.tokens_cache_write,
            "custo_micro": self.custo_micro,
            "custo_cents": self.custo_cents,
            "componentes_sem_preco": list(self.componentes_sem_preco),
        }


def resolver_tier(config: ExperimentConfig, tier: str) -> tuple[str, str]:
    """Tier -> (provedor, modelo). O agente nunca escolhe modelo (secao 3.9)."""
    escolha = config.tiers.get(tier)
    if escolha is None:
        raise TierNaoConfigurado(f"tier '{tier}' nao existe na configuracao")
    if not escolha.model:
        raise TierNaoConfigurado(
            f"tier '{tier}' aponta para {escolha.provider} sem modelo definido"
        )
    return escolha.provider, escolha.model


def reserva_conservadora(
    pedido: Pedido, preco: Any
) -> int:
    """Limite superior do custo da chamada, em centavos. Nunca gravado.

    Supoe que a saida ocupa `max_tokens` inteiros ao preco cheio de saida, e
    que a entrada tem o dobro dos tokens que provavelmente tera. E uma
    barreira, nao uma medida.
    """
    chars = len(pedido.sistema) + sum(len(t) for _, t in pedido.mensagens)
    tokens_entrada = chars // CHARS_POR_TOKEN_PESSIMISTA
    micro = (
        Decimal(tokens_entrada) * Decimal(preco.input_usd_per_mtok)
        + Decimal(pedido.max_tokens) * Decimal(preco.output_usd_per_mtok)
    ) / Decimal(1_000_000) * Decimal(1_000_000)
    return -(-int(micro) // 10_000)


def executar(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    node: str,
    tier: str,
    sistema: str,
    mensagens: tuple[tuple[str, str], ...],
    schema: dict,
    schema_nome: str,
    max_tokens: int,
    config: ExperimentConfig,
    settings: Settings,
    parent_event_id: int | None = None,
    adaptador: Adaptador | None = None,
) -> Chamada:
    """Faz (ou reaproveita) uma chamada e deixa tudo registrado."""
    provider, model = resolver_tier(config, tier)
    preco = precificacao.preco_de(config, provider, model)

    pedido = Pedido(
        provider=provider,
        model=model,
        sistema=sistema,
        mensagens=mensagens,
        schema=schema,
        schema_nome=schema_nome,
        max_tokens=max_tokens,
    )

    veredito = tetos.consultar(
        conn,
        run_id=run_id,
        config=config,
        settings=settings,
        custo_previsto_cents=reserva_conservadora(pedido, preco),
    )
    if not veredito.permitido:
        raise TetoAtingido(veredito)

    acerto = cache.buscar(conn, pedido)
    if acerto is not None:
        resposta = acerto.resposta
        custo_micro = acerto.custo_micro_original
        componentes_sem_preco: tuple[str, ...] = ()
        origem: Literal["provedor", "cache"] = "cache"
    else:
        adaptador = adaptador or adaptador_de(provider)
        resposta = adaptador.chamar(
            pedido, api_key=chave_do_provedor(settings, provider)
        )
        conta = precificacao.calcular(resposta.uso, preco)
        custo_micro = conta.total_micro
        componentes_sem_preco = conta.componentes_sem_preco
        origem = "provedor"
        cache.guardar(conn, pedido, resposta, custo_micro=custo_micro)

    custo_cents = -(-custo_micro // 10_000)

    event_id, _ = registrar_custo_reflexao(
        conn,
        run_id=run_id,
        node=node,
        kind="reflexao" if origem == "provedor" else "reflexao_do_cache",
        custo_usd_minor=custo_cents,
        custo_usd_micro=custo_micro,
        fx_rate_micro=fx_micro(config.fx_brl_per_usd),
        fx_rate_date=config.fx_rate_date,
        uso=resposta.uso,
        tier=tier,
        provider=provider,
        model=model,
        parent_event_id=parent_event_id,
        inputs_digest=pedido.chave(),
        outputs_digest=hashlib.sha256(resposta.texto.encode("utf-8")).hexdigest(),
        price_table_version=config.price_table_version,
        # Cache quente nao gasta real nenhum, e afirmar que gastou seria
        # inventar despesa. O livro simulado paga nos dois casos.
        dinheiro_real=(origem == "provedor"),
    )

    chamada = Chamada(
        event_id=event_id,
        texto=resposta.texto,
        origem=origem,
        tier=tier,
        provider=provider,
        model=model,
        uso=resposta.uso,
        custo_micro=custo_micro,
        custo_cents=custo_cents,
        componentes_sem_preco=componentes_sem_preco,
    )
    log.info("cerebro.chamada", extra=chamada.como_dict())
    return chamada


__all__ = [
    "Chamada",
    "ErroDoProvedor",
    "TetoAtingido",
    "TierNaoConfigurado",
    "executar",
    "resolver_tier",
]
