"""Adaptadores de provedor. Um por SDK, chamado direto de dentro do no.

Sem wrapper generico de modelo, e a decisao e da CLAUDE.md: R13 e a secao 5.2
exigem tokens de entrada, saida e cache lidos do `usage` REAL, e wrappers
apagam justamente a diferenca de semantica de cache entre provedores - a
Anthropic entrega `input_tokens` ja sem os lidos do cache, a OpenAI entrega
`prompt_tokens` com eles dentro. Um adaptador que nao soubesse disso
reportaria o mesmo numero com significados diferentes.

Este e o unico pacote do projeto onde nome de provedor aparece fora da
configuracao, e nao ha como ser diferente: e a camada cuja funcao e conhecer
os SDKs. O que a secao 3.9 proibe e o codigo de DECISAO conhecer provedor ou
modelo - o grafo pede um tier, a configuracao resolve, e o teste do criterio 7
confere que nenhum id de modelo aparece fora de `app/config`.
"""

from __future__ import annotations

from ...settings import Settings
from .base import Adaptador, Pedido, Resposta

__all__ = [
    "Adaptador",
    "Pedido",
    "Resposta",
    "adaptador_de",
    "chave_do_provedor",
]


class ProvedorIndisponivel(Exception):
    """Nao ha adaptador, ou nao ha chave, para este provedor."""


def chave_do_provedor(settings: Settings, provider: str) -> str:
    """A chave vive so em env, e so passa daqui para o SDK (secao 10.2.4).

    Nunca e gravada, nunca entra em log, nunca entra na chave do cache.

    Mora aqui, e nao no motor de reflexao, porque saber que a chave da
    Anthropic se chama ANTHROPIC_API_KEY e conhecimento sobre QUEM ATENDE -
    a mesma razao pela qual os adaptadores existem. Com isto o motor de
    reflexao ficou sem uma unica mencao a provedor, e ha teste que confere.
    """
    campo = {"anthropic": "anthropic_api_key", "openai": "openai_api_key"}.get(
        provider
    )
    if campo is None:
        raise ProvedorIndisponivel(f"provedor sem chave configuravel: {provider!r}")
    valor = getattr(settings, campo).get_secret_value()
    if not valor:
        raise ProvedorIndisponivel(
            f"chave do provedor {provider} ausente do ambiente"
        )
    return valor


def adaptador_de(provider: str) -> Adaptador:
    """Resolve o adaptador pelo nome do provedor gravado na configuracao.

    O import e local por dois motivos. Primeiro, nenhum SDK de provedor deve
    ser carregado por importar a API - o teste do criterio 1 do incremento 4
    afirma que rodar um baseline nao carrega provedor nenhum, e uma cadeia de
    imports no topo tornaria essa afirmacao falsa sem ninguem perceber.
    Segundo, um provedor nao configurado nao deve impedir o servico de subir.
    """
    if provider == "anthropic":
        from .anthropic_adaptador import AdaptadorAnthropic

        return AdaptadorAnthropic()
    if provider == "openai":
        from .openai_adaptador import AdaptadorOpenAI

        return AdaptadorOpenAI()
    raise ValueError(f"provedor sem adaptador: {provider!r}")
