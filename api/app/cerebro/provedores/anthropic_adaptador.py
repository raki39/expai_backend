"""Adaptador da Anthropic. Chama o SDK direto, de dentro do no.

Tres coisas verificadas e registradas em `.aprendizado/claude-api-notas.md`,
que este adaptador respeita:

- `budget_tokens` e **rejeitado com 400** nos modelos em uso. Pensamento
  adaptativo e o default nesses modelos; nao mandamos o campo.
- **Prefill de assistente e rejeitado.** O formato da saida vem de
  `output_config.format`, que e justamente o contrato do `contrato.py`.
- `usage.cache_read_input_tokens` maior que zero e a unica prova de que o
  cache de prompt esta funcionando. Zero em chamada repetida indica
  invalidador silencioso no prefixo, e e defeito, nao detalhe.
- **Minimo do cache**, por modelo: 1024 tokens no tier Sonnet 5, 512 no tier
  Opus 5. Abaixo disso o provedor nao grava e nao avisa. E por isso que
  `prompts.prefixo_curto_demais()` existe - e por isso que o numero mora
  aqui, e nao la: e conhecimento sobre quem atende.

**Normalizacao do uso.** A Anthropic ja entrega `input_tokens` SEM os tokens
lidos do cache, entao `tokens_in` e o proprio `input_tokens`. Os dois numeros
de cache vem separados porque tem precos diferentes. Campo que a resposta nao
trouxer vira `None` - nunca zero (criterio 7c).
"""

from __future__ import annotations

import logging
from typing import Any

from ...ledger.livro import Uso
from .base import ErroDoProvedor, Pedido, Resposta

log = logging.getLogger(__name__)

TIMEOUT_S = 120.0
MAX_RETRIES = 2


def _inteiro_ou_nulo(fonte: Any, campo: str) -> int | None:
    """`None` quando o provedor nao informou. "Nao sei" nao e "foi zero"."""
    valor = getattr(fonte, campo, None)
    return int(valor) if valor is not None else None


class AdaptadorAnthropic:
    provider = "anthropic"

    def chamar(self, pedido: Pedido, *, api_key: str) -> Resposta:
        import anthropic  # local de proposito: ver provedores/__init__.py

        cliente = anthropic.Anthropic(
            api_key=api_key, timeout=TIMEOUT_S, max_retries=MAX_RETRIES
        )
        try:
            resposta = cliente.messages.create(
                model=pedido.model,
                max_tokens=pedido.max_tokens,
                # O ponto de corte do cache fica no FIM do bloco de sistema:
                # e o pedaco identico em toda chamada de todo run.
                system=[
                    {
                        "type": "text",
                        "text": pedido.sistema,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {"role": papel, "content": texto}
                    for papel, texto in pedido.mensagens
                ],
                output_config={
                    "format": {
                        "type": "json_schema",
                        "name": pedido.schema_nome,
                        "schema": pedido.schema,
                    }
                },
            )
        except Exception as erro:  # noqa: BLE001 - o no grava evento de erro
            raise ErroDoProvedor(f"anthropic: {type(erro).__name__}: {erro}") from erro

        texto = "".join(
            bloco.text for bloco in resposta.content if bloco.type == "text"
        )
        uso_bruto = resposta.usage

        return Resposta(
            texto=texto,
            uso=Uso(
                tokens_in=_inteiro_ou_nulo(uso_bruto, "input_tokens"),
                tokens_out=_inteiro_ou_nulo(uso_bruto, "output_tokens"),
                tokens_cache_read=_inteiro_ou_nulo(
                    uso_bruto, "cache_read_input_tokens"
                ),
                tokens_cache_write=_inteiro_ou_nulo(
                    uso_bruto, "cache_creation_input_tokens"
                ),
                # O payload cru fica guardado inteiro: nenhuma normalizacao
                # apaga o que o provedor de fato disse.
                bruto=_serializavel(uso_bruto),
            ),
            bruto={"stop_reason": getattr(resposta, "stop_reason", None)},
        )


def _serializavel(objeto: Any) -> dict:
    if hasattr(objeto, "to_dict"):
        return dict(objeto.to_dict())
    if hasattr(objeto, "model_dump"):
        return dict(objeto.model_dump(mode="json"))
    return {"repr": repr(objeto)}
