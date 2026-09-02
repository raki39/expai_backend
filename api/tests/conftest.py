from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.settings import get_settings

TOKEN = "token-de-teste-0a"
ANTHROPIC = "sk-ant-segredo-de-teste-nao-deve-vazar"
OPENAI = "sk-openai-segredo-de-teste-nao-deve-vazar"


@pytest.fixture
def ambiente(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Ambiente isolado. Variaveis de ambiente vencem o arquivo .env."""
    db = tmp_path / "fase0a.sqlite3"
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "datasets"))
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("API_SERVICE_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC)
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI)
    monkeypatch.setenv("LLM_MAX_USD_ABSOLUTE", "5.00")
    get_settings.cache_clear()
    yield db
    get_settings.cache_clear()


@pytest.fixture
def client(ambiente: Path) -> Iterator[TestClient]:
    # Fabrica, e nao o objeto de modulo: a app precisa ser construida com o
    # ambiente DESTE teste (a lista de origens do CORS e fixada na construcao).
    from app.main import criar_app

    with TestClient(criar_app()) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


@pytest.fixture
def conn(client: TestClient) -> sqlite3.Connection:
    """Conexao viva do app, para inspecionar o banco direto."""
    return client.app.state.conn
