"""As rotas, agrupadas por DOMINIO - um modulo por area de `app/`.

Antes eram 33 handlers num arquivo de 1.138 linhas, com os caminhos soltos na
raiz de `/api`: `/lote` e `/creditos` sao do validador e estavam ao lado de
`/curva`; a separacao de dados aparecia como `POST /api/dataset/separacao` e
`GET /api/separacao`, a mesma coisa em dois lugares.

Cada modulo aqui traz seu proprio `APIRouter` com **prefixo** e **tag**. A tag
e o que faz o Swagger desenhar secoes em vez de uma lista de 33 linhas.

`diagnostico` e o unico agrupado por NATUREZA e nao por dominio: e onde vive o
que existe so para provar o substrato e nunca participa do experimento.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...security import exigir_token_de_servico
from . import (
    agente,
    baselines,
    config,
    dataset,
    diagnostico,
    ledger,
    relatorio,
    simulador,
    substrato,
    validador,
)

# A ORDEM aqui e a ordem das secoes no Swagger, e ela conta a historia do
# experimento: substrato -> config -> dados -> dinheiro -> execucao ->
# controle -> agente -> validacao -> relatorio. Diagnostico por ultimo, porque
# nao faz parte dela.
MODULOS = (
    substrato,
    config,
    dataset,
    ledger,
    simulador,
    baselines,
    agente,
    validador,
    relatorio,
    diagnostico,
)

# A dependencia de token vive AQUI, no router raiz, e nao em cada modulo.
# Repetida por modulo, um esquecimento abriria uma secao inteira - e a
# ausencia de uma linha e o defeito mais dificil de ver numa revisao.
router = APIRouter(dependencies=[Depends(exigir_token_de_servico)])

for _modulo in MODULOS:
    router.include_router(_modulo.router)

__all__ = ["router", "MODULOS"]
