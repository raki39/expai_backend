"""Testes da sonda de rede do incremento 1 (TEMPORARIO, sai com a D18).

Nenhum teste aqui toca a rede. Uma suite que depende da internet nao falha
por defeito do codigo, falha por dia ruim - e a partir dai ninguem confia
nela. O `urlopen` e substituido em todos os casos.

O que precisa estar protegido: a sonda tem de sobreviver a QUALQUER desfecho.
Ela existe para medir uma falha; se ela mesma quebra na falha, nao mede nada.
"""

from __future__ import annotations

import email.message
import io
import socket
import urllib.error

import pytest
from fastapi.testclient import TestClient

from app import sonda_rede
from app.sonda_rede import ALVOS, Alvo, sondar, veredito

ALVO = Alvo(nome="teste", url="https://exemplo.invalido/arquivo.zip", porque="teste")


def _headers(**pares: str) -> email.message.Message:
    msg = email.message.Message()
    for chave, valor in pares.items():
        msg[chave.replace("_", "-")] = valor
    return msg


class _RespostaFalsa:
    """Imita o retorno de urlopen, inclusive o uso como context manager."""

    def __init__(self, status: int, corpo: bytes, reason: str = "OK", **cabecalhos):
        self.status = status
        self.reason = reason
        self.headers = _headers(**cabecalhos)
        self._corpo = corpo

    def read(self, n: int = -1) -> bytes:
        return self._corpo[:n] if n and n > 0 else self._corpo

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _fingir(monkeypatch: pytest.MonkeyPatch, resultado):
    """Substitui urlopen. `resultado` e uma resposta ou uma excecao."""

    def falso(requisicao, timeout=None):
        if isinstance(resultado, BaseException):
            raise resultado
        falso.requisicao = requisicao
        return resultado

    monkeypatch.setattr(sonda_rede.urllib.request, "urlopen", falso)
    return falso


# --------------------------------------------------------- desfechos crus


def test_206_com_range_e_lido_como_sucesso(monkeypatch: pytest.MonkeyPatch) -> None:
    _fingir(
        monkeypatch,
        _RespostaFalsa(
            206,
            b"PK\x03\x04binario",
            reason="Partial Content",
            content_range="bytes 0-511/1048576",
        ),
    )
    r = sondar(ALVO)
    assert r["status"] == 206
    assert r["erro"] is None
    assert r["bloqueado_por_jurisdicao"] is False
    assert r["cabecalhos"]["content-range"] == "bytes 0-511/1048576"


def test_451_e_resposta_e_nao_falha(monkeypatch: pytest.MonkeyPatch) -> None:
    """O caso que motiva a sonda inteira: 451 chega como HTTPError."""
    _fingir(
        monkeypatch,
        urllib.error.HTTPError(
            ALVO.url,
            451,
            "Unavailable For Legal Reasons",
            _headers(server="nginx"),
            io.BytesIO(b"Service unavailable from a restricted location."),
        ),
    )
    r = sondar(ALVO)
    assert r["status"] == 451
    assert r["bloqueado_por_jurisdicao"] is True
    # Sem `erro`: o servidor RESPONDEU. Confundir os dois inverteria a leitura.
    assert r["erro"] is None
    assert "restricted location" in r["amostra"]


def test_451_sem_corpo_nao_derruba_a_sonda(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTPError com fp=None nao tem `read`. A sonda ainda tem de reportar."""
    _fingir(
        monkeypatch,
        urllib.error.HTTPError(ALVO.url, 451, "Blocked", _headers(), None),
    )
    r = sondar(ALVO)
    assert r["status"] == 451
    assert r["amostra"] is None
    assert r["erro"] is None


def test_timeout_nao_vira_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _fingir(monkeypatch, socket.timeout())
    r = sondar(ALVO)
    assert r["status"] is None
    assert r["erro"] == "timeout"
    # Sem status, nao se afirma nada sobre bloqueio.
    assert r["bloqueado_por_jurisdicao"] is None


def test_dns_quebrado_e_distinguivel_de_bloqueio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fingir(monkeypatch, urllib.error.URLError(socket.gaierror("nome nao resolvido")))
    r = sondar(ALVO)
    assert r["status"] is None
    assert "gaierror" in r["erro"]


def test_excecao_inesperada_e_capturada(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uma sonda que levanta nao mede. Qualquer erro vira dado."""
    _fingir(monkeypatch, RuntimeError("algo que nao previmos"))
    r = sondar(ALVO)
    assert r["status"] is None
    assert r["erro"] == "RuntimeError: algo que nao previmos"


def test_corpo_binario_vira_hex_legivel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Precisamos poder ver 'PK' e reconhecer um zip de verdade."""
    _fingir(monkeypatch, _RespostaFalsa(200, b"PK\x03\x04\xff\xfe\x00"))
    r = sondar(ALVO)
    assert r["amostra"].startswith("<binario> 504b0304")


def test_user_agent_e_range_vao_na_requisicao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    falso = _fingir(monkeypatch, _RespostaFalsa(206, b"PK"))
    sondar(ALVOS[0])
    enviados = falso.requisicao.headers
    assert enviados["Range"] == "bytes=0-511"
    assert "fase0a-sonda" in enviados["User-agent"]


# ------------------------------------------------------------- veredito


@pytest.mark.parametrize(
    "status, esperado",
    [
        (200, "liberado"),
        (206, "liberado"),
        (451, "bloqueado_por_jurisdicao"),
        (403, "resposta_inesperada"),
        (404, "resposta_inesperada"),
    ],
)
def test_veredito_por_status(status: int, esperado: str) -> None:
    assert veredito([{"nome": "dump_zip", "status": status}])["acesso_ao_dump"] == esperado


def test_veredito_sem_resposta_nao_decide() -> None:
    """Sem resposta nao ha decisao. Chutar aqui seria o oposto do objetivo."""
    v = veredito([{"nome": "dump_zip", "status": None, "erro": "timeout"}])
    assert v["acesso_ao_dump"] == "sem_resposta"
    assert "indefinida" in v["decisao_d18"]


def test_veredito_ignora_os_outros_alvos() -> None:
    """So o dump decide: a API REST bloqueada nao impede usar os dumps."""
    v = veredito(
        [
            {"nome": "dump_zip", "status": 206},
            {"nome": "api_rest", "status": 451},
        ]
    )
    assert v["acesso_ao_dump"] == "liberado"


# ----------------------------------------------------------------- rota


def test_rota_exige_token(client: TestClient) -> None:
    resposta = client.get("/api/sonda/binance", headers={"Authorization": ""})
    assert resposta.status_code == 401


def test_rota_devolve_evidencia_e_veredito(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fingir(monkeypatch, _RespostaFalsa(206, b"PK\x03\x04"))
    corpo = client.get("/api/sonda/binance").json()

    assert corpo["veredito"]["acesso_ao_dump"] == "liberado"
    assert "Railway" in corpo["veredito"]["decisao_d18"]
    # A evidencia crua acompanha o veredito: quem decide confere a leitura.
    assert [s["nome"] for s in corpo["sondas"]] == [a.nome for a in ALVOS]
    assert all(s["status"] == 206 for s in corpo["sondas"])
    assert "temporario" in corpo
