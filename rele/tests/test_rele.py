"""Testes do rele (ADR 0029, incremento 16)."""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error

import pytest

from rele import binance, envio

PASSO = 900_000
T0 = 1_756_000_000_000 // PASSO * PASSO


def kline_bruta(i: int, close: str = "60000.50") -> list:
    """O formato exato que a Binance devolve: lista de strings."""
    return [
        T0 + i * PASSO, "60000.00", "60001.00", "59999.00", close,
        "10.5", T0 + (i + 1) * PASSO - 1, "630000.00", 42, "5.0", "300000.0", "0",
    ]


def resposta(linhas: list) -> bytes:
    return json.dumps(linhas).encode()


# ============================================== o corte da barra em formacao

def test_a_barra_em_FORMACAO_e_descartada_aqui():
    """E a razao de `buscar` existir em vez de um `json.loads` solto.

    A Binance devolve a barra em formacao junto com as fechadas, e ela muda a
    cada negocio. Se chegasse ao fluxo, a proxima tentativa levantaria
    `DivergenciaDeConteudo` na api - erro alto, apontando para corrupcao de
    dado em vez de para aqui.
    """
    # `agora` no meio da barra 3: fechadas sao 0, 1, 2. A 3 esta em formacao.
    agora = T0 + 3 * PASSO + 60_000
    ks = binance.buscar(
        "BTCUSDT", "15m", agora_ms=agora, interval_ms=PASSO,
        buscar_bytes=lambda _u, _t: resposta([kline_bruta(i) for i in range(4)]),
    )
    assert [k.open_time_ms for k in ks] == [T0, T0 + PASSO, T0 + 2 * PASSO]


def test_barra_exatamente_fechada_ENTRA():
    """A fronteira e inclusiva: quem fechou, entra."""
    # `agora` exatamente na abertura da barra 3 -> a 2 fechou.
    agora = T0 + 3 * PASSO
    ks = binance.buscar(
        "BTCUSDT", "15m", agora_ms=agora, interval_ms=PASSO,
        buscar_bytes=lambda _u, _t: resposta([kline_bruta(i) for i in range(4)]),
    )
    assert [k.open_time_ms for k in ks] == [T0, T0 + PASSO, T0 + 2 * PASSO]


# ======================================================= inteiros exatos

def test_preco_vira_inteiro_SEM_ponto_flutuante():
    """Regra 5, e a razao aparece aqui.

    O hash canonico do snapshot compara inteiros byte a byte. `float("0.1") *
    10**8` nao da 10000000 exato em toda plataforma, e duas maquinas
    discordariam sobre o mesmo dado.
    """
    ks = binance.buscar(
        "BTCUSDT", "15m", agora_ms=T0 + 2 * PASSO, interval_ms=PASSO,
        buscar_bytes=lambda _u, _t: resposta([kline_bruta(0, "0.1")]),
    )
    assert ks[0].close == 10_000_000
    assert isinstance(ks[0].close, int)


def test_as_escalas_sao_as_MESMAS_da_ingestao_historica():
    """Escala diferente faria o mesmo intervalo ter hashes diferentes.

    Elas entram no hash canonico do snapshot (ADR 0029, garantia 2), e a
    ingestao historica usa 8 para preco e volume.
    """
    assert binance.PRICE_SCALE_EXP == 8
    assert binance.VOLUME_SCALE_EXP == 8


# ============================================================ jurisdicao

def _urlopen_que_falha(codigo: int, razao: str = ""):
    """Substitui `urlopen`, e nao `_buscar`.

    A primeira versao destes testes injetava em `buscar_bytes`, que E a funcao
    que faz o mapeamento de erro - entao o falso substituia exatamente o codigo
    sob teste, e a excecao passava crua. O teste falhava mostrando o proprio
    `HTTPError`, e nao havia mapeamento nenhum sendo exercitado.
    """
    def falso(_req, timeout=None):
        raise urllib.error.HTTPError(
            "https://api.binance.com/api/v3/klines", codigo, razao, {}, None
        )  # type: ignore[arg-type]

    return falso


def test_451_e_bloqueio_por_jurisdicao_e_nao_transitorio(monkeypatch):
    """O rele nasce sabendo o que o coletor aprendeu no primeiro deploy."""
    monkeypatch.setattr(binance.urllib.request, "urlopen",
                        _urlopen_que_falha(451))
    with pytest.raises(binance.BloqueioPorJurisdicao) as e:
        binance.buscar("BTCUSDT", "15m", agora_ms=T0, interval_ms=PASSO)
    assert "Singapura" in str(e.value), "a mensagem tem de dizer a ACAO"
    assert "NAO adianta repetir" in str(e.value)


def test_outro_erro_http_e_transitorio(monkeypatch):
    monkeypatch.setattr(binance.urllib.request, "urlopen",
                        _urlopen_que_falha(503, "unavailable"))
    with pytest.raises(binance.ErroDeFonte) as e:
        binance.buscar("BTCUSDT", "15m", agora_ms=T0, interval_ms=PASSO)
    assert not isinstance(e.value, binance.BloqueioPorJurisdicao)
    assert "503" in str(e.value)


def test_falha_de_rede_tambem_e_transitoria(monkeypatch):
    def sem_rede(_req, timeout=None):
        raise OSError("nome nao resolve")

    monkeypatch.setattr(binance.urllib.request, "urlopen", sem_rede)
    with pytest.raises(binance.ErroDeFonte) as e:
        binance.buscar("BTCUSDT", "15m", agora_ms=T0, interval_ms=PASSO)
    assert not isinstance(e.value, binance.BloqueioPorJurisdicao)


def test_resposta_malformada_e_erro_e_nao_lista_vazia():
    """Lista vazia seria lida como "nao ha barra nova", que e outra coisa."""
    with pytest.raises(binance.ErroDeFonte):
        binance.buscar("BTCUSDT", "15m", agora_ms=T0, interval_ms=PASSO,
                       buscar_bytes=lambda _u, _t: b"nao e json")
    with pytest.raises(binance.ErroDeFonte):
        binance.buscar("BTCUSDT", "15m", agora_ms=T0, interval_ms=PASSO,
                       buscar_bytes=lambda _u, _t: resposta([[1, 2]]))


# ============================================================= assinatura

DESTINO = envio.Destino(
    base_url="https://api.exemplo", token="tok", segredo="segredo-de-teste"
)


def test_os_bytes_assinados_sao_OS_MESMOS_enviados():
    """Serializar duas vezes faria a verificacao falhar por espacamento.

    E o modo de falha seria confuso: pareceria credencial errada, e seria
    formatacao de JSON.
    """
    capturado: dict = {}

    def pedir(req, _t):
        capturado["corpo"] = req.data
        capturado["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return 202, b'{"aceitas": 1, "repetidas": 0}'

    ks = [binance.Kline(T0, 1, 2, 1, 2, 1, 1, 1)]
    envio.enviar(DESTINO, venue="binance", symbol="BTCUSDT", timeframe="15m",
                 interval_ms=PASSO, origem="ao_vivo", klines=ks,
                 agora_ms=1_700_000_000_000, pedir=pedir)

    corpo = capturado["corpo"]
    h = capturado["headers"]
    esperada = hmac.new(
        DESTINO.segredo.encode(),
        str(h["x-rele-carimbo"]).encode() + b"\n"
        + h["x-rele-nonce"].encode() + b"\n" + corpo,
        hashlib.sha256,
    ).hexdigest()
    assert h["x-rele-assinatura"] == esperada


def test_cada_envio_usa_nonce_NOVO():
    """Nonce reusado seria recusado pela api como replay."""
    vistos = set()

    def pedir(req, _t):
        vistos.add({k.lower(): v for k, v in req.headers.items()}["x-rele-nonce"])
        return 202, b"{}"

    ks = [binance.Kline(T0, 1, 2, 1, 2, 1, 1, 1)]
    for _ in range(5):
        envio.enviar(DESTINO, venue="b", symbol="S", timeframe="15m",
                     interval_ms=PASSO, origem="ao_vivo", klines=ks, pedir=pedir)
    assert len(vistos) == 5


def test_409_vira_DivergenciaRecusada_e_diz_para_NAO_reenviar():
    def pedir(_req, _t):
        return 409, b'{"detail": "append-only"}'

    ks = [binance.Kline(T0, 1, 2, 1, 2, 1, 1, 1)]
    with pytest.raises(envio.DivergenciaRecusada) as e:
        envio.enviar(DESTINO, venue="b", symbol="S", timeframe="15m",
                     interval_ms=PASSO, origem="ao_vivo", klines=ks, pedir=pedir)
    assert "NAO reenviar" in str(e.value)


def test_o_ponto_de_retomada_e_PERGUNTADO_e_nao_lembrado():
    """O estado de verdade e o da api.

    Um rele que guardasse o proprio ponto divergiria no primeiro desencontro,
    e a divergencia apareceria como lacuna que ninguem consegue explicar.
    """
    def pedir(req, _t):
        assert "/api/aovivo/ponto" in req.full_url
        assert req.headers["Authorization"] == "Bearer tok"
        return 200, json.dumps({"retomar_de_ms": T0 + 5 * PASSO}).encode()

    assert envio.ponto_de_retomada(
        DESTINO, venue="binance", symbol="BTCUSDT", timeframe="15m",
        interval_ms=PASSO, pedir=pedir,
    ) == T0 + 5 * PASSO


# ========================================================= sem estado

def test_o_rele_nao_guarda_estado_e_nao_decide():
    """A lista curta de dependencias torna isso verificavel por leitura."""
    from pathlib import Path

    raiz = Path(__file__).resolve().parents[1] / "rele"
    proibidos = ("sqlite3", "langgraph", "anthropic", "openai", "fastapi")
    for py in raiz.rglob("*.py"):
        texto = py.read_text(encoding="utf-8")
        for termo in proibidos:
            assert f"import {termo}" not in texto, f"{py.name} importa {termo}"


def test_falta_de_credencial_falha_FECHADO(monkeypatch):
    """Um rele sem credencial nao deve subir e tentar."""
    from rele import main

    for n in ("RELE_API_URL", "API_SERVICE_TOKEN", "RELE_HMAC_SECRET"):
        monkeypatch.delenv(n, raising=False)
    with pytest.raises(SystemExit) as e:
        main.destino_do_ambiente()
    assert "FECHADO" in str(e.value)


def test_a_espera_e_um_terco_do_intervalo_da_barra():
    """Checar tres vezes por barra apanha o fechamento sem depender do relogio."""
    from rele import main

    assert main.FRACAO_DO_INTERVALO == 3
    assert main.MAX_LOTE == 500, "o menor entre o teto da Binance e o da rota"
