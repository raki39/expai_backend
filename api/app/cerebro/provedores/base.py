"""O contrato que todo adaptador cumpre, e nada alem dele.

O `Pedido` e o que identifica uma chamada. Sua chave e o hash do conteudo
INTEIRO - provedor, modelo, sistema, mensagens, schema e limite de saida -,
e e ela que enderece o cache. Trocar qualquer byte do pedido troca a chave,
entao um acerto de cache so acontece quando a pergunta e literalmente a mesma
(criterio 4).

A chave NAO inclui a chave de API, obviamente, e nao inclui nada de ambiente:
segredo nunca entra em valor derivado que possa ser gravado ou logado
(secao 10.2.4).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from ...ledger.livro import Uso


@dataclass(frozen=True)
class Pedido:
    """Uma chamada de modelo, em forma canonica e enderecavel."""

    provider: str
    model: str
    sistema: str
    mensagens: tuple[tuple[str, str], ...]  # (papel, texto)
    schema: dict[str, Any]
    schema_nome: str
    max_tokens: int

    def payload(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "sistema": self.sistema,
            "mensagens": [list(m) for m in self.mensagens],
            "schema": self.schema,
            "schema_nome": self.schema_nome,
            "max_tokens": self.max_tokens,
        }

    def canonico(self) -> str:
        return json.dumps(
            self.payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    def chave(self) -> str:
        return hashlib.sha256(self.canonico().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Credenciais:
    """O que um provedor exige para autenticar. Nem sempre e so uma chave.

    Descoberto na primeira chamada real: uma chave da Anthropic ligada a
    IDENTIDADE (e nao ao workspace) e recusada com 400 sem o cabecalho
    `anthropic-workspace-id`. Um parametro `api_key` solto teria obrigado a
    contrabandear isso por variavel global ou por um segundo argumento
    especifico de um provedor dentro da interface de todos.

    Nada daqui e gravado, logado ou devolvido em qualquer resposta.
    """

    api_key: str
    cabecalhos: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Resposta:
    """O que voltou. `texto` e o JSON cru; `uso` ja esta normalizado."""

    texto: str
    uso: Uso
    bruto: dict[str, Any] = field(default_factory=dict)


class ErroDoProvedor(Exception):
    """Falha de chamada. O no que a recebe grava evento de erro e para."""


class Adaptador(Protocol):
    provider: str

    def chamar(self, pedido: Pedido, *, credenciais: Credenciais) -> Resposta:
        ...
