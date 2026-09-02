"""Adaptador da OpenAI. O segundo provedor exigido pela secao 3.9.

**Exercitado contra a API real em 2026-09-02** (criterio 7b, ADR 0009): a
resposta validou contra o MESMO schema da regra e os campos de uso chegaram ao
normalizador. "Viabilidade nunca exercitada e suposicao" - agora nao e mais.
O teste que faz isso e marcado `rede` e so roda com o interruptor ligado,
porque gasta dinheiro de verdade.

Usa **chat.completions**, e nao a API de respostas, por estabilidade: e a
superficie que menos mudou de forma entre versoes do SDK, e este adaptador
existe para provar que a troca de provedor e viavel, nao para explorar a
fronteira da API deles.

**A diferenca que justifica dois adaptadores em vez de um wrapper:**
`prompt_tokens` da OpenAI INCLUI os tokens lidos do cache; `input_tokens` da
Anthropic NAO inclui. Somar ou comparar os dois numeros direto seria erro
silencioso no custo por decisao. Aqui a subtracao e feita, e o payload cru
fica guardado inteiro.

A OpenAI nao cobra escrita de cache e nao reporta o numero: `tokens_cache_write`
fica `None` - **indisponivel, nunca zero** (criterio 7c). Gravar zero afirmaria
que nada foi escrito no cache, que e coisa diferente de nao saber.
"""

from __future__ import annotations

import logging
from typing import Any

from ...ledger.livro import Uso
from .base import Credenciais, ErroDoProvedor, Pedido, Resposta

log = logging.getLogger(__name__)

TIMEOUT_S = 120.0
MAX_RETRIES = 2


class AdaptadorOpenAI:
    provider = "openai"

    def chamar(self, pedido: Pedido, *, credenciais: Credenciais) -> Resposta:
        import openai  # local de proposito: ver provedores/__init__.py

        cliente = openai.OpenAI(
            api_key=credenciais.api_key,
            default_headers=credenciais.cabecalhos or None,
            timeout=TIMEOUT_S,
            max_retries=MAX_RETRIES,
        )
        try:
            resposta = cliente.chat.completions.create(
                model=pedido.model,
                max_completion_tokens=pedido.max_tokens,
                messages=[
                    {"role": "system", "content": pedido.sistema},
                    *(
                        {"role": papel, "content": texto}
                        for papel, texto in pedido.mensagens
                    ),
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": pedido.schema_nome,
                        "schema": pedido.schema,
                        "strict": True,
                    },
                },
            )
        except Exception as erro:  # noqa: BLE001 - o no grava evento de erro
            raise ErroDoProvedor(f"openai: {type(erro).__name__}: {erro}") from erro

        uso_bruto = getattr(resposta, "usage", None)
        prompt_tokens = _inteiro_ou_nulo(uso_bruto, "prompt_tokens")
        cache_read = _cache_lido(uso_bruto)

        # `prompt_tokens` inclui o que veio do cache; `tokens_in` e sempre
        # "entrada cobrada ao preco cheio". Se um dos dois nao veio, nao ha
        # subtracao possivel e o campo fica indisponivel.
        if prompt_tokens is None:
            tokens_in = None
        elif cache_read is None:
            tokens_in = prompt_tokens
        else:
            tokens_in = max(prompt_tokens - cache_read, 0)

        return Resposta(
            texto=resposta.choices[0].message.content or "",
            uso=Uso(
                tokens_in=tokens_in,
                tokens_out=_inteiro_ou_nulo(uso_bruto, "completion_tokens"),
                tokens_cache_read=cache_read,
                # Indisponivel: a OpenAI nao reporta escrita de cache.
                tokens_cache_write=None,
                bruto=_serializavel(uso_bruto),
            ),
            bruto={"finish_reason": resposta.choices[0].finish_reason},
        )


def _inteiro_ou_nulo(fonte: Any, campo: str) -> int | None:
    if fonte is None:
        return None
    valor = getattr(fonte, campo, None)
    return int(valor) if valor is not None else None


def _cache_lido(uso: Any) -> int | None:
    detalhes = getattr(uso, "prompt_tokens_details", None)
    return _inteiro_ou_nulo(detalhes, "cached_tokens")


def _serializavel(objeto: Any) -> dict:
    if objeto is None:
        return {}
    if hasattr(objeto, "to_dict"):
        return dict(objeto.to_dict())
    if hasattr(objeto, "model_dump"):
        return dict(objeto.model_dump(mode="json"))
    return {"repr": repr(objeto)}
