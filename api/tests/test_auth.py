"""Criterio 4 do incremento 0: sem token, 401 em TODOS os endpoints.

A `api` nao tem dominio publico, mas "nao ter dominio" nao e autenticacao.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .conftest import TOKEN

ROTAS_GET = ["/api/health", "/api/config", "/api/config/history", "/api/sentinel"]


@pytest.mark.parametrize("rota", ROTAS_GET)
def test_sem_credencial_401(client: TestClient, rota: str) -> None:
    r = client.get(rota, headers={"Authorization": ""})
    assert r.status_code == 401, rota


@pytest.mark.parametrize("rota", ROTAS_GET)
def test_credencial_errada_401(client: TestClient, rota: str) -> None:
    r = client.get(rota, headers={"Authorization": "Bearer token-errado"})
    assert r.status_code == 401, rota


@pytest.mark.parametrize("rota", ROTAS_GET)
def test_credencial_certa_200(client: TestClient, rota: str) -> None:
    assert client.get(rota).status_code == 200, rota


def test_post_tambem_exige_token(client: TestClient) -> None:
    r = client.post(
        "/api/sentinel", json={"label": "x"}, headers={"Authorization": ""}
    )
    assert r.status_code == 401

    r = client.post(
        "/api/config",
        json={"author": "teste", "changes": {"default_seed": 7}},
        headers={"Authorization": ""},
    )
    assert r.status_code == 401


def test_esquema_diferente_de_bearer_401(client: TestClient) -> None:
    r = client.get("/api/health", headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


def test_health_nao_e_excecao(client: TestClient) -> None:
    """Um endpoint aberto e um endpoint aberto, mesmo so com metadados."""
    assert client.get("/api/health", headers={"Authorization": ""}).status_code == 401


def test_sem_documentacao_publica(client: TestClient) -> None:
    """Superficie minima: uma coisa a menos para proteger."""
    for rota in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(rota).status_code == 404, rota
