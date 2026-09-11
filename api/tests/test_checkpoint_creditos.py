"""Os tres contadores de credito no checkpoint, cada um no seu campo.

> "Nao deixe 'lancamentos' substituir silenciosamente o antigo contador de
> creditos." - o usuario, 2026-09-10
"""

from __future__ import annotations

from tests.test_portao_a import cenario_testavel  # noqa: F401


def _rodar_b4(conn, cenario):
    """O B4 numa janela TESTAVEL: ele cobra credito de verdade.

    O cenario de 3.000 barras deixa toda hipotese arquivada como nao testavel,
    e ai nada e cobrado - o teste passaria comparando zero com zero.
    """
    from app.b4 import braco as b4_braco

    dataset_id, cfg = cenario
    b4_braco.rodar(conn, dataset_id=dataset_id, config=cfg, config_version_id=1)


def _creditos(conn):
    from app.hipotese import dimensionamento as d
    from app.relatorio import checkpoint

    return checkpoint.montar(conn, potencia_ppm=d.POTENCIA_ALVO_PPM)["contadores"][
        "creditos"
    ]


def test_as_TRES_grandezas_batem_com_o_banco(conn, cenario_testavel):
    _rodar_b4(conn, cenario_testavel)
    c = _creditos(conn)
    linhas = conn.execute("SELECT COUNT(*) FROM test_credit_entry").fetchone()[0]
    soma = conn.execute(
        "SELECT COALESCE(SUM(creditos), 0) FROM test_credit_entry"
    ).fetchone()[0]
    orcamentos = conn.execute("SELECT COUNT(*) FROM test_credit_budget").fetchone()[0]
    assert linhas > 0 and orcamentos > 0, "o cenario precisa ter cobrado teste"
    assert c["credit_entry_rows"] == linhas
    assert c["creditos_consumidos_total"] == soma
    assert c["orcamentos_de_credito"] == orcamentos
    assert c["consumo_confere_com_os_orcamentos"] is True
    assert set(c["o_que_cada_um_e"]) == {
        "credit_entry_rows", "creditos_consumidos_total", "orcamentos_de_credito",
    }


def test_LANCAMENTO_nao_e_CREDITO_quando_ha_reteste(conn, cenario_testavel):
    """A diferenca que o rotulo apagava: um reteste e UMA linha e TRES creditos."""
    _rodar_b4(conn, cenario_testavel)
    primeiro = _creditos(conn)
    # O B4 de novo, na mesma config: as mesmas hipoteses, reconhecidas como
    # reteste pelo content_hash - cada uma cobra 3.
    _rodar_b4(conn, cenario_testavel)
    c = _creditos(conn)
    assert c["credit_entry_rows"] > primeiro["credit_entry_rows"], (primeiro, c)
    assert c["creditos_consumidos_total"] > c["credit_entry_rows"], c
