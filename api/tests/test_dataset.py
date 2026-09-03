"""Testes do incremento 1: dataset imutavel, fixado e com reserva carvada.

Nenhum teste toca a rede. O downloader e injetado.

O peso esta nos criterios 4 e 5 - o loader nao pode devolver barra reservada
nem barra posterior a decisao. Sao testes de ESTRUTURA: nao verificam que o
loader lembrou de filtrar, verificam que nao existe chamada capaz de furar o
filtro.
"""

from __future__ import annotations

import calendar
import hashlib
import io
import sqlite3
import zipfile
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config.schema import ExperimentConfig
from app.dataset import binance, loader
from app.dataset.binance import (
    Barra,
    ArquivoBaixado,
    ChecksumDivergente,
    DadosInconsistentes,
    intervalo_ms,
    meses_da_janela,
    normalizar_timestamp,
)
from app.dataset.ingest import (
    DivergenciaNaReingestao,
    LacunasNaoAceitas,
    analisar,
    hash_dos_dados,
    ingerir,
)

INTERVALO = 900_000  # 15m
ESCALA = 10**8


def ms(texto: str) -> int:
    return int(datetime.fromisoformat(texto).replace(tzinfo=timezone.utc).timestamp() * 1000)


def barra(open_time_ms: int, *, preco: int = 50_000, volume: int = 100) -> Barra:
    p = preco * ESCALA
    return Barra(
        open_time_ms=open_time_ms,
        open=p,
        high=p + ESCALA,
        low=p - ESCALA,
        close=p,
        volume=volume * ESCALA,
        quote_volume=volume * preco * ESCALA,
        trades=volume,
    )


def barras_do_mes(mes: str, *, pular: set[int] | None = None) -> list[Barra]:
    """Serie completa de um mes, na grade de 15 minutos."""
    ano, m = (int(x) for x in mes.split("-"))
    dias = calendar.monthrange(ano, m)[1]
    inicio = ms(f"{ano:04d}-{m:02d}-01T00:00:00")
    pular = pular or set()
    return [
        barra(inicio + i * INTERVALO)
        for i in range(dias * 96)
        if i not in pular
    ]


def baixador_falso(por_mes: dict[str, list[Barra]]):
    """Substitui o download. Assinatura identica a de `baixar_mes`."""

    def baixar(symbol, timeframe, mes, *, conferir_checksum=True):
        if mes not in por_mes:
            raise binance.ErroDeFonte(f"mes {mes} nao disponivel no teste")
        barras = por_mes[mes]
        return barras, ArquivoBaixado(
            mes=mes,
            url=f"falso://{mes}",
            bytes_baixados=len(barras) * 100,
            sha256="0" * 64,
            sha256_publicado="0" * 64 if conferir_checksum else None,
            barras=len(barras),
        )

    return baixar


def config_curta(**extra) -> ExperimentConfig:
    """Janela de dois meses, para o teste ser rapido sem deixar de ser real."""
    base = {"data_start": "2024-09-01", "data_end": "2024-11-01"}
    base.update(extra)
    return ExperimentConfig(**base)


@pytest.fixture
def dois_meses() -> dict[str, list[Barra]]:
    return {"2024-09": barras_do_mes("2024-09"), "2024-10": barras_do_mes("2024-10")}


# ------------------------------------------------------------- primitivas


@pytest.mark.parametrize(
    "tf, esperado", [("15m", 900_000), ("1m", 60_000), ("4h", 14_400_000), ("1d", 86_400_000)]
)
def test_intervalo_ms(tf: str, esperado: int) -> None:
    assert intervalo_ms(tf) == esperado


@pytest.mark.parametrize("tf", ["15", "m15", "0m", "-5m", "", "15x"])
def test_intervalo_ms_recusa_lixo(tf: str) -> None:
    with pytest.raises(ValueError):
        intervalo_ms(tf)


def test_meses_da_janela_trata_o_fim_como_exclusivo() -> None:
    """2026-09-01 significa "ate o fim de agosto", nao "inclua setembro"."""
    meses = meses_da_janela(date(2024, 9, 1), date(2026, 9, 1))
    assert meses[0] == "2024-09"
    assert meses[-1] == "2026-08"
    assert len(meses) == 24


def test_meses_da_janela_inclui_o_mes_de_um_fim_no_meio() -> None:
    assert meses_da_janela(date(2024, 9, 1), date(2024, 10, 15))[-1] == "2024-10"


# ---------------------------------------------- a armadilha do timestamp


def test_normaliza_milissegundos_sem_mexer() -> None:
    assert normalizar_timestamp(1_725_148_800_000) == 1_725_148_800_000


def test_normaliza_microssegundos_para_milissegundos() -> None:
    """Dumps a partir de 2025-01 vem em microssegundos."""
    assert normalizar_timestamp(1_735_689_600_000_000) == 1_735_689_600_000


def test_mesma_data_nas_duas_unidades_da_o_mesmo_resultado() -> None:
    """E o ponto: a serie tem de ser continua atravessando 2025-01."""
    assert normalizar_timestamp(1_735_689_600_000) == normalizar_timestamp(
        1_735_689_600_000_000
    )


@pytest.mark.parametrize("bruto", [1, 1_000, 999_999_999, 10**18])
def test_timestamp_fora_da_faixa_plausivel_falha_alto(bruto: int) -> None:
    with pytest.raises(DadosInconsistentes, match="fora da faixa plausivel"):
        normalizar_timestamp(bruto)


# ------------------------------------------------------------- precisao


def test_preco_vira_inteiro_exato_sem_float() -> None:
    linha = "1725148800000,93576.00000000,93702.15000000,93489.03000000,93656.18000000,175.85673000,1725149699999,16461794.00035600,19788,0,0,0"
    b = list(binance.ler_csv(linha, origem="t"))[0]
    assert b.open == 9_357_600_000_000
    assert b.high == 9_370_215_000_000
    assert b.quote_volume == 1_646_179_400_035_600
    assert all(isinstance(v, int) for v in b)


def test_precisao_excessiva_e_recusada_em_vez_de_arredondada() -> None:
    """Perder centavo em silencio e pior que falhar."""
    linha = "1725148800000,1.000000001,2,0.5,1,1,0,1,1"
    with pytest.raises(DadosInconsistentes, match="mais precisao"):
        list(binance.ler_csv(linha, origem="t"))


def test_cabecalho_e_ignorado_mas_lixo_no_meio_nao() -> None:
    bom = "open_time,open,high,low,close,volume,close_time,quote,trades\n1725148800000,1,2,0.5,1,1,0,1,1"
    assert len(list(binance.ler_csv(bom, origem="t"))) == 1

    ruim = "1725148800000,1,2,0.5,1,1,0,1,1\nlixo,1,2,0.5,1,1,0,1,1"
    with pytest.raises(DadosInconsistentes, match="nao e inteiro"):
        list(binance.ler_csv(ruim, origem="t"))


# ------------------------------------------------- relatorio de integridade


def test_serie_perfeita_e_completa() -> None:
    r = analisar(barras_do_mes("2024-09"), INTERVALO)
    assert r.completo
    assert r.barras_obtidas == r.barras_esperadas == 2880
    assert r.lacunas == []


def test_lacuna_e_detectada_com_duracao_e_contagem() -> None:
    r = analisar(barras_do_mes("2024-09", pular={10, 11, 12}), INTERVALO)
    assert not r.completo
    assert len(r.lacunas) == 1
    assert r.lacunas[0].barras_faltando == 3
    assert r.lacunas[0].duracao_ms == 4 * INTERVALO
    assert r.barras_esperadas - r.barras_obtidas == 3


def test_desalinhamento_e_sempre_fatal() -> None:
    """Grade errada nao e lacuna: e dataset que nao e o que diz ser."""
    bs = barras_do_mes("2024-09")[:5]
    bs.append(barra(bs[-1].open_time_ms + 137_000))
    with pytest.raises(DadosInconsistentes, match="desalinhada"):
        analisar(bs, INTERVALO)


def test_duplicata_e_fatal() -> None:
    bs = barras_do_mes("2024-09")[:5]
    bs.append(bs[-1])
    with pytest.raises(DadosInconsistentes, match="fora de ordem ou duplicadas"):
        analisar(bs, INTERVALO)


def test_volume_zero_e_contado_e_nao_e_erro() -> None:
    bs = barras_do_mes("2024-09")[:10]
    bs[3] = barra(bs[3].open_time_ms, volume=0)
    r = analisar(bs, INTERVALO)
    assert r.completo
    assert r.barras_volume_zero == [bs[3].open_time_ms]


def test_hash_depende_dos_dados_e_nao_da_ordem_de_chegada() -> None:
    bs = barras_do_mes("2024-09")[:100]
    assert hash_dos_dados(bs) == hash_dos_dados(list(bs))
    outras = bs[:-1] + [barra(bs[-1].open_time_ms, preco=51_000)]
    assert hash_dos_dados(bs) != hash_dos_dados(outras)


# ------------------------------------------------------------- ingestao


def test_ingestao_grava_metadados_do_criterio_1(
    conn: sqlite3.Connection, dois_meses
) -> None:
    r = ingerir(conn, config_curta(), baixador=baixador_falso(dois_meses))
    linha = conn.execute("SELECT * FROM dataset WHERE id = ?", (r.dataset_id,)).fetchone()

    assert linha["venue"] == "binance"
    assert linha["symbol"] == "BTCUSDT"
    assert linha["timeframe"] == "15m"
    assert linha["bars"] == 2880 + 2976
    assert len(linha["sha256"]) == 64
    assert linha["source"].startswith("https://data.binance.vision")
    assert linha["fetched_at"]
    assert linha["reserved_from_ms"] > linha["start_ms"]
    assert linha["fidelity_level"] == 1  # criterio 6


def test_reingestao_e_idempotente(conn: sqlite3.Connection, dois_meses) -> None:
    """Criterio 2: mesmo hash, nenhuma linha duplicada, nenhum metadado mexido."""
    cfg = config_curta()
    primeira = ingerir(conn, cfg, baixador=baixador_falso(dois_meses))
    antes = conn.execute("SELECT COUNT(*) AS n FROM bar").fetchone()["n"]

    segunda = ingerir(conn, cfg, baixador=baixador_falso(dois_meses))

    assert segunda.ja_existia is True
    assert segunda.dataset_id == primeira.dataset_id
    assert segunda.sha256 == primeira.sha256
    assert conn.execute("SELECT COUNT(*) AS n FROM bar").fetchone()["n"] == antes
    assert conn.execute("SELECT COUNT(*) AS n FROM dataset").fetchone()["n"] == 1


def test_reingestao_divergente_nao_sobrescreve(
    conn: sqlite3.Connection, dois_meses
) -> None:
    cfg = config_curta()
    ingerir(conn, cfg, baixador=baixador_falso(dois_meses))

    adulterado = dict(dois_meses)
    modificado = list(adulterado["2024-10"])
    modificado[5] = barra(modificado[5].open_time_ms, preco=99_999)
    adulterado["2024-10"] = modificado

    with pytest.raises(DivergenciaNaReingestao, match="mudaram"):
        ingerir(conn, cfg, baixador=baixador_falso(adulterado))

    assert conn.execute("SELECT COUNT(*) AS n FROM dataset").fetchone()["n"] == 1


def test_lacuna_aborta_a_ingestao_por_padrao(conn: sqlite3.Connection) -> None:
    """Criterio 3: nao se ignora lacuna."""
    com_buraco = {
        "2024-09": barras_do_mes("2024-09", pular={100, 101}),
        "2024-10": barras_do_mes("2024-10"),
    }
    with pytest.raises(LacunasNaoAceitas) as exc:
        ingerir(conn, config_curta(), baixador=baixador_falso(com_buraco))

    assert exc.value.relatorio.lacunas[0].barras_faltando == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM dataset").fetchone()["n"] == 0


def test_lacuna_aceita_explicitamente_prossegue(conn: sqlite3.Connection) -> None:
    com_buraco = {
        "2024-09": barras_do_mes("2024-09", pular={100, 101}),
        "2024-10": barras_do_mes("2024-10"),
    }
    r = ingerir(
        conn,
        config_curta(),
        aceitar_lacunas=True,
        baixador=baixador_falso(com_buraco),
    )
    assert r.relatorio.completo is False
    assert r.relatorio.lacunas[0].barras_faltando == 2


def test_janela_recorta_barras_fora_dela(conn: sqlite3.Connection) -> None:
    """O mes de borda traz barras que a janela decidida nao pediu."""
    cfg = ExperimentConfig(data_start="2024-09-10", data_end="2024-10-05")
    r = ingerir(
        conn,
        cfg,
        baixador=baixador_falso(
            {"2024-09": barras_do_mes("2024-09"), "2024-10": barras_do_mes("2024-10")}
        ),
    )
    assert r.start_ms == ms("2024-09-10T00:00:00")
    assert r.end_ms == ms("2024-10-04T23:45:00")


def test_falha_no_meio_nao_deixa_dataset_pela_metade(
    conn: sqlite3.Connection, dois_meses
) -> None:
    """Barra com low > high viola CHECK: a transacao inteira volta atras."""
    quebrado = dict(dois_meses)
    ruim = list(quebrado["2024-10"])
    p = 50_000 * ESCALA
    ruim[7] = Barra(ruim[7].open_time_ms, p, p - ESCALA, p + ESCALA, p, 1, 1, 1)
    quebrado["2024-10"] = ruim

    with pytest.raises(sqlite3.IntegrityError):
        ingerir(conn, config_curta(), baixador=baixador_falso(quebrado))

    assert conn.execute("SELECT COUNT(*) AS n FROM dataset").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM bar").fetchone()["n"] == 0


# --------------------------------------------------------- imutabilidade


def test_dataset_e_bar_sao_imutaveis(conn: sqlite3.Connection, dois_meses) -> None:
    r = ingerir(conn, config_curta(), baixador=baixador_falso(dois_meses))

    with pytest.raises(sqlite3.IntegrityError, match="imutavel"):
        conn.execute("UPDATE dataset SET bars = 1 WHERE id = ?", (r.dataset_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM dataset WHERE id = ?", (r.dataset_id,))
    with pytest.raises(sqlite3.IntegrityError, match="imutavel"):
        conn.execute("UPDATE bar SET close = 1 WHERE dataset_id = ?", (r.dataset_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM bar WHERE dataset_id = ?", (r.dataset_id,))


# ============================================================================
# CRITERIO 4 - a reserva nao sai do SQL
# ============================================================================


def test_view_nao_devolve_barra_reservada(conn: sqlite3.Connection, dois_meses) -> None:
    """A garantia esta na definicao da view, nao no chamador."""
    r = ingerir(conn, config_curta(), baixador=baixador_falso(dois_meses))
    vazou = conn.execute(
        "SELECT COUNT(*) AS n FROM bar_experimento b"
        " JOIN dataset d ON d.id = b.dataset_id"
        " WHERE b.open_time_ms >= d.reserved_from_ms"
    ).fetchone()["n"]
    assert vazou == 0

    # E as barras reservadas EXISTEM - a view esconde, nao apagamos o dado.
    total = conn.execute("SELECT COUNT(*) AS n FROM bar").fetchone()["n"]
    visiveis = conn.execute("SELECT COUNT(*) AS n FROM bar_experimento").fetchone()["n"]
    assert total == r.barras
    assert visiveis < total


def test_reserva_e_de_aproximadamente_20_por_cento(
    conn: sqlite3.Connection, dois_meses
) -> None:
    r = ingerir(conn, config_curta(), baixador=baixador_falso(dois_meses))
    resumo = loader.resumo(conn, r.dataset_id)
    proporcao = resumo["barras_reservadas"] / resumo["barras_total"]
    assert 0.19 < proporcao < 0.21


def test_nenhuma_chamada_do_loader_devolve_barra_reservada(
    conn: sqlite3.Connection, dois_meses
) -> None:
    """Criterio 4, na forma que importa: nao ha COMO furar.

    Tenta com decision_ts no infinito, com e sem recorte. Se alguma combinacao
    devolvesse barra reservada, a reserva seria convencao e nao estrutura.
    """
    r = ingerir(conn, config_curta(), baixador=baixador_falso(dois_meses))
    reserved = r.reserved_from_ms

    for ultimas in (None, 1, 10, 10**9):
        barras = loader.carregar(
            conn, r.dataset_id, decision_ts_ms=10**15, ultimas=ultimas,
            finalidade="in_sample",
        )
        assert barras, "deveria devolver alguma coisa"
        assert max(b.open_time_ms for b in barras) < reserved


# ============================================================================
# CRITERIO 5 - nada posterior a decisao
# ============================================================================


def test_loader_nao_devolve_barra_posterior_a_decisao(
    conn: sqlite3.Connection, dois_meses
) -> None:
    r = ingerir(conn, config_curta(), baixador=baixador_falso(dois_meses))
    corte = ms("2024-09-15T12:00:00")
    barras = loader.carregar(
        conn, r.dataset_id, decision_ts_ms=corte, finalidade="in_sample"
    )

    assert barras
    assert max(b.open_time_ms for b in barras) < corte
    assert max(b.close_time_ms for b in barras) <= corte


def test_loader_exclui_a_barra_ainda_em_formacao(
    conn: sqlite3.Connection, dois_meses
) -> None:
    """Mais restritivo que o criterio literal, e de proposito.

    Uma barra que abriu antes da decisao mas ainda nao fechou tem maxima,
    minima e fechamento desconhecidos naquele instante. Devolve-la seria
    entregar dado futuro no formato mais dificil de perceber.
    """
    r = ingerir(conn, config_curta(), baixador=baixador_falso(dois_meses))
    abertura = ms("2024-09-15T12:00:00")

    # Um milissegundo antes do fechamento: a barra ainda esta se formando.
    quase = loader.carregar(
        conn, r.dataset_id, decision_ts_ms=abertura + INTERVALO - 1,
        finalidade="in_sample",
    )
    assert abertura not in {b.open_time_ms for b in quase}

    # No instante exato do fechamento, ela ja e conhecida.
    fechada = loader.carregar(
        conn, r.dataset_id, decision_ts_ms=abertura + INTERVALO,
        finalidade="in_sample",
    )
    assert abertura in {b.open_time_ms for b in fechada}


def test_decision_ts_e_obrigatorio(conn: sqlite3.Connection, dois_meses) -> None:
    """Sem default: default e a forma mais comum de esquecer."""
    r = ingerir(conn, config_curta(), baixador=baixador_falso(dois_meses))
    with pytest.raises(TypeError):
        loader.carregar(conn, r.dataset_id)  # type: ignore[call-arg]


def test_ultimas_devolve_o_fim_da_serie_visivel(
    conn: sqlite3.Connection, dois_meses
) -> None:
    r = ingerir(conn, config_curta(), baixador=baixador_falso(dois_meses))
    corte = ms("2024-09-15T12:00:00")
    todas = loader.carregar(
        conn, r.dataset_id, decision_ts_ms=corte, finalidade="in_sample"
    )
    ultimas = loader.carregar(
        conn, r.dataset_id, decision_ts_ms=corte, ultimas=5,
        finalidade="in_sample",
    )
    assert ultimas == todas[-5:]


# ---------------------------------------------- checksum contra a origem


def test_checksum_divergente_recusa_o_arquivo(monkeypatch: pytest.MonkeyPatch) -> None:
    conteudo = io.BytesIO()
    with zipfile.ZipFile(conteudo, "w") as z:
        z.writestr("BTCUSDT-15m-2024-09.csv", "1725148800000,1,2,0.5,1,1,0,1,1")
    zip_bytes = conteudo.getvalue()

    def buscar(url, timeout=None):
        if url.endswith(".CHECKSUM"):
            return b"f" * 64 + b"  BTCUSDT-15m-2024-09.zip"
        return zip_bytes

    monkeypatch.setattr(binance, "_buscar", buscar)
    with pytest.raises(ChecksumDivergente):
        binance.baixar_mes("BTCUSDT", "15m", "2024-09")


def test_checksum_correto_aceita_o_arquivo(monkeypatch: pytest.MonkeyPatch) -> None:
    conteudo = io.BytesIO()
    with zipfile.ZipFile(conteudo, "w") as z:
        z.writestr("BTCUSDT-15m-2024-09.csv", "1725148800000,1,2,0.5,1,1,0,1,1")
    zip_bytes = conteudo.getvalue()
    sha = hashlib.sha256(zip_bytes).hexdigest()

    def buscar(url, timeout=None):
        if url.endswith(".CHECKSUM"):
            return f"{sha}  BTCUSDT-15m-2024-09.zip".encode()
        return zip_bytes

    monkeypatch.setattr(binance, "_buscar", buscar)
    barras, info = binance.baixar_mes("BTCUSDT", "15m", "2024-09")
    assert len(barras) == 1
    assert info.sha256 == sha == info.sha256_publicado


# ------------------------------------------------------------------ rotas


def test_rota_dataset_antes_da_ingestao(client: TestClient) -> None:
    corpo = client.get("/api/dataset").json()
    assert corpo["existe"] is False


def test_rota_dataset_depois_da_ingestao(
    client: TestClient, conn: sqlite3.Connection, dois_meses
) -> None:
    r = ingerir(conn, config_curta(), baixador=baixador_falso(dois_meses))
    corpo = client.get("/api/dataset").json()
    assert corpo["existe"] is True
    assert corpo["dataset_id"] == r.dataset_id
    assert corpo["barras_reservadas"] > 0
    assert corpo["barras_disponiveis"] + corpo["barras_reservadas"] == corpo["barras_total"]


def test_rota_de_ingestao_exige_token(client: TestClient) -> None:
    resposta = client.post(
        "/api/dataset/ingest",
        json={"author": "teste"},
        headers={"Authorization": ""},
    )
    assert resposta.status_code == 401


def _instalar_baixador(monkeypatch: pytest.MonkeyPatch, por_mes) -> None:
    monkeypatch.setattr(
        "app.dataset.ingest.baixar_mes", baixador_falso(por_mes)
    )


def meses_da_config() -> list[str]:
    """Meses da janela que o bootstrap realmente usa.

    Derivado da config, nunca repetido a mao: teste que duplica a data quebra
    sozinho no dia em que a janela muda - e foi o que aconteceu.
    """
    cfg = ExperimentConfig()
    return meses_da_janela(
        date.fromisoformat(cfg.data_start), date.fromisoformat(cfg.data_end)
    )


def test_rota_de_ingestao_devolve_relatorio(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Caminho feliz de ponta a ponta, pela rota HTTP."""
    _instalar_baixador(monkeypatch, {m: barras_do_mes(m) for m in meses_da_config()})

    resposta = client.post("/api/dataset/ingest", json={"author": "teste"})
    assert resposta.status_code == 201
    corpo = resposta.json()
    assert corpo["ja_existia"] is False
    assert corpo["relatorio_integridade"]["completo"] is True
    assert len(corpo["sha256"]) == 64

    # Idempotente tambem pela rota.
    segunda = client.post("/api/dataset/ingest", json={"author": "teste"})
    assert segunda.status_code == 201
    assert segunda.json()["ja_existia"] is True


def test_rota_responde_409_com_o_relatorio_quando_ha_lacuna(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O 409 tem de trazer o relatorio: e com ele que se decide aceitar."""
    meses = meses_da_config()
    todos = {mes: barras_do_mes(mes) for mes in meses}
    todos[meses[3]] = barras_do_mes(meses[3], pular={50, 51, 52, 53})
    _instalar_baixador(monkeypatch, todos)

    resposta = client.post("/api/dataset/ingest", json={"author": "teste"})
    assert resposta.status_code == 409
    detalhe = resposta.json()["detail"]
    assert detalhe["erro"] == "lacunas_nao_aceitas"
    assert detalhe["relatorio_integridade"]["lacunas"]["barras_faltando"] == 4
    assert "aceitar_lacunas=true" in detalhe["como_prosseguir"]

    aceita = client.post(
        "/api/dataset/ingest", json={"author": "teste", "aceitar_lacunas": True}
    )
    assert aceita.status_code == 201


def test_rota_traduz_bloqueio_por_jurisdicao(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """451 nao pode virar erro generico: e o unico sinal de que o ADR 0012 caiu."""

    def bloqueado(*a, **kw):
        raise binance.BloqueioPorJurisdicao("HTTP 451 em ...")

    monkeypatch.setattr("app.dataset.ingest.baixar_mes", bloqueado)
    resposta = client.post("/api/dataset/ingest", json={"author": "teste"})
    assert resposta.status_code == 502
    assert resposta.json()["detail"]["erro"] == "bloqueio_por_jurisdicao"
    assert resposta.json()["detail"]["referencia"] == "ADR 0012"


def test_rota_separa_dado_inconsistente_de_falha_de_rede(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DadosInconsistentes herda de ErroDeFonte: a ordem dos except importa."""

    def incoerente(*a, **kw):
        raise DadosInconsistentes("grade desalinhada")

    monkeypatch.setattr("app.dataset.ingest.baixar_mes", incoerente)
    assert client.post("/api/dataset/ingest", json={"author": "t"}).status_code == 422

    def rede(*a, **kw):
        raise binance.ErroDeFonte("timeout")

    monkeypatch.setattr("app.dataset.ingest.baixar_mes", rede)
    assert client.post("/api/dataset/ingest", json={"author": "t"}).status_code == 502
