"""OP-3 — o A2 parcelado, e as oito garantias que o usuário exigiu.

Medido em produção na recertificação de 2026-09-10: o a2 roda num pedido
**único de 88,7 s**, acima dos 60 s do proxy do painel. É a mesma aposta no
timeout que a OP-1 tirou do a1a, num escopo que a OP-1 não cobria.

| # | garantia | onde |
|---|---|---|
| 1 | equivalência entre o A2 antigo e o parcelado | `test_EQUIVALENCIA_*` |
| 2 | mesmos insumos congelados e mesmo resultado final | `test_EQUIVALENCIA_*`, `test_os_INSUMOS_*` |
| 3 | retomada idempotente depois de interrupção | `test_RETOMADA_*` |
| 4 | nenhuma redução de B1, giros ou controles | `test_NADA_e_reduzido` |
| 5 | uma única escritora | `test_a_API_e_a_UNICA_escritora` |
| 6 | nada move hipótese, crédito, holdout ou contador do DSR | `test_NAO_move_*` |
| 7 | execução e alvo esperados conferidos em cada etapa | `test_cada_etapa_confere_*` |
| 8 | manifesto só depois de todas as etapas | `test_o_MANIFESTO_so_depois_*` |
"""

from __future__ import annotations

import inspect
import json
import sqlite3

import pytest

from app.certificacao import escopos as esc
from tests.test_portao_a import cenario  # noqa: F401

CABECALHO = {"Authorization": "Bearer token-de-teste-0a"}


def _rodar_tudo(client, *, escopo: str) -> list[str]:
    """Todas as etapas, uma por pedido. Devolve a ordem em que rodaram."""
    feitas = []
    for _ in esc.PLANOS[escopo]:
        r = client.post(f"/api/certificacao/{escopo}", headers=CABECALHO,
                        json={"author": "teste"})
        assert r.status_code == 200, r.text
        feitas.append(r.json()["etapa"])
    return feitas


def _manifesto(conn, execucao_id: int) -> dict | None:
    linha = conn.execute(
        "SELECT manifesto_json FROM certificacao_manifesto"
        " WHERE execucao_id = ?", (execucao_id,),
    ).fetchone()
    return None if linha is None else json.loads(linha["manifesto_json"])


# ------------------------------------------------- 4. nada é reduzido


def test_NADA_e_reduzido():
    """Garantia 4: o plano é a lista das unidades INDIVISÍVEIS do caminho de
    uma vez, na mesma ordem.

    Se alguma sumisse, a soma das etapas mediria menos que o original — e um
    certificado assim valeria sobre um teste menor. **O segundo giro está
    nela**, e é ele que dá ao a2 o segundo ponto: com um giro só a inclinação
    sai `None`, e `None` sobe como `None`.
    """
    assert esc.PLANO_A2 == (
        "preparo.baselines", "preparo.b4", "a2.segundo_giro", "a2.avaliacao",
    )
    assert esc.BLOCOS[esc.A2] == len(esc.PLANO_A2)
    assert esc.CASOS_ESPERADOS[esc.A2] == 2


def test_os_DOIS_caminhos_montam_os_casos_pela_MESMA_funcao():
    """Garantias 1 e 2, por estrutura: duas montagens dos mesmos dois casos
    divergiriam no primeiro ajuste — e a equivalência é o que a OP-3 prova."""
    from app.certificacao import parcelado, por_escopo

    fonte_parcelado = inspect.getsource(parcelado._executar)
    fonte_uma_vez = inspect.getsource(por_escopo.rodar_a2)
    assert "_casos_do_a2" in fonte_parcelado
    assert "_casos_do_a2" in fonte_uma_vez
    # e o corpo dos casos mora SÓ na função compartilhada
    assert "b1_proporcional_ao_giro" not in fonte_uma_vez
    assert "b1_proporcional_ao_giro" not in fonte_parcelado

    # o mesmo para o preparo e para o segundo giro
    fonte = inspect.getsource(parcelado._executar)
    assert "_preparar_baselines" in fonte
    assert "_preparar_b4" in fonte
    assert "_segundo_giro_de_b1" in fonte


# ------------------------------------- 1 e 2. equivalência, medida


def test_EQUIVALENCIA_o_parcelado_produz_os_MESMOS_casos_do_de_uma_vez(
    conn, client, cenario
):
    """Garantia 1, e ela é a que exige rodar os dois.

    O de uma vez e o parcelado rodam sobre o **mesmo banco oficial**, cada um
    na sua cópia descartável. As chaves, as famílias e os vereditos têm de
    coincidir — se não coincidissem, o parcelamento teria mudado o teste.
    """
    from app.certificacao import por_escopo
    from app.config import service as config_service
    from app.dataset import loader as dataset_loader

    atual = config_service.versao_atual(conn)
    meta = dataset_loader.dataset_vigente(conn)

    de_uma_vez = por_escopo.rodar_a2(
        conn, dataset_id=meta.id, config=atual.config,
        config_version_id=atual.id,
    )

    _rodar_tudo(client, escopo="a2")
    selo = client.post("/api/certificacao/a2", headers=CABECALHO,
                       json={"author": "teste", "selar": True})
    assert selo.status_code == 200, selo.text
    do_parcelado = _manifesto(conn, selo.json()["execucao_id"])["casos"]

    def resumo(casos):
        return sorted(
            (c["chave"], c.get("familia_de_defeito"), c.get("contido"),
             c.get("promovido"), c.get("barrado"))
            for c in casos
        )

    assert resumo(do_parcelado) == resumo(de_uma_vez.casos), (
        "o parcelamento mudou o resultado do a2"
    )


def test_os_INSUMOS_sao_CONGELADOS_antes_da_primeira_etapa(
    conn, client, cenario
):
    """Garantia 2: alvo e plano fixados antes de a primeira etapa rodar.

    Decidir o corte depois de ver uma etapa seria escolher olhando o resultado,
    e um alvo que mudasse no meio produziria um certificado de um laboratório
    que nunca existiu.
    """
    r = client.post("/api/certificacao/a2", headers=CABECALHO,
                    json={"author": "teste"})
    assert r.status_code == 200, r.text
    execucao_id = r.json()["execucao_id"]

    linha = conn.execute(
        "SELECT congelado_json, alvo_hash, blocos_esperados, escopo"
        "  FROM certificacao_execucao WHERE id = ?", (execucao_id,),
    ).fetchone()
    congelado = json.loads(linha["congelado_json"])
    assert congelado["plano"] == list(esc.PLANO_A2)
    assert "congelado_em" in congelado
    assert "dataset_id" in congelado and "config_version_id" in congelado
    assert linha["blocos_esperados"] == len(esc.PLANO_A2)
    assert linha["escopo"] == "a2"
    assert linha["alvo_hash"]


# --------------------------------------------- 3. retomada idempotente


def test_RETOMADA_depois_de_interrupcao_nao_roda_a_etapa_DUAS_vezes(
    conn, client, cenario
):
    """Garantia 3. O cliente perde a resposta e repete o pedido — o retry
    segue para a PRÓXIMA, e nunca refaz a anterior."""
    primeira = client.post("/api/certificacao/a2", headers=CABECALHO,
                           json={"author": "teste"})
    assert primeira.status_code == 200
    execucao_id = primeira.json()["execucao_id"]
    etapa_1 = primeira.json()["etapa"]

    segunda = client.post("/api/certificacao/a2", headers=CABECALHO,
                          json={"author": "teste"})
    assert segunda.status_code == 200
    assert segunda.json()["execucao_id"] == execucao_id
    assert segunda.json()["etapa"] != etapa_1

    # UMA tentativa por etapa concluída.
    por_etapa = conn.execute(
        "SELECT indice_bloco, COUNT(*) AS n FROM certificacao_bloco"
        " WHERE execucao_id = ? GROUP BY indice_bloco", (execucao_id,),
    ).fetchall()
    assert por_etapa and all(r["n"] == 1 for r in por_etapa), (
        [dict(r) for r in por_etapa]
    )


def test_RETOMADA_o_estado_diz_qual_e_a_PROXIMA(conn, client, cenario):
    """Sem isso, retomar seria adivinhar — e o painel adivinhando é como a
    corrida da OP-6 nasceu."""
    antes = client.get("/api/certificacao/a2", headers=CABECALHO).json()
    assert antes["iniciada"] is False
    assert antes["plano"] == list(esc.PLANO_A2)
    assert antes["escopo"] == "a2"

    client.post("/api/certificacao/a2", headers=CABECALHO,
                json={"author": "teste"})
    depois = client.get("/api/certificacao/a2", headers=CABECALHO).json()
    assert depois["iniciada"] is True
    assert depois["escopo"] == "a2"
    # `proxima` e o INDICE da proxima etapa, e nao o nome dela.
    assert depois["proxima"] == 1
    assert depois["plano"][depois["proxima"]] == esc.PLANO_A2[1]


# ------------------------------------------- 5. uma única escritora


def test_a_API_e_a_UNICA_escritora():
    """Garantia 5: não há worker, fila nem segundo processo.

    A regra 1 proíbe na Fase 0, e a OP-3 não podia pagar a dívida criando
    exatamente o que `.docs/18` deixa para a Fase 1.
    """
    from app.certificacao import parcelado

    fonte = inspect.getsource(parcelado)
    for proibido in ("subprocess", "multiprocessing", "asyncio.create_task",
                     "concurrent.futures"):
        assert proibido not in fonte, f"{proibido} apareceu no parcelado"
    # `threading` aparece, e é só a TRAVA por execução — não um worker.
    assert "threading.Lock" in fonte
    assert "threading.Thread" not in fonte


# --------------------- 6. nada move hipótese, crédito, holdout, DSR


def test_NAO_move_hipotese_credito_holdout_nem_o_contador_do_DSR(
    conn, client, cenario
):
    """Garantia 6. Certificar não anda o experimento — e o contador do DSR é
    `COUNT(*) FROM hypothesis`, então basta ele não se mover."""
    def foto():
        um = lambda sql: conn.execute(sql).fetchone()[0]   # noqa: E731
        return {
            "hypothesis": um("SELECT COUNT(*) FROM hypothesis"),
            "hypothesis_state": um("SELECT COUNT(*) FROM hypothesis_state"),
            "credit_entry": um("SELECT COUNT(*) FROM test_credit_entry"),
            "credit_budget": um("SELECT COUNT(*) FROM test_credit_budget"),
            "holdout": um("SELECT COUNT(*) FROM holdout_access"),
            "config_version": um("SELECT COUNT(*) FROM config_version"),
        }

    antes = foto()
    _rodar_tudo(client, escopo="a2")
    client.post("/api/certificacao/a2", headers=CABECALHO,
                json={"author": "teste", "selar": True})
    assert foto() == antes


# ------------------- 7. execução e alvo conferidos em cada etapa


def test_cada_etapa_confere_a_EXECUCAO_e_o_ALVO_esperados(
    conn, client, cenario
):
    """Garantia 7, e ela tem duas metades.

    O **alvo** foi congelado no início e cada etapa o relê; a **cópia** carrega
    a impressão em que a etapa anterior a deixou, e a cadeia é imposta pelo
    banco. Uma cópia perdida ou fora da impressão **aborta** — nunca é
    recriada em silêncio.
    """
    client.post("/api/certificacao/a2", headers=CABECALHO,
                json={"author": "teste"})
    estado = client.get("/api/certificacao/a2", headers=CABECALHO).json()
    execucao_id = estado["execucao_id"]

    client.post("/api/certificacao/a2", headers=CABECALHO,
                json={"author": "teste"})
    blocos = conn.execute(
        "SELECT indice_bloco, conteudo_json FROM certificacao_bloco"
        " WHERE execucao_id = ? ORDER BY indice_bloco", (execucao_id,),
    ).fetchall()
    assert len(blocos) >= 2

    # A CADEIA: a impressão depois de uma etapa é a de antes da seguinte.
    primeira = json.loads(blocos[0]["conteudo_json"])
    segunda = json.loads(blocos[1]["conteudo_json"])
    assert primeira["impressao_depois"] == segunda["impressao_antes"], (
        "a cadeia de impressoes da copia nao fecha entre as etapas"
    )

    # E o ALVO congelado é o mesmo em que a execução nasceu.
    linha = conn.execute(
        "SELECT alvo_hash FROM certificacao_execucao WHERE id = ?",
        (execucao_id,),
    ).fetchone()
    assert estado["alvo_de_certificacao_hash"] == linha["alvo_hash"]


# ------------------------- 8. manifesto só depois de todas as etapas


def test_o_MANIFESTO_so_depois_de_TODAS_as_etapas(conn, client, cenario):
    """Garantia 8, e o banco é quem impõe.

    Um manifesto parcial seria ilegível: ele afirmaria um escopo inteiro sobre
    metade dele. O gatilho da migração 32 recusa, e o teste pede o selo CEDO de
    propósito para que a recusa seja exercitada, e não suposta.
    """
    primeira = client.post("/api/certificacao/a2", headers=CABECALHO,
                           json={"author": "teste"})
    execucao_id = primeira.json()["execucao_id"]
    assert _manifesto(conn, execucao_id) is None

    # SELAR CEDO não sela, e a recusa é EXPLÍCITA: 409, não um 200 silencioso
    # que deixaria quem chamou achando que selou.
    cedo = client.post("/api/certificacao/a2", headers=CABECALHO,
                       json={"author": "teste", "selar": True})
    assert cedo.status_code == 409, cedo.text
    assert _manifesto(conn, execucao_id) is None, "selou com etapa faltando"

    for _ in range(len(esc.PLANO_A2) - 1):
        client.post("/api/certificacao/a2", headers=CABECALHO,
                    json={"author": "teste"})
    selo = client.post("/api/certificacao/a2", headers=CABECALHO,
                       json={"author": "teste", "selar": True})
    assert selo.status_code == 200, selo.text

    manifesto = _manifesto(conn, execucao_id)
    assert manifesto is not None
    assert manifesto["escopo"] == "a2"
    assert len(manifesto["casos"]) == esc.CASOS_ESPERADOS[esc.A2]
    assert manifesto["suite"]["blocos"] == len(esc.PLANO_A2)


def test_o_parcelado_NAO_e_mais_do_a1a_e_o_escopo_vem_da_LINHA(conn, client,
                                                               cenario):
    """O que a OP-3 mudou estruturalmente: `esc.A1A` deixou de ser literal.

    Ele estava cravado em dez pontos de `parcelado.py`, e um escopo novo
    exigiria achar os dez. Hoje o escopo vem da linha da execução, e `PLANOS`
    é a única lista.
    """
    from app.certificacao import parcelado

    from tests._prosa import codigo_sem_prosa

    # SEM PROSA: o docstring de `ultima_do_alvo` cita `a1a` para explicar o
    # default, e uma guarda que proíbe a explicação de por que algo é assim
    # empurra para apagar o comentário - a lição do incremento 11.
    import pathlib as _p

    codigo = codigo_sem_prosa(_p.Path("app/certificacao/parcelado.py"))
    # Sobram DOIS, e os dois são `escopo: str = esc.A1A` — o default de
    # compatibilidade de `ultima_do_alvo` e de `iniciar`. Nenhum é escrita.
    assert codigo.count("esc . A1A") == 2, codigo.count("esc . A1A")
    assert codigo.count("escopo : str = esc . A1A") == 2
    fonte = inspect.getsource(parcelado)
    assert 'linha["escopo"]' in fonte
    assert "esc.PLANOS" in fonte

    client.post("/api/certificacao/a2", headers=CABECALHO,
                json={"author": "teste"})
    assert conn.execute(
        "SELECT escopo FROM certificacao_execucao ORDER BY id DESC LIMIT 1"
    ).fetchone()["escopo"] == "a2"


def test_uma_execucao_de_OUTRO_escopo_nao_e_confundida(conn, client, cenario):
    """`ultima_do_alvo` passou a receber o escopo. Sem isso, o a2 continuaria
    a execução do a1a do mesmo alvo — e as duas têm planos diferentes."""
    from app.certificacao import alvo as alvo_mod
    from app.certificacao import parcelado
    from app.dataset import loader as dataset_loader

    client.post("/api/certificacao/a1a", headers=CABECALHO,
                json={"author": "teste"})
    client.post("/api/certificacao/a2", headers=CABECALHO,
                json={"author": "teste"})

    meta = dataset_loader.dataset_vigente(conn)
    alvo = alvo_mod.montar(conn, dataset_hash=meta.sha256)[
        "alvo_de_certificacao_hash"
    ]
    do_a1a = parcelado.ultima_do_alvo(conn, alvo, escopo="a1a")
    do_a2 = parcelado.ultima_do_alvo(conn, alvo, escopo="a2")
    assert do_a1a is not None and do_a2 is not None
    assert do_a1a != do_a2

    with pytest.raises(ValueError):
        parcelado.ultima_do_alvo(conn, alvo, escopo="a3")


def test_a_lista_do_SQL_e_a_do_PYTHON_nao_divergem(conn):
    """Duas listas fechadas iguais em arquivos diferentes DIVERGEM.

    A migração 34 escreve `('a1a','a2')` em três gatilhos; `escopos.PLANOS`
    escreve a mesma coisa em Python. Aqui a divergência aparece na suíte, e não
    na primeira etapa de um escopo novo — que é onde ela apareceria sozinha.
    """
    import re

    sql = "\n".join(
        r["sql"] or "" for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger'"
            "   AND name IN ('copia_so_em_execucao_parcelada',"
            "                'bloco_parcelado_exige_tentativa',"
            "                'bloco_parcelado_carrega_impressoes')"
        )
    )
    assert sql, "os tres gatilhos da migracao 34 tem de existir"
    do_sql = {
        tuple(sorted(re.findall(r"'(\w+)'", grupo)))
        for grupo in re.findall(r"IN \(([^)]*'a1a'[^)]*)\)", sql)
    }
    assert do_sql, "nenhuma lista de escopos encontrada no SQL"
    do_python = tuple(sorted(esc.PLANOS))
    assert do_sql == {do_python}, (do_sql, do_python)


def test_os_gatilhos_ANTIGOS_do_a1a_nao_existem_mais(conn):
    """A migração 34 os SUBSTITUI. Se os dois pares convivessem, o antigo
    continuaria valendo e o novo pareceria ter resolvido."""
    antigos = [
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            "   AND name IN ('bloco_a1a_exige_tentativa',"
            "                'bloco_a1a_carrega_impressoes')"
        )
    ]
    assert antigos == [], antigos
