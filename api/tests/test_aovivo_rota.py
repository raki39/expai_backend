"""A rota que RECEBE dado: HMAC, replay e validação (ADR 0029, incremento 16).

É a primeira rota do projeto que grava dado vindo de fora. Cada teste aqui
corresponde a uma exigência do usuário na aprovação da alternativa A de
transporte.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.aovivo import assinatura

from .conftest import TOKEN

SEGREDO = "segredo-hmac-do-rele-para-teste"
PASSO = 900_000
T0 = 1_756_000_000_000 // PASSO * PASSO

CABECALHO = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def com_segredo(ambiente, monkeypatch: pytest.MonkeyPatch):
    """O segredo do HMAC vive só no env (regra 15)."""
    from app.settings import get_settings

    monkeypatch.setenv("RELE_HMAC_SECRET", SEGREDO)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def cliente(com_segredo) -> Any:
    from app.main import criar_app

    with TestClient(criar_app()) as c:
        yield c


def corpo(n: int = 3, *, de: int = 0, origem: str = "ao_vivo") -> bytes:
    return json.dumps({
        "venue": "binance", "symbol": "BTCUSDT", "timeframe": "15m",
        "interval_ms": PASSO, "price_scale_exp": 8, "volume_scale_exp": 8,
        "origem": origem,
        "barras": [
            {"open_time_ms": T0 + i * PASSO, "open": 6_000_000_000_000,
             "high": 6_000_000_001_000, "low": 5_999_999_999_000,
             "close": 6_000_000_000_500, "volume": 10, "quote_volume": 20,
             "trades": 3}
            for i in range(de, de + n)
        ],
    }).encode("utf-8")


def enviar(cliente, bruto: bytes, *, segredo: str = SEGREDO,
           carimbo_ms: int | None = None, nonce: str | None = None,
           assinar_com_outro_corpo: bytes | None = None):
    carimbo = int(time.time() * 1000) if carimbo_ms is None else carimbo_ms
    n = secrets.token_hex(16) if nonce is None else nonce
    alvo = bruto if assinar_com_outro_corpo is None else assinar_com_outro_corpo
    sig = assinatura.assinar(
        assinatura.Pedido(carimbo_ms=carimbo, nonce=n, corpo=alvo), segredo
    )
    return cliente.post(
        "/api/aovivo/barras",
        content=bruto,
        headers={
            **CABECALHO,
            "content-type": "application/json",
            "x-rele-assinatura": sig,
            "x-rele-carimbo": str(carimbo),
            "x-rele-nonce": n,
        },
    )


# =========================================================== o caminho bom

def test_lote_assinado_e_aceito(cliente):
    r = enviar(cliente, corpo(3))
    assert r.status_code == 202, r.text
    d = r.json()
    assert d["aceitas"] == 3 and d["repetidas"] == 0
    assert d["ultima_confirmada_ms"] == T0 + 2 * PASSO


def test_202_e_nao_201_porque_o_lote_pode_ser_todo_repetido(cliente):
    """Dizer `201` afirmaria criação que não houve."""
    assert enviar(cliente, corpo(3)).status_code == 202
    r = enviar(cliente, corpo(3))
    assert r.status_code == 202
    assert r.json() == {**r.json(), "aceitas": 0, "repetidas": 3}


# ================================================================== HMAC

def test_sem_assinatura_e_recusado(cliente):
    r = cliente.post("/api/aovivo/barras", content=corpo(),
                     headers={**CABECALHO, "content-type": "application/json"})
    assert r.status_code == 422  # cabeçalho obrigatório ausente


def test_assinatura_de_outro_segredo_e_recusada(cliente):
    r = enviar(cliente, corpo(), segredo="segredo-errado")
    assert r.status_code == 401
    assert "assinatura nao bate" in r.json()["detail"]


def test_corpo_ALTERADO_depois_de_assinado_e_recusado(cliente):
    """O HMAC amarra a credencial ao pedido EXATO.

    É o que o token de serviço sozinho não faz: capturado, um Bearer vale para
    qualquer corpo. Aqui o corpo entra na mensagem assinada.
    """
    r = enviar(cliente, corpo(3), assinar_com_outro_corpo=corpo(2))
    assert r.status_code == 401
    assert "assinatura nao bate" in r.json()["detail"]


def test_o_corpo_assinado_e_o_CRU_e_nao_o_reserializado(cliente):
    """Assinar JSON reconstruído faria a verificação depender de formatação.

    Dois espaçamentos diferentes do mesmo JSON são o mesmo objeto e bytes
    diferentes — e a primeira divergência de biblioteca quebraria tudo,
    parecendo credencial errada.
    """
    original = corpo(2)
    reformatado = json.dumps(json.loads(original), indent=2).encode()
    assert original != reformatado
    assert json.loads(original) == json.loads(reformatado)

    # Assinado sobre o original, enviado reformatado: recusa.
    r = enviar(cliente, reformatado, assinar_com_outro_corpo=original)
    assert r.status_code == 401


# ====================================================== replay: as duas metades

def test_carimbo_velho_e_recusado(cliente):
    """A janela limita por quanto tempo um pedido capturado serve."""
    velho = int(time.time() * 1000) - assinatura.TOLERANCIA_MS - 60_000
    r = enviar(cliente, corpo(), carimbo_ms=velho)
    assert r.status_code == 401
    assert "fora da janela" in r.json()["detail"]


def test_carimbo_no_FUTURO_tambem_e_recusado(cliente):
    """A janela é de valor absoluto: relógio adiantado não compra validade."""
    futuro = int(time.time() * 1000) + assinatura.TOLERANCIA_MS + 60_000
    r = enviar(cliente, corpo(), carimbo_ms=futuro)
    assert r.status_code == 401
    assert "fora da janela" in r.json()["detail"]


def test_nonce_repetido_DENTRO_da_janela_e_recusado(cliente):
    """A metade que a janela não cobre.

    Sem o nonce, capturar e reenviar em dez segundos passaria — e a janela não
    pode ser curta o bastante para impedir isso, porque relógios divergem. O
    coletor mediu offset de 2.450 ms numa máquina real.
    """
    n = secrets.token_hex(16)
    carimbo = int(time.time() * 1000)
    assert enviar(cliente, corpo(2), nonce=n, carimbo_ms=carimbo).status_code == 202

    # MESMO pedido, byte a byte, com o mesmo nonce: replay.
    r = enviar(cliente, corpo(2), nonce=n, carimbo_ms=carimbo)
    assert r.status_code == 401
    assert "ja foi usado" in r.json()["detail"]


def test_as_duas_metades_sao_NECESSARIAS(cliente):
    """Nenhuma sozinha protege, e o teste mostra as duas em ação.

    Carimbo válido + nonce novo passa; carimbo válido + nonce repetido é
    recusado; carimbo velho é recusado mesmo com nonce novo.
    """
    ok = enviar(cliente, corpo(1, de=0))
    assert ok.status_code == 202

    # nonce novo, carimbo velho
    velho = int(time.time() * 1000) - assinatura.TOLERANCIA_MS - 1
    assert enviar(cliente, corpo(1, de=1), carimbo_ms=velho).status_code == 401

    # carimbo bom, nonce reusado
    n = secrets.token_hex(16)
    c = int(time.time() * 1000)
    assert enviar(cliente, corpo(1, de=2), nonce=n, carimbo_ms=c).status_code == 202
    assert enviar(cliente, corpo(1, de=3), nonce=n, carimbo_ms=c).status_code == 401


def test_nonce_fora_do_formato_e_recusado(cliente):
    for ruim in ("curto", "x" * 200, "com-hifen-nao-alnum"):
        r = enviar(cliente, corpo(), nonce=ruim)
        assert r.status_code == 401, ruim
        assert "nonce fora do formato" in r.json()["detail"]


def test_o_nonce_so_e_gravado_DEPOIS_de_a_assinatura_passar(
    cliente, conn: sqlite3.Connection
):
    """Antes disso, qualquer um encheria a tabela.

    E a ordem também não vaza informação: conferir a janela antes da
    assinatura diria a quem não tem o segredo se o carimbo dele estava bom.
    """
    antes = conn.execute("SELECT COUNT(*) FROM rele_nonce").fetchone()[0]
    n = secrets.token_hex(16)
    assert enviar(cliente, corpo(), segredo="errado", nonce=n).status_code == 401
    depois = conn.execute("SELECT COUNT(*) FROM rele_nonce").fetchone()[0]
    assert depois == antes, "nonce de assinatura invalida foi gravado"


def test_o_nonce_antigo_e_PODADO_e_a_excecao_esta_declarada(
    cliente, conn: sqlite3.Connection
):
    """A única tabela do projeto que pode ser apagada.

    Pode porque, fora da janela, o carimbo já recusa — guardar por mais tempo
    não acrescenta proteção e a tabela cresceria sem limite. Ela não é
    registro de experimento, é cache de proteção.
    """
    conn.execute(
        "INSERT INTO rele_nonce (nonce, carimbo_ms, visto_em)"
        " VALUES ('antigo0000000000', 1000, 'x')"
    )
    saíram = assinatura.podar(conn, agora_ms=10_000_000_000)
    assert saíram >= 1
    assert conn.execute(
        "SELECT COUNT(*) FROM rele_nonce WHERE nonce = 'antigo0000000000'"
    ).fetchone()[0] == 0

    # E não há gatilho de append-only nesta tabela, ao contrário do resto.
    gatilhos = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
            " AND tbl_name = 'rele_nonce'"
        )
    ]
    assert gatilhos == [], "a exceção é deliberada e não pode virar acidente"


# ============================================ validação integral pela API

def test_a_api_valida_e_nao_confia_no_rele(cliente):
    """O relé é código nosso, mas roda noutro lugar e fala pela rede."""
    ruim = json.loads(corpo(1))
    ruim["barras"][0]["high"] = 1        # high < low
    r = enviar(cliente, json.dumps(ruim).encode())
    assert r.status_code == 422
    assert "high" in r.json()["detail"]


def test_barra_desalinhada_da_grade_e_recusada(cliente):
    ruim = json.loads(corpo(1))
    ruim["barras"][0]["open_time_ms"] += 1
    r = enviar(cliente, json.dumps(ruim).encode())
    assert r.status_code == 422
    assert "grade" in r.json()["detail"]


def test_divergencia_de_conteudo_e_409_e_nada_e_gravado(
    cliente, conn: sqlite3.Connection
):
    """Erro alto, e não "aceito com aviso"."""
    assert enviar(cliente, corpo(2)).status_code == 202
    antes = conn.execute("SELECT COUNT(*) FROM stream_bar").fetchone()[0]

    # A barra divergente tem de ser VALIDA e diferente. A primeira versao
    # deste teste punha `close` fora de [low, high], e a validacao a recusava
    # com 422 antes de a divergencia ser detectada - o teste passava a medir
    # outra coisa.
    divergente = json.loads(corpo(2))
    divergente["barras"][1].update({
        "open": 7_000_000_000_000, "high": 7_000_000_001_000,
        "low": 6_999_999_999_000, "close": 7_000_000_000_500,
    })
    r = enviar(cliente, json.dumps(divergente).encode())
    assert r.status_code == 409
    assert "append-only" in r.json()["detail"]

    depois = conn.execute("SELECT COUNT(*) FROM stream_bar").fetchone()[0]
    assert depois == antes, "o lote com divergencia entrou pela metade"


def test_lote_vazio_e_recusado(cliente):
    vazio = json.loads(corpo(1))
    vazio["barras"] = []
    r = enviar(cliente, json.dumps(vazio).encode())
    assert r.status_code == 422
    assert "vazio" in r.json()["detail"]


def test_lote_acima_do_teto_e_recusado(cliente):
    from app.api.rotas.aovivo import MAX_BARRAS

    grande = json.loads(corpo(1))
    grande["barras"] = grande["barras"] * (MAX_BARRAS + 1)
    r = enviar(cliente, json.dumps(grande).encode())
    assert r.status_code == 413


# ============================================================ coordenação

def test_o_ponto_de_retomada_diz_de_onde_o_backfill_parte(cliente):
    """Queda do relé vira atraso RECUPERÁVEL, e não lacuna."""
    q = ("?venue=binance&symbol=BTCUSDT&timeframe=15m"
         f"&interval_ms={PASSO}")
    r = cliente.get(f"/api/aovivo/ponto{q}", headers=CABECALHO)
    assert r.status_code == 200
    assert r.json()["ultima_confirmada_ms"] is None
    assert r.json()["retomar_de_ms"] is None

    enviar(cliente, corpo(3))
    r2 = cliente.get(f"/api/aovivo/ponto{q}", headers=CABECALHO)
    assert r2.json()["ultima_confirmada_ms"] == T0 + 2 * PASSO
    assert r2.json()["retomar_de_ms"] == T0 + 3 * PASSO


def test_o_estado_diz_ATRASO_e_nao_lacuna(cliente):
    enviar(cliente, corpo(3))
    r = cliente.get("/api/aovivo/estado", headers=CABECALHO)
    assert r.status_code == 200
    d = r.json()
    assert d["barras"] == 3
    assert d["por_origem"] == {"ao_vivo": 3}
    assert "atraso NAO e lacuna" in d["nota"]
    assert "atraso_barras" in d and "lacunas" not in d


def test_o_backfill_e_marcado_como_tal(cliente):
    enviar(cliente, corpo(2))
    enviar(cliente, corpo(2, de=2, origem="backfill"))
    d = cliente.get("/api/aovivo/estado", headers=CABECALHO).json()
    assert d["por_origem"] == {"ao_vivo": 2, "backfill": 2}


# ================================================================ segredo

def test_o_segredo_do_hmac_nunca_aparece(cliente):
    """Regra 15, e ele é o quarto segredo do projeto."""
    from app.settings import SECRET_FIELDS

    assert "rele_hmac_secret" in SECRET_FIELDS

    for rota in ("/api/substrato/health", "/api/aovivo/estado",
                 "/api/relatorio/exportar"):
        r = cliente.get(rota, headers=CABECALHO)
        assert SEGREDO not in r.text, rota


def test_sem_segredo_configurado_a_rota_falha_FECHADO(ambiente, monkeypatch):
    """Como `exigir_token_de_servico` já faz: ausência não abre a porta."""
    from app.main import criar_app
    from app.settings import get_settings

    monkeypatch.delenv("RELE_HMAC_SECRET", raising=False)
    get_settings.cache_clear()
    with TestClient(criar_app()) as c:
        r = enviar(c, corpo(), segredo="qualquer")
        assert r.status_code == 401
        assert "nao configurado" in r.json()["detail"]
    get_settings.cache_clear()
