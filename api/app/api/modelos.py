"""Modelos de PEDIDO das rotas.

Ficam juntos, e nao ao lado do handler que os usa, porque alguns sao
usados por um handler diferente daquele em cujo bloco foram escritos -
`PedidoRun` estava no meio da ingestao de dataset. Espalhados, o proximo
split repetiria o mesmo engano.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any


class AlteracaoConfig(BaseModel):
    """Pedido de nova versao de configuracao."""

    author: str = Field(min_length=1, max_length=120)
    changes: dict[str, Any] = Field(min_length=1)
    note: str = Field(default="", max_length=500)


class PedidoReancoragem(BaseModel):
    author: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=500)


class PedidoIngestao(BaseModel):
    """Disparo da ingestao unica.

    `aceitar_lacunas` e o "relatorio aceito" do criterio 3: a decisao de
    prosseguir com serie incompleta e de uma pessoa, tem autor e fica no log.
    Por isso o default e recusar.
    """

    author: str = Field(min_length=1, max_length=120)
    aceitar_lacunas: bool = False


class PedidoRun(BaseModel):
    author: str = Field(min_length=1, max_length=120)


class PedidoComparacao(BaseModel):
    author: str = Field(min_length=1, max_length=120)
    # Semente diferente reexecuta legitimamente com o MESMO config_hash
    # (secao 14.4.1). `None` usa a da configuracao.
    semente: int | None = None


class PedidoAgente(BaseModel):
    author: str = Field(min_length=1, max_length=120)


class PedidoProva(BaseModel):
    """Semente da prova de reprodutibilidade.

    Opcional: ausente usa `default_seed` da config. E entrada do run, e nao
    campo do `config_hash` - e o que torna enunciavel a segunda metade da
    prova, "digest diferente com config_hash igual".
    """

    semente: int | None = None


class PedidoB4(BaseModel):
    """O braco de controle. Sem teto de gasto porque nao ha gasto.

    `semente` opcional pelo mesmo motivo de `PedidoComparacao`: ela e entrada
    do run e nao campo do `config_hash`, entao trocar de semente reexecuta
    legitimamente sob a mesma config - e e o que torna a reprodutibilidade da
    busca enunciavel (R12: mesma semente, mesmo conjunto de hipoteses).
    """

    author: str = Field(min_length=1, max_length=120)
    semente: int | None = None
