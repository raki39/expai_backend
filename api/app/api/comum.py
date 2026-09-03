"""O que toda rota precisa e nenhuma deveria redefinir.

A conexao por THREAD e o detalhe mais caro deste arquivo, e a razao dele: o
painel dispara catorze chamadas em paralelo e o threadpool do FastAPI as
espalha por threads diferentes. Uma conexao unica entre elas produzia
`sqlite3.InterfaceError` em ~1,4% das requisicoes - defeito que ficou em
producao desde o incremento 6 porque `TestClient` chama uma rota de cada vez.
"""

from __future__ import annotations

import sqlite3

from fastapi import Request

from ..settings import Settings
from ..store import conexao_do_thread


def _conn(request: Request) -> sqlite3.Connection:
    return conexao_do_thread(request.app.state.settings.db_path)


def _settings(request: Request) -> Settings:
    return request.app.state.settings
