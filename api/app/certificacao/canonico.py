"""O que um certificado pode citar: CONTEÚDO, e nunca id de linha da cópia.

> *"conteúdo canônico no certificado, nunca IDs temporários"* — o usuário,
> 2026-09-10

## O defeito que isto conserta, medido nos certificados SELADOS

Lido de `/api/relatorio/checkpoint` em 2026-09-10, antes da janela:

| escopo | onde roda | o que o manifesto selado citava |
|---|---|---|
| a1a | cópia | `duplicata.hypothesis_original = 57` — produção tem 41 hipóteses |
| a2 | cópia | `corridas[].run_id = 82` e `99` |

O ADR 0040 diz *"pela `chave` do catálogo, nunca por id temporário"*, e
`_caso_canonico` cumpria isso **no nível do caso**: tirava `run_id` e
`hypothesis_id` do `ResultadoDeUm`. Mas o `observado` ia inteiro, e carregava os
ids de dentro dele. A garantia valia na camada que alguém olhou, e não na de
baixo — a forma de sempre.

## Por que REMOVER, e não traduzir

Um id de linha criada na cópia não tem tradução: a linha não existe fora dela.
E o conteúdo que o id apontava já está ao lado — `content_hash_original` na
duplicata; `operacoes_alvo` e os percentis nas corridas do B1.

## E a remoção é PUBLICADA

`sem_ids_temporarios` devolve os caminhos removidos, e o manifesto os publica.
Um certificado que limpasse calado deixaria o leitor sem saber que havia algo
ali.

## A regra é por FORMA, com uma lista positiva do que fica

Uma lista fixa de nomes (`run_id`, `hypothesis_id`...) deixaria passar o
primeiro id com nome novo — `b1_run_id`, digamos — e ele entraria no
certificado sem que nada acusasse. Então sai **toda** chave com forma de id
(`..._id`, `..._ids`, `..._original`) e valor inteiro, e só ficam as que
apontam para linha do banco OFICIAL que a cópia **copiou** em vez de criar:
`config_version_id` e `dataset_id`. O a3 cita `config_version_id` 2, 3 e 4, e
eles existem em produção.
"""

from __future__ import annotations

import re
from typing import Any

#: Ids de linhas do banco OFICIAL, que a cópia recebe por `backup()` e nunca
#: cria. Continuam válidos depois de a cópia ser descartada.
PERSISTENTES = frozenset({"config_version_id", "dataset_id"})

_FORMA_DE_ID = re.compile(r"(^|_)ids?$|_original$")


def _e_id(chave: str, valor: Any) -> bool:
    if chave in PERSISTENTES or not _FORMA_DE_ID.search(chave):
        return False
    if isinstance(valor, bool):
        return False
    if isinstance(valor, int):
        return True
    return isinstance(valor, list) and bool(valor) and all(
        isinstance(x, int) and not isinstance(x, bool) for x in valor
    )


def sem_ids_temporarios(valor: Any) -> tuple[Any, list[str]]:
    """O mesmo valor sem ids de linha da cópia, e a lista do que saiu."""
    removidos: list[str] = []

    def limpar(v: Any, caminho: str) -> Any:
        if isinstance(v, dict):
            fora = {}
            for k, x in v.items():
                if _e_id(k, x):
                    removidos.append(f"{caminho}.{k}")
                    continue
                fora[k] = limpar(x, f"{caminho}.{k}")
            return fora
        if isinstance(v, (list, tuple)):
            return [limpar(x, f"{caminho}[{i}]") for i, x in enumerate(v)]
        return v

    return limpar(valor, ""), removidos
