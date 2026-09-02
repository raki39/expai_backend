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


def test_liveness_responde_sem_credencial(client: TestClient) -> None:
    """A unica rota aberta. Existe para diagnosticar deploy.

    Sem ela, "container morto" e "auth funcionando" sao indistinguiveis.
    """
    r = client.get("/", headers={"Authorization": ""})
    assert r.status_code == 200
    assert r.json() == {"status": "alive", "service": "fase0a-api", "fase": "0A"}


def test_liveness_nao_vaza_nada(client: TestClient) -> None:
    """Ela diz que o processo respondeu, e so isso."""
    corpo = client.get("/", headers={"Authorization": ""}).json()
    proibidos = {
        "schema_version", "config_version", "config_hash", "db_path",
        "credenciais_configuradas", "volume_gravavel", "cors_allowed_origins",
        "app_env", "build",
    }
    assert proibidos & set(corpo) == set()


def test_dados_reais_continuam_exigindo_token(client: TestClient) -> None:
    """Liveness aberta nao afrouxa nada: /api/health segue fechado."""
    assert client.get("/api/health", headers={"Authorization": ""}).status_code == 401


def test_sem_documentacao_publica(client: TestClient) -> None:
    """Superficie minima: uma coisa a menos para proteger."""
    for rota in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(rota).status_code == 404, rota


# ------------------------------------------------------------------- CORS


def test_cors_desligado_por_padrao(client: TestClient) -> None:
    """Sem CORS_ALLOWED_ORIGINS, nenhuma origem e liberada.

    Com o proxy no servidor, o navegador nao chama a api direto e isto nunca
    e exercitado. O default fechado garante que ligar CORS seja um ato
    explicito, nunca um acidente.
    """
    r = client.get("/api/health", headers={"Origin": "https://qualquer.app"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
    assert client.get("/api/health").json()["cors_allowed_origins"] == []


def test_cors_libera_apenas_origem_da_lista(
    ambiente, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.main import criar_app
    from app.settings import get_settings

    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "https://painel.vercel.app, http://localhost:3000",
    )
    get_settings.cache_clear()

    with TestClient(criar_app()) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})

        permitida = c.get(
            "/api/health", headers={"Origin": "https://painel.vercel.app"}
        )
        assert permitida.headers["access-control-allow-origin"] == (
            "https://painel.vercel.app"
        )

        outra = c.get("/api/health", headers={"Origin": "http://localhost:3000"})
        assert outra.headers["access-control-allow-origin"] == "http://localhost:3000"

        negada = c.get("/api/health", headers={"Origin": "https://invasor.app"})
        assert "access-control-allow-origin" not in {
            k.lower() for k in negada.headers
        }


def test_cors_nunca_usa_curinga(ambiente, monkeypatch: pytest.MonkeyPatch) -> None:
    """Curinga com credencial e invalido no protocolo e desleixado na pratica."""
    from app.main import criar_app
    from app.settings import get_settings

    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://painel.vercel.app")
    get_settings.cache_clear()

    with TestClient(criar_app()) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        r = c.get("/api/health", headers={"Origin": "https://painel.vercel.app"})
        assert r.headers["access-control-allow-origin"] != "*"


def test_cors_nao_substitui_autenticacao(
    ambiente, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Origem liberada continua precisando de credencial.

    CORS e politica do navegador sobre LER resposta; nao autoriza ninguem.
    """
    from app.main import criar_app
    from app.settings import get_settings

    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://painel.vercel.app")
    get_settings.cache_clear()

    with TestClient(criar_app()) as c:
        r = c.get("/api/health", headers={"Origin": "https://painel.vercel.app"})
        assert r.status_code == 401
