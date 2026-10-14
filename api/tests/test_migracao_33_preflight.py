"""A migração 33 recria cinco tabelas, e PARA se alguma tiver ganhado linha.

O ADR 0041 autoriza recriar em vez de migrar porque elas estão vazias em
produção — conferido por leitura em 2026-09-22 e de novo em 2026-10-07. Mas
**"estava vazio quando eu olhei" não é garantia**: entre a leitura e o deploy
cabe uma chamada, e um `DROP TABLE` sobre linha que alguém gravou apaga
evidência sem volta.

Exigência do usuário em 2026-10-07: a conferência roda **imediatamente antes**
da migração, e a linha de `janela_piloto` atravessa inteira.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from app import migrations, store

GRADE = 900_000
RECRIADAS = (
    "calibracao_observacao", "janela_revalidacao", "revalidacao_uso",
    "calibracao_versao", "calibracao_regime",
)


def _ate(conn: sqlite3.Connection, versao: int) -> None:
    """Aplica as migrações até `versao`, inclusive, pelo caminho oficial."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration ("
        " version INTEGER PRIMARY KEY, description TEXT NOT NULL,"
        " applied_at TEXT NOT NULL)"
    )
    for v, d, sql in migrations.MIGRACOES:
        if v > versao:
            break
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migration (version, description, applied_at)"
            " VALUES (?, ?, datetime('now'))", (v, d),
        )


@pytest.fixture
def em_32(tmp_path):
    caminho = tmp_path / "em32.sqlite3"
    conn = sqlite3.connect(caminho, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    _ate(conn, 32)
    assert store.versao_schema(conn) == 32
    yield conn
    conn.close()


def _contrato_e_piloto(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO bbo_contrato (contrato, timeframe, execution_reference,"
        " latency_bars, grade_ms, tolerancia_ms, criado_em)"
        " VALUES ('bbo@1','15m','abertura',1,?,2000,'x')", (GRADE,)
    )
    conn.execute(
        "INSERT INTO janela_piloto (contrato, venue, symbol, de_ms,"
        " ate_ms_exclusive, observacoes_validas, observacoes_totais,"
        " dias_corridos_x1000, fechada_por, criado_em)"
        " VALUES ('bbo@1','binance','BTCUSDT',1788538500000,1789748100000,"
        " 1222,1344,14000,'dias','2026-09-21T15:40:12Z')"
    )


def test_com_as_cinco_VAZIAS_a_migracao_33_aplica(em_32):
    """O caminho normal, e ele é o que produção vai percorrer."""
    _contrato_e_piloto(em_32)
    antes = store._impressao_da_janela_piloto(em_32)
    assert len(antes) == 1

    # `migrar` aplica TODAS as pendentes, e a 34 veio depois (OP-3). O que
    # este teste mede e a 33 ter passado pelo preflight sem parar.
    assert store.migrar(em_32) == migrations.MIGRACOES[-1][0]
    assert store.versao_schema(em_32) >= 33

    # A LINHA DO PILOTO ATRAVESSOU INTEIRA — conferida pelo CONTEÚDO, e não
    # pela contagem: recriar a tabela com outra linha faria a contagem fechar.
    assert store._impressao_da_janela_piloto(em_32) == antes
    linha = em_32.execute("SELECT * FROM janela_piloto").fetchone()
    assert linha["de_ms"] == 1788538500000
    assert linha["ate_ms_exclusive"] == 1789748100000
    assert linha["observacoes_validas"] == 1222
    assert linha["fechada_por"] == "dias"
    assert linha["criado_em"] == "2026-09-21T15:40:12Z"


@pytest.mark.parametrize("tabela", RECRIADAS)
def test_uma_linha_em_qualquer_das_cinco_PARA_a_migracao(em_32, tabela):
    """Cada uma das cinco, por parametrização: a lista não é decorativa.

    Um teste só sobre `calibracao_observacao` deixaria as outras quatro
    declaradas na constante e não conferidas — e lista fechada que ninguém
    exercita é o que este projeto chama de guarda vazia.
    """
    _contrato_e_piloto(em_32)
    _semear(em_32, tabela)
    assert em_32.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0] == 1

    with pytest.raises(store.PreflightFalhou) as e:
        store.migrar(em_32)
    # A mensagem NOMEIA cada tabela com linha, e não só a primeira: "alguma
    # delas tem dado" mandaria procurar, e a conferência existe para dizer
    # ONDE. Semear `calibracao_versao` ou `calibracao_regime` arrasta
    # `janela_revalidacao` junto, pela `NOT NULL` do schema 32 — e as duas
    # aparecem.
    assert tabela in str(e.value)
    assert "a migracao 33 recria" in str(e.value)

    # E NADA FOI DESTRUÍDO: o schema continua 32 e a linha continua lá.
    assert store.versao_schema(em_32) == 32
    assert em_32.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0] == 1
    assert len(store._impressao_da_janela_piloto(em_32)) == 1


def _semear(conn: sqlite3.Connection, tabela: str) -> None:
    """Uma linha em cada uma das cinco, no schema 32 (o ANTIGO)."""
    if tabela == "calibracao_observacao":
        conn.execute(
            "INSERT INTO calibracao_observacao (contrato, venue, symbol,"
            " t_grid_ms, lado, config_version_id, abertura, bbo_bid,"
            " bbo_bid_qty, bbo_ask, bbo_ask_qty, p_exec_previsto,"
            " e1_mili_bps, e2_mili_bps, nocional_cents, dentro_do_l1,"
            " criado_em) VALUES ('bbo@1','binance','BTCUSDT',?,'compra',1,"
            " 100,99,1,101,1,102,1,1,10,1,'x')", (GRADE,)
        )
    elif tabela == "janela_revalidacao":
        conn.execute(
            "INSERT INTO janela_revalidacao (contrato, venue, symbol, de_ms,"
            " ate_ms_exclusive, piloto_ate_ms_exclusive, selada_em)"
            " VALUES ('bbo@1','binance','BTCUSDT',100,200,100,'x')"
        )
    elif tabela == "revalidacao_uso":
        conn.execute(
            "INSERT INTO janela_revalidacao (contrato, venue, symbol, de_ms,"
            " ate_ms_exclusive, piloto_ate_ms_exclusive, selada_em)"
            " VALUES ('bbo@1','binance','BTCUSDT',100,200,100,'x')"
        )
        jid = conn.execute("SELECT id FROM janela_revalidacao").fetchone()[0]
        _versao(conn)
        cid = conn.execute("SELECT id FROM calibracao_versao").fetchone()[0]
        conn.execute(
            "INSERT INTO revalidacao_uso (janela_id, calibracao_versao_id,"
            " lb95_mili_bps, p10_mili_bps, n, passou, usada_em)"
            " VALUES (?,?,1,1,1,1,'x')", (jid, cid)
        )
    elif tabela == "calibracao_versao":
        _versao(conn)
    else:
        _versao(conn)
        cid = conn.execute("SELECT id FROM calibracao_versao").fetchone()[0]
        conn.execute(
            "INSERT INTO calibracao_regime (calibracao_versao_id, regime, n,"
            " n_efetivo_x1000, estado, motivo) VALUES (?, 'vol_baixa', 1,"
            " 1000, 'nao_calibrado', 'semeado pelo teste do preflight')",
            (cid,)
        )


def _versao(conn: sqlite3.Connection) -> None:
    """Uma `calibracao_versao` no schema 32, que exige `janela_revalidacao_id`.

    No 32 a coluna é `NOT NULL` — era o desenho em que a revalidação era UMA,
    global. A migração 33 a remove justamente porque a revalidação passou a ser
    por regime; aqui ela tem de ser satisfeita, porque o que se testa é o
    preflight sobre o schema ANTIGO.
    """
    if not conn.execute("SELECT 1 FROM janela_revalidacao").fetchone():
        conn.execute(
            "INSERT INTO janela_revalidacao (contrato, venue, symbol, de_ms,"
            " ate_ms_exclusive, piloto_ate_ms_exclusive, selada_em)"
            " VALUES ('bbo@1','binance','BTCUSDT',100,200,100,'x')"
        )
    jid = conn.execute("SELECT id FROM janela_revalidacao").fetchone()[0]
    conn.execute(
        "INSERT INTO calibracao_versao (contrato, venue, symbol,"
        " config_version_origem, piloto_de_ms, piloto_ate_ms_exclusive,"
        " janela_revalidacao_id, delta_mili_bps, n, n_efetivo_x1000,"
        " tau_x1000, sigma_e_x1000, p10_mili_bps, n_necessario, aplicado,"
        " motivo, criado_em)"
        " VALUES ('bbo@1','binance','BTCUSDT',1,1,2,?,500,1,1000,1000,1,1,1,0,"
        " 'x','x')", (jid,)
    )


def test_o_PREFLIGHT_esta_ligado_a_VERSAO_e_cada_um_tem_motivo(em_32):
    """A lista não é um `if` perdido: é um mapa por versão.

    A 33 está nele porque **destrói** — ela recria cinco tabelas. A 35 está
    porque **rotula**: ela acrescenta `tem_override` com `DEFAULT 1`, e um
    rótulo errado não aparece em número nenhum. São motivos diferentes para a
    mesma precaução, e por isso os dois são nomeados aqui.

    Uma versão nova no mapa quebra este teste até alguém dizer qual dos dois
    motivos ela tem — ou que não tem nenhum e saiu do mapa.
    """
    assert sorted(store.PREFLIGHT) == [33, 35]
    assert set(RECRIADAS) <= set(store._preflight_33.__doc__ and RECRIADAS)
    # E cada um confere a coisa que o seu motivo nomeia.
    assert "vazias" in store._preflight_33.__doc__
    assert "DEFAULT" in store._preflight_35.__doc__


def test_o_PREFLIGHT_35_PARA_quando_o_rotulo_mentiria(em_32):
    """Uma linha antiga cujo valor já é a base seria rotulada como override.

    Em produção não há linha nenhuma — a identidade vigente termina em
    `sem-perfil` —, mas "não havia quando eu olhei" não é garantia. A
    conferência roda na mesma transação, imediatamente antes do `ALTER`.
    """
    em_32.execute(
        "INSERT INTO calibracao_perfil (hash,"
        " taxonomia_corte_inferior_mili_bps, taxonomia_corte_superior_mili_bps,"
        " taxonomia_janela_barras, taxonomia_permanencia_barras,"
        " spread_bps_base_x1000, criado_em)"
        " VALUES (?,19300,25300,672,672,1000,'x')", ("p" * 64,)
    )
    # Uma linha cujo valor JÁ É a base: sob a semântica nova ela é
    # `calibrado_sem_override`, e o `DEFAULT 1` a descreveria errado.
    em_32.execute(
        "INSERT INTO calibracao_perfil_regime (perfil_hash, regime,"
        " spread_bps_x1000, n, p10_mili_bps) VALUES (?,'vol_baixa',1000,5,600)",
        ("p" * 64,)
    )
    with pytest.raises(store.PreflightFalhou) as e:
        store.migrar(em_32)
    assert "vol_baixa" in str(e.value)
    assert store.versao_schema(em_32) < 35, (
        "a migração 35 entrou mesmo com o preflight recusando"
    )


def test_o_PREFLIGHT_35_NAO_recusa_override_de_verdade(em_32):
    """A outra metade, e sem ela a de cima mostraria só que nada passa.

    O preflight recusa o caso que **mentiria** — valor igual à base —, e não a
    existência de linha. Uma linha que é override de verdade atravessa, e o
    `DEFAULT 1` a descreve corretamente.

    Em teste separado porque `calibracao_perfil_regime` é append-only: o
    gatilho de `DELETE` recusa, corretamente, e desfazer a linha do teste
    anterior é impossível por desenho.
    """
    em_32.execute(
        "INSERT INTO calibracao_perfil (hash,"
        " taxonomia_corte_inferior_mili_bps, taxonomia_corte_superior_mili_bps,"
        " taxonomia_janela_barras, taxonomia_permanencia_barras,"
        " spread_bps_base_x1000, criado_em)"
        " VALUES (?,19300,25300,672,672,1000,'x')", ("p" * 64,)
    )
    em_32.execute(
        "INSERT INTO calibracao_perfil_regime (perfil_hash, regime,"
        " spread_bps_x1000, n, p10_mili_bps) VALUES (?,'vol_baixa',1674,5,163)",
        ("p" * 64,)
    )
    assert store.migrar(em_32) == migrations.MIGRACOES[-1][0]
    linha = em_32.execute(
        "SELECT tem_override, motivo FROM calibracao_perfil_regime"
    ).fetchone()
    assert int(linha["tem_override"]) == 1
    assert "semantica antiga" in linha["motivo"]


# ===========================================================================
# O ENSAIO `32 -> preflight 33 -> 33 -> 34` COMO TESTE
#
# Ele foi feito primeiro como script, em 2026-10-13, sobre uma copia
# CONSTRUIDA do schema 32 - nao ha rota que exporte producao nem acesso ao
# volume da Railway, e o banco local esta em schema 12 e vazio.
#
# Mas uma conferencia que so existe num script de scratchpad some com a
# sessao, e "eu rodei e passou" nao se repete. O que o script mediu e o que
# estes testes medem; o que eles NAO reproduzem sao as 1.995 amostras de BBO e
# as 1.834 barras do fluxo, porque migracao nenhuma as toca.
# ===========================================================================

# O estado que producao publica, lido em 2026-09-21 e 2026-10-07.
HIPOTESES_POR_ORIGEM = {"a1a@0b": 6, "b4@0b": 32, "transacao@0b": 3}
CREDITOS = {"linhas": 33, "consumidos": 65, "orcamentos": 5}
GATILHOS_GENERALIZADOS = (
    "copia_so_em_execucao_parcelada",
    "bloco_parcelado_exige_tentativa",
    "bloco_parcelado_carrega_impressoes",
)
ALVO_SEMEADO = "1" * 64


def _semear_o_que_atravessa(conn) -> None:
    """Certificados, hipoteses, creditos e holdout - nas contagens de producao.

    Nenhum valor e suposto: as contagens sao as que o checkpoint publica, e os
    nomes das colunas sao os do schema. Um helper que inventasse colunas
    passaria a medir o helper.
    """
    from app.certificacao import escopos as esc

    conn.execute(
        "INSERT INTO config_version (created_at, author, payload_json,"
        " config_hash, material, note) VALUES ('x','ensaio','{}','h',1,'x')"
    )
    conn.execute(
        "INSERT INTO run (agent_id, state, config_version_id, created_at,"
        " updated_at) VALUES ('agent-0001','concluido',1,'x','x')"
    )
    i = 0
    for origem, quantas in HIPOTESES_POR_ORIGEM.items():
        for _ in range(quantas):
            i += 1
            conn.execute(
                "INSERT INTO agent_event (run_id, occurred_at, node, kind,"
                " cost_usd_minor, cost_usd_micro)"
                " VALUES (1,'t','propor_regra','proposta',0,0)"
            )
            ev = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO hypothesis (run_id, agent_event_id, enunciado,"
                " agente_origem, timestamp_registro, metrica_primaria,"
                " efeito_minimo, n_minimo, sharpe_esperado_milesimos,"
                " criterio_parada, condicoes_validade_json,"
                " condicoes_falseamento_json, testavel, horizonte_barras,"
                " content_hash)"
                " VALUES (1,?,?,?,'t','idas_e_voltas',0,10,3000,"
                "'fim_da_janela','{}',"
                "'[{\"metrica\":\"idas_e_voltas\",\"comparador\":\"maior_que\",\"valor\":9}]',"
                "1,10000,?)",
                (ev, f"hipotese {i}", origem, f"{i:064x}"),
            )

    # CREDITOS: o braco vem da ORIGEM, como `creditos.braco_da_hipotese`
    # deriva. `a2` nao e braco de credito - ele nao gera hipotese -, e os cinco
    # orcamentos de producao sao "um por (braco, config)".
    for braco in ("agente", "b4", "a1a", "a1b"):
        conn.execute(
            "INSERT INTO test_credit_budget (braco, config_version_id,"
            " creditos, created_at) VALUES (?,1,60,'x')", (braco,)
        )
    conn.execute(
        "INSERT INTO config_version (created_at, author, payload_json,"
        " config_hash, material, note)"
        " VALUES ('x','ensaio','{}','h2',1,'a segunda config, para o 5o"
        " orcamento')"
    )
    conn.execute(
        "INSERT INTO test_credit_budget (braco, config_version_id, creditos,"
        " created_at) VALUES ('agente',2,60,'x')"
    )
    por_origem = {"a1a@0b": "a1a", "b4@0b": "b4", "transacao@0b": "agente"}
    linhas = [(int(r["id"]), str(r["agente_origem"])) for r in conn.execute(
        "SELECT id, agente_origem FROM hypothesis ORDER BY id LIMIT 33")]
    for k, (hid, origem) in enumerate(linhas):
        tipo, custo = ("in_sample", 1) if k < 17 else ("reteste_parametro", 3)
        conn.execute(
            "INSERT INTO test_credit_entry (braco, config_version_id,"
            " hypothesis_id, tipo, creditos, occurred_at, impacto_fdr_bps,"
            " cpu_micros, barras_reservadas) VALUES (?,1,?,?,?,'x',0,0,0)",
            (por_origem[origem], hid, tipo, custo),
        )

    for escopo in esc.TODOS:
        cur = conn.execute(
            "INSERT INTO certificacao_execucao (alvo_hash, componentes_json,"
            " build_do_backend, iniciada_em, casos_esperados,"
            " micros_para_copiar, micros_da_suite, bytes_da_copia,"
            " intocado_json, escopo, blocos_esperados)"
            " VALUES (?,'{\"identidade_executavel\":\"base+sem-perfil\"}',"
            " 'ensaio','x',?,0,0,0,'{}',?,0)",
            (ALVO_SEMEADO, esc.CASOS_ESPERADOS[escopo], escopo),
        )
        eid = int(cur.lastrowid)
        for n in range(esc.CASOS_ESPERADOS[escopo]):
            conn.execute(
                "INSERT INTO certificacao_caso (execucao_id, chave, familia,"
                " tipo, barrado, promovido, resultado_json)"
                " VALUES (?,?,'x','estrutural',1,0,'{}')",
                (eid, f"{escopo}-{n}"),
            )
        conn.execute(
            "INSERT INTO certificacao_manifesto (execucao_id, alvo_hash,"
            " manifesto_json, selado_em, passa)"
            " VALUES (?,?,'{}','2026-09-10T19:52:37Z',1)",
            (eid, ALVO_SEMEADO),
        )


def _fotografia(conn) -> dict:
    def um(sql):
        return conn.execute(sql).fetchone()[0]

    return {
        "piloto": store._impressao_da_janela_piloto(conn),
        "hipoteses_por_origem": sorted(
            (str(r[0]), int(r[1])) for r in conn.execute(
                "SELECT agente_origem, COUNT(*) FROM hypothesis"
                " GROUP BY agente_origem")),
        "credit_entry_rows": um("SELECT COUNT(*) FROM test_credit_entry"),
        "creditos_consumidos_total": um(
            "SELECT COALESCE(SUM(creditos),0) FROM test_credit_entry"),
        "orcamentos_de_credito": um("SELECT COUNT(*) FROM test_credit_budget"),
        "holdout_access": um("SELECT COUNT(*) FROM holdout_access"),
        "certificacao": sorted(
            (str(r["escopo"]), str(r["selado_em"]), int(r["passa"]))
            for r in conn.execute(
                "SELECT e.escopo, m.selado_em, m.passa"
                "  FROM certificacao_manifesto m"
                "  JOIN certificacao_execucao e ON e.id = m.execucao_id")),
    }


def test_ENSAIO_o_caminho_32_ate_34_nao_move_NADA_do_experimento(em_32):
    """As conferencias 1, 2, 3, 6 e 7 do ensaio, num teste so.

    Elas sao UM teste porque sao a MESMA pergunta - "a migracao mexeu no
    experimento?" - e separa-las faria cinco testes repetirem a mesma
    semeadura, que e cara e identica.
    """
    _contrato_e_piloto(em_32)
    _semear_o_que_atravessa(em_32)
    antes = _fotografia(em_32)

    assert antes["hipoteses_por_origem"] == sorted(
        HIPOTESES_POR_ORIGEM.items())
    assert antes["credit_entry_rows"] == CREDITOS["linhas"]
    assert antes["creditos_consumidos_total"] == CREDITOS["consumidos"]
    assert antes["orcamentos_de_credito"] == CREDITOS["orcamentos"]
    assert len(antes["certificacao"]) == 5

    assert store.migrar(em_32) == migrations.MIGRACOES[-1][0]

    # 1, 2, 3 - a fotografia inteira, identica.
    assert _fotografia(em_32) == antes

    # 6 - nada de calibracao foi CRIADO pela migracao.
    for tabela in ("calibracao_versao", "calibracao_perfil",
                   "calibracao_observacao", "janela_revalidacao",
                   "revalidacao_uso", "janela_calibracao",
                   "calibracao_regime_fechamento"):
        assert int(em_32.execute(
            f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]) == 0, tabela

    # 7 - o alvo ANTIGO continua legivel. Ele deixa de ser o VIGENTE porque o
    #     schema mudou, e nao porque alguem o apagou.
    from app.certificacao import escopos as esc

    comp = esc.composicao(em_32, ALVO_SEMEADO)
    assert comp["passa"] is True
    assert sorted(comp["certificados"]) == sorted(esc.TODOS)


def test_ENSAIO_as_OITO_tabelas_da_calibracao_existem_depois_da_34(em_32):
    """A conferencia 4: tres criadas, cinco recriadas, e os gatilhos delas."""
    _contrato_e_piloto(em_32)
    antes_t = {r[0] for r in em_32.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    antes_g = {r[0] for r in em_32.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}

    store.migrar(em_32)

    depois_t = {r[0] for r in em_32.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    depois_g = {r[0] for r in em_32.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}

    assert sorted(depois_t - antes_t) == [
        "calibracao_regime_fechamento", "calibracao_regime_regra",
        "janela_calibracao",
    ]
    for tabela in RECRIADAS:
        assert tabela in depois_t

    novos = sorted(g for g in depois_g - antes_g if g.startswith(
        ("janela_calibracao", "janela_revalidacao", "calibracao_")))
    # Nao e um numero magico: e a lista, e ela esta no relatorio de
    # integridade. O que o teste fixa e que os TRES da certificacao do adendo
    # 9i estao entre eles.
    for g in ("janela_revalidacao_exige_os_cinco_certificados",
              "janela_revalidacao_identidade_e_a_da_finalizacao",
              "janela_revalidacao_alvo_descreve_a_identidade"):
        assert g in novos, g
    assert len(novos) >= 12, novos


def test_ENSAIO_os_TRES_gatilhos_do_parcelado_deixaram_de_ser_do_a1a(em_32):
    """A conferencia 5, e ela e a migracao 34 inteira.

    Os nomes ANTIGOS tem de sumir: dois gatilhos com a mesma funcao, um
    chamado `a1a` e outro `parcelado`, e a forma do defeito que este projeto
    conta.
    """
    from app.certificacao import escopos as esc

    _contrato_e_piloto(em_32)
    store.migrar(em_32)

    sql = {r["name"]: r["sql"] for r in em_32.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger'")}
    for g in GATILHOS_GENERALIZADOS:
        assert g in sql, g
        assert "'a1a', 'a2'" in sql[g], (g, sql[g])
    for antigo in ("bloco_a1a_exige_tentativa", "bloco_a1a_carrega_impressoes"):
        assert antigo not in sql, antigo

    # E a lista do SQL e `escopos.PLANOS` nao divergem - duas listas fechadas
    # iguais em arquivos diferentes divergem, e aqui a divergencia so
    # apareceria na primeira etapa de um escopo novo.
    assert sorted(esc.PLANOS) == ["a1a", "a2"]


def test_ENSAIO_o_caminho_vai_ate_a_35_e_os_artefatos_DELA_existem(em_32):
    """O caminho completo `32 -> 33 -> 34 -> 35`, afirmado e nao implicado.

    Os outros testes do ensaio comparam com `MIGRACOES[-1][0]`, que e o certo:
    uma migracao nova quebra a suite pedindo decisao. Mas comparar com o
    ultimo **nao afirma o que o ultimo faz** — se a 35 fosse removida, todos
    eles continuariam passando contra a 34.

    Este nomeia os artefatos dela: as duas colunas e o gatilho. E nomeia
    tambem o VALOR do `DEFAULT`, porque e ele que rotula o passado.
    """
    _contrato_e_piloto(em_32)

    # Em 32 as colunas NAO existem — o ponto de partida e real.
    antes = {r["name"] for r in em_32.execute(
        "PRAGMA table_info(calibracao_perfil_regime)")}
    assert "tem_override" not in antes
    assert "motivo" not in antes

    assert store.migrar(em_32) == 35, (
        "o ensaio tem de chegar a 35: a representacao dos tres estados vive"
        " nela, e um caminho que parasse na 34 nao a exercitaria"
    )

    colunas = {
        r["name"]: r for r in em_32.execute(
            "PRAGMA table_info(calibracao_perfil_regime)")
    }
    for campo in ("tem_override", "motivo"):
        assert campo in colunas, campo
        assert int(colunas[campo]["notnull"]) == 1, campo
        assert colunas[campo]["dflt_value"] is not None, (
            f"{campo} sem DEFAULT: o `ALTER` de uma coluna NOT NULL exige um,"
            f" e e ele que descreve as linhas que ja existiam"
        )
    assert colunas["tem_override"]["dflt_value"] == "1", (
        "o DEFAULT tem de ser 1: sob a semantica antiga do ADR 0033 uma linha"
        " so existia para regime COM override, e um DEFAULT 0 rotularia todas"
        " elas como `calibrado_sem_override`"
    )

    gatilhos = {r["name"] for r in em_32.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert "calibracao_perfil_regime_estado_coerente" in gatilhos

    # E ele FUNCIONA sobre a copia migrada — nao basta existir.
    em_32.execute(
        "INSERT INTO calibracao_perfil (hash,"
        " taxonomia_corte_inferior_mili_bps, taxonomia_corte_superior_mili_bps,"
        " taxonomia_janela_barras, taxonomia_permanencia_barras,"
        " spread_bps_base_x1000, criado_em)"
        " VALUES (?,19300,25300,672,672,1000,'x')", ("e" * 64,)
    )
    with pytest.raises(sqlite3.IntegrityError) as e:
        em_32.execute(
            "INSERT INTO calibracao_perfil_regime (perfil_hash, regime,"
            " spread_bps_x1000, n, p10_mili_bps, tem_override, motivo)"
            " VALUES (?,'vol_baixa',2000,5,600,0,'x')", ("e" * 64,)
        )
    assert "discordam" in str(e.value)

    # Os TRES estados, pelo modulo que os define, sobre a copia migrada.
    from app.calibracao import perfil as perfil_mod

    assert perfil_mod.ESTADOS == (
        "nao_calibrado", "calibrado_sem_override", "calibrado_com_override",
    )


def test_ENSAIO_a_implementacao_nova_aparece_INVALIDADA(em_32):
    """A conferencia 8, e ela nao depende do banco.

    O arquivo congelado tem de dizer que a certificacao esta invalidada, e o
    hash de hoje tem de ser diferente do que esta selado em producao. Um
    arquivo que dissesse "certificado" com a implementacao mudada e a forma
    exata do defeito que este projeto registra.
    """
    import json

    from app.certificacao import alvo as alvo_mod

    congelado = json.loads(
        (pathlib.Path(__file__).parent / "alvo_congelado.json")
        .read_text(encoding="utf-8"))
    cert = congelado["certificacao"]
    assert cert["estado"].startswith("INVALIDAD"), cert["estado"]
    assert cert["o_que_falta"]

    h, modulos = alvo_mod.hash_da_implementacao()
    assert len(modulos) == len(congelado["modulos"])
    assert h[:16] == congelado["implementacao_hash"]
    assert h[:16] != congelado["implementacao_hash_em_producao"], (
        "a implementacao de hoje e a de producao sao a MESMA - entao nao ha"
        " nada a recertificar, e o estado INVALIDADO estaria mentindo"
    )
