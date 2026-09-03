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
from .. import paradas
from .base import Credenciais, ErroDoProvedor, Pedido, Resposta

log = logging.getLogger(__name__)

TIMEOUT_S = 120.0
#: **Zero de proposito, e isto e uma mudanca.** Era 2, e o retry do SDK
#: convivia com o de `reflexao._chamar_com_retry`, criado no incremento 11b -
#: duas politicas empilhadas, ate 9 tentativas, e a de dentro invisivel: ela
#: nao loga, nao classifica e nao aparece no evento de parada.
#:
#: Uma politica, num lugar. A nossa e a que fica, porque e ela que le
#: `erro.transitorio`, escreve `cerebro.retry` no log e alimenta
#: `stop_category`. Deixar a do SDK ligada faria o numero de tentativas que
#: o teste afirma ser 3 valer 9 na producao - o valor deixando de descrever o
#: comportamento, que e o padrao que este projeto ja registrou dez vezes.
MAX_RETRIES = 0


def _inteiro_ou_nulo(fonte: Any, campo: str) -> int | None:
    """`None` quando o provedor nao informou. "Nao sei" nao e "foi zero"."""
    valor = getattr(fonte, campo, None)
    return int(valor) if valor is not None else None


class AdaptadorAnthropic:
    provider = "anthropic"

    def chamar(self, pedido: Pedido, *, credenciais: Credenciais) -> Resposta:
        import anthropic  # local de proposito: ver provedores/__init__.py

        cliente = anthropic.Anthropic(
            api_key=credenciais.api_key,
            default_headers=credenciais.cabecalhos or None,
            timeout=TIMEOUT_S,
            max_retries=MAX_RETRIES,
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
                # SEM `name` aqui. A OpenAI exige nome no `json_schema`; esta
                # API recusa o campo com 400 ("Extra inputs are not
                # permitted"). O `schema_nome` do pedido segue existindo -
                # o outro adaptador precisa dele, e ele entra na chave do
                # cache -, so nao viaja nesta requisicao. E a diferenca de
                # forma que um wrapper generico teria escondido ate a
                # primeira chamada real.
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": pedido.schema,
                    }
                },
            )
        except Exception as erro:  # noqa: BLE001 - o no grava evento de parada
            categoria, transitorio = paradas.classificar(erro)
            raise ErroDoProvedor(
                f"anthropic: {type(erro).__name__}: {erro}",
                categoria=categoria,
                transitorio=transitorio,
            ) from erro

        texto = "".join(
            bloco.text for bloco in resposta.content if bloco.type == "text"
        )
        if not texto:
            # Sem isto a resposta vazia chega a validacao e vira "Invalid JSON
            # at column 0", que manda procurar erro de schema quando o
            # problema e outro: o pensamento adaptativo consumiu `max_tokens`
            # antes de sair uma linha de texto. A causa esta no `stop_reason`,
            # e e ele que precisa aparecer.
            raise ErroDoProvedor(
                # Categoria propria: nao e schema e nao e falha de rede. Sem
                # ela este caso volta a chegar disfarcado de erro de JSON e
                # manda procurar no contrato, que esta correto.
                categoria=paradas.MAX_TOKENS,
                transitorio=False,
                mensagem="anthropic: resposta sem bloco de texto"
                f" (stop_reason={getattr(resposta, 'stop_reason', None)!r},"
                f" blocos={[b.type for b in resposta.content]}). Com"
                " stop_reason='max_tokens', o limite de saida acabou antes de"
                " o modelo escrever a resposta - o pensamento conta nele."
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
