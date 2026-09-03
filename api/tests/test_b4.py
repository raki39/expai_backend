"""Incremento 12: B4, o controle que a 0A nao tinha.

§14.3 e o motivo de tudo aqui: sem B4, aprovar na Fase 0 nao distingue duas
explicacoes muito diferentes:

- a reflexao com modelo de qualidade gera hipoteses melhores;
- trabalho quant competente com validacao rigorosa funciona, e o LLM e um
  custo decorativo.

Os cinco critterios do plano, e o primeiro sustenta o resto: se B4 tivesse um
caminho de validacao proprio, a comparacao mediria a diferenca entre duas
implementacoes em vez de entre busca de parametro e reflexao.
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3

import pytest

from app.b4 import braco, busca
from app.config.schema import ExperimentConfig
from app.hipotese import registro as hipotese_registro
from app.validador import contador, lote
from tests.test_cerebro import settings  # noqa: F401
from tests.test_maos_rapidas import criar_dataset, precos_passeio

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
def cenario_sem_baseline(conn: sqlite3.Connection):
    """Dataset dividido e SEM comparacao. Para a recusa de `BaselineAusente`."""
    dataset_id = criar_dataset(conn, precos_passeio(3_000))
    return dataset_id, ExperimentConfig()


@pytest.fixture
def cenario_b4(conn: sqlite3.Connection, cenario_sem_baseline):
    """Janela curta, com os baselines rodados.

    B4 exige o B3 sob a mesma `config_version`: a metrica primaria dele e
    `excesso_sobre_b3_cents`, e sem o baseline os dezesseis vereditos sairiam
    `None`. A ordem aqui e a mesma do painel - Dataset, Baselines, e so
    depois o braco.

    Curta de proposito: B4 abre 16 runs, e o teste nao precisa de dois anos.
    Nesta janela nada e testavel, o que exercita o caminho da D33; o caminho
    com credito cobrado esta em `cenario_testavel`.
    """
    from app.maos_rapidas import baselines

    dataset_id, cfg = cenario_sem_baseline
    baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        semente=cfg.default_seed,
    )
    return dataset_id, cfg


@pytest.fixture
def resultado(conn: sqlite3.Connection, cenario_b4):
    dataset_id, cfg = cenario_b4
    return braco.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1
    )


# ===========================================================================
# CRITERIO 1 - o mesmo caminho de validacao, e nao uma implementacao paralela
# ===========================================================================


def _importados(caminho: pathlib.Path) -> set[str]:
    arvore = ast.parse(caminho.read_text(encoding="utf-8"))
    saida: set[str] = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.ImportFrom) and no.module:
            saida.add(no.module)
        elif isinstance(no, ast.Import):
            saida.update(a.name for a in no.names)
    return saida


def test_b4_nao_reimplementa_o_caminho_de_validacao() -> None:
    """Ele CHAMA `app/validador` e `app/hipotese`, e nao copia nenhum dos dois.

    O mesmo princípio do nucleo de precificacao compartilhado do incremento 4:
    a paridade e por construcao, e nao por disciplina. Duas implementacoes que
    hoje concordam sao duas implementacoes livres para divergir amanha - e
    quando divergissem, o placar da fase mediria a divergencia.
    """
    modulo = APP / "b4" / "braco.py"
    imports = _importados(modulo)

    assert any("validador" in i for i in imports), (
        "B4 tem de pedir o parecer ao mesmo validador do agente"
    )
    assert any("hipotese" in i for i in imports), (
        "o pre-registro de B4 passa pelo mesmo registro de §8.2"
    )
    assert any("maos_rapidas" in i for i in imports), (
        "B4 executa pelo mesmo simulador"
    )

    # E nao ha nenhuma escrita propria nas tabelas do protocolo. Uma insercao
    # aqui seria um segundo caminho de escrita para a tabela que a regra 16
    # chama de autoridade.
    fonte = modulo.read_text(encoding="utf-8")
    for tabela in ("hypothesis", "hypothesis_state", "test_credit_entry",
                   "rule_proposal", "ledger_entry"):
        assert f"INSERT INTO {tabela}" not in fonte, (
            f"B4 escreve direto em `{tabela}` em vez de chamar o modulo dono"
        )


def test_o_parecer_de_b4_sai_da_mesma_funcao_do_agente(
    conn: sqlite3.Connection, resultado
) -> None:
    """Prova de comportamento, e nao so de import.

    Um teste que so olha `import` passaria com o modulo importado e nunca
    chamado - foi assim que `BLOCOS` ficou declarado sem leitor no incremento
    6. Aqui a asercao e que as hipoteses de B4 tem estado na maquina de §8.1, e
    quem escreve `hypothesis_state` e exclusivamente `app/validador`.
    """
    from app.validador import estados

    assert resultado.hipoteses
    for h in resultado.hipoteses:
        atual = estados.atual(conn, h.hypothesis_id)
        assert atual is not None, (
            f"hipotese {h.hypothesis_id} sem estado: nao passou pelo validador"
        )
        assert atual.estado in {
            estados.ENTRADA, "candidata", "invalidado", "nao_testavel",
        }, atual.estado


# ===========================================================================
# CRITERIO 2 - mesmo orcamento, e a comparacao e POR CREDITO
# ===========================================================================


def test_o_orcamento_de_b4_e_o_mesmo_do_agente(
    conn: sqlite3.Connection, resultado, cenario_b4
) -> None:
    """60 nos dois bracos (D30). Um controle com mais tentativas nao e controle."""
    from app import creditos as creditos_mod

    _, cfg = cenario_b4
    b4 = creditos_mod.saldo(conn, braco="b4", config_version_id=1)
    assert b4 is not None
    assert b4.orcamento == cfg.creditos_por_braco == 60

    # E rodar de novo NAO ganha orcamento novo - senao bastaria reexecutar
    # para comprar tentativas (§8.6).
    creditos_mod.conceder(
        conn, braco="b4", config_version_id=1, creditos=cfg.creditos_por_braco
    )
    assert creditos_mod.saldo(
        conn, braco="b4", config_version_id=1
    ).orcamento == 60


def test_a_comparacao_e_por_credito_gasto_e_nao_por_hipotese(
    resultado,
) -> None:
    """§14.3, R44: "mede se a reflexao produz hipoteses melhores POR CREDITO".

    A taxa tem de ser um campo calculado, e nao algo que quem le o resumo
    divida na cabeca: uma taxa que cada leitor calcula e uma taxa que cada
    leitor calcula de um jeito.
    """
    d = resultado.como_dict()
    assert "sustentadas_por_credito_ppm" in d
    assert "creditos_consumidos" in d

    creditos = d["creditos_consumidos"]
    if creditos:
        assert d["sustentadas_por_credito_ppm"] == (
            d["sustentadas"] * 1_000_000 // creditos
        )
        assert d["por_que_sem_taxa"] is None
    else:
        # Sem denominador nao ha taxa, e devolver zero afirmaria que ela foi
        # medida. O campo diz por que esta vazio.
        assert d["sustentadas_por_credito_ppm"] is None
        assert d["por_que_sem_taxa"]


# ===========================================================================
# CRITERIO 3 - B4 nao consome tokens
# ===========================================================================


def test_b4_nao_importa_o_cerebro_em_lugar_nenhum() -> None:
    """A fronteira e de IMPORTACAO, verificavel, e nao convencao.

    A mesma forma da regra 3 para as maos rapidas. B4 sem acesso ao cerebro
    nao e uma promessa sobre intencao: e a impossibilidade de a chamada
    existir.
    """
    for arquivo in sorted((APP / "b4").glob("*.py")):
        for imp in _importados(arquivo):
            assert "cerebro" not in imp, f"{arquivo.name} importa {imp}"
            assert "provedor" not in imp, f"{arquivo.name} importa {imp}"
            assert "anthropic" not in imp and "openai" not in imp, (
                f"{arquivo.name} importa {imp}"
            )


def test_o_braco_inteiro_roda_sem_cliente_de_modelo(
    conn: sqlite3.Connection, resultado
) -> None:
    """Criterio 3, e a asercao vem do LEDGER.

    A fixture nao passa adaptador nenhum, nao ha `settings` com chave, e
    nenhum evento do braco tem provedor. O numero de reflexoes e derivado da
    mesma consulta que o painel usa - `provider IS NOT NULL` -, e nao de uma
    promessa no docstring do modulo.
    """
    assert resultado.hipoteses

    for h in resultado.hipoteses:
        reflexoes = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_event"
            " WHERE run_id = ? AND provider IS NOT NULL",
            (h.run_id,),
        ).fetchone()["n"]
        assert reflexoes == 0, f"run {h.run_id} de B4 chamou modelo"

        custo = conn.execute(
            "SELECT COALESCE(SUM(cost_usd_micro), 0) AS c FROM agent_event"
            " WHERE run_id = ?",
            (h.run_id,),
        ).fetchone()["c"]
        assert custo == 0, f"run {h.run_id} de B4 custou {custo} micros"

    # E nenhuma transacao de reflexao no ledger, nos dois livros.
    ids = tuple(h.run_id for h in resultado.hipoteses)
    marcadores = ",".join("?" * len(ids))
    assert conn.execute(
        f"SELECT COUNT(*) AS n FROM ledger_transaction"
        f" WHERE kind = 'reflexao' AND run_id IN ({marcadores})",
        ids,
    ).fetchone()["n"] == 0

    assert resultado.como_dict()["reflexoes"] == 0
    assert resultado.cpu_micros > 0, "so CPU, mas CPU existe e e medida"


def test_o_evento_de_b4_existe_e_e_nao_cognitivo(
    conn: sqlite3.Connection, resultado
) -> None:
    """Toda hipotese aponta para o evento que a produziu, inclusive as de B4.

    `hypothesis.agent_event_id` e `NOT NULL`, e isso nao e obstaculo a
    contornar: B4 DECIDE parametros, so nao reflete. O evento sai com provedor
    nulo, que e o que distingue as duas coisas no registro.
    """
    primeira = resultado.hipoteses[0]
    evento = conn.execute(
        "SELECT node, kind, tier, provider, model, cost_usd_minor"
        "  FROM agent_event WHERE run_id = ?",
        (primeira.run_id,),
    ).fetchone()
    assert evento["kind"] == "proposta_nao_cognitiva"
    assert evento["node"].startswith("b4_")
    assert evento["tier"] is None
    assert evento["provider"] is None
    assert evento["model"] is None
    assert evento["cost_usd_minor"] == 0


# ===========================================================================
# CRITERIO 4 - a mesma familia fechada e o mesmo contador global
# ===========================================================================


def test_as_hipoteses_de_b4_entram_na_mesma_familia_e_no_mesmo_contador(
    conn: sqlite3.Connection, resultado
) -> None:
    """Uma familia, dois bracos. §9.2: cada tentativa conta na multiplicidade.

    Se B4 tivesse familia propria, o agente competiria contra um limiar mais
    frouxo do que o real - e o DSR de cada braco desconheceria as tentativas
    do outro, o que subestima exatamente o que ele existe para corrigir.
    """
    membros = lote.membros(conn, 1)
    ids_no_lote = {m.hypothesis_id for m in membros}
    ids_de_b4 = {h.hypothesis_id for h in resultado.hipoteses}

    assert ids_de_b4, "o braco nao produziu hipotese"
    assert ids_de_b4 <= ids_no_lote, (
        "hipotese de B4 fora do lote fechado: a multiplicidade ficaria"
        " subestimada, e BY promoveria mais facil do que deveria"
    )

    # E no contador global, que alimenta o DSR e nunca e zerado.
    resumo = contador.resumo(conn)
    assert resumo["total"] >= len(ids_de_b4)
    por = {e["especialidade"]: e for e in resumo["por_especialidade"]}
    assert hipotese_registro.AGENTE_ORIGEM_B4 in por
    assert por[hipotese_registro.AGENTE_ORIGEM_B4]["tentativas"] == len(ids_de_b4)


def test_o_contador_separa_os_dois_bracos(
    conn: sqlite3.Connection, resultado
) -> None:
    """§8.6 quer o orcamento POR ESPECIALIDADE, e o contador precisa separar.

    Sem origem propria para B4, "quantas tentativas o controle fez" seria uma
    pergunta que o registro nao responde - e ela e o denominador da comparacao
    da fase.
    """
    origens = {
        l["agente_origem"]
        for l in conn.execute("SELECT DISTINCT agente_origem FROM hypothesis")
    }
    assert hipotese_registro.AGENTE_ORIGEM_B4 in origens
    assert hipotese_registro.AGENTE_ORIGEM_B4 != hipotese_registro.AGENTE_ORIGEM


def test_a_familia_fechada_recusa_b4_quando_o_teto_estoura(
    conn: sqlite3.Connection, cenario_b4
) -> None:
    """O teto e do LOTE, e nao de cada braco. Recusada, nunca truncada.

    Com teto de 8 e 16 candidatas, B4 bate na parede no meio do braco e o
    BANCO recusa. Truncar em silencio seria pior: o lote continuaria parecendo
    completo com a multiplicidade subestimada, o que empurra BY na direcao de
    promover.

    O teto vem da `config_version` do RUN, e nao do objeto Python que o
    chamador passa - foi assim que este teste falhou na primeira escrita, com
    `ExperimentConfig(familia_max_hipoteses=8)` que o gatilho nunca le. A
    correcao do teste e a prova de que o teto e do banco.
    """
    from app.maos_rapidas import baselines
    from tests.test_creditos import _config_version

    dataset_id, cfg = cenario_b4
    versao = _config_version(conn, teto=8)
    # O B3 tambem precisa existir SOB ESTA versao: comparar atravessando
    # config e o que §10.2.3 invalida, e B4 recusa antes de gastar credito.
    baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=versao,
        semente=cfg.default_seed,
    )
    with pytest.raises(sqlite3.IntegrityError, match="familia"):
        braco.rodar(
            conn, dataset_id=dataset_id, config=cfg, config_version_id=versao
        )

    # E parou NO teto, e nao antes nem depois: oito gravadas, a nona recusada.
    gravadas = conn.execute(
        "SELECT COUNT(*) AS n FROM hypothesis h JOIN run r ON r.id = h.run_id"
        " WHERE r.config_version_id = ?",
        (versao,),
    ).fetchone()["n"]
    assert gravadas == 8, gravadas


# ===========================================================================
# CRITERIO 5 - reprodutivel
# ===========================================================================


def test_a_mesma_semente_produz_o_mesmo_conjunto(cenario_b4) -> None:
    """R12 continua valendo na 0B, e aqui ele e sobre a BUSCA.

    O digest cobre regra e pre-registro de cada candidata, na ordem. Nao cobre
    resultado: essa pergunta e do `digest_do_run`, que sai dos lancamentos.
    """
    _, cfg = cenario_b4
    k = dict(config=cfg, duracao_barra_ms=900_000, horizonte_barras=21_024)

    a = busca.gerar(**k)
    b = busca.gerar(**k)
    assert busca.digest(a) == busca.digest(b)
    assert [x.como_dict() for x in a] == [x.como_dict() for x in b]

    # E outra semente produz outro conjunto - senao o digest seria constante e
    # o teste acima passaria por vacuidade.
    c = busca.gerar(**k, semente=cfg.default_seed + 1)
    assert busca.digest(c) != busca.digest(a)

    # A varredura NAO muda com a semente: ela e grade fixa. So a busca
    # aleatoria muda, e isso precisa ficar visivel.
    assert [x.como_dict() for x in a[: busca.QUANTAS_VARREDURA]] == [
        x.como_dict() for x in c[: busca.QUANTAS_VARREDURA]
    ]
    assert [x.como_dict() for x in a[busca.QUANTAS_VARREDURA:]] != [
        x.como_dict() for x in c[busca.QUANTAS_VARREDURA:]
    ]


def test_o_braco_reporta_o_digest_da_busca(resultado) -> None:
    _, = (1,)  # noqa: F841 - marcador de leitura
    assert resultado.digest_das_hipoteses
    assert len(resultado.digest_das_hipoteses) == 64


def test_sao_dezesseis_e_meio_a_meio() -> None:
    """D25 fixou 16 para B4, e §14.3 lista as duas tecnicas."""
    assert busca.QUANTAS == 16
    assert busca.QUANTAS_ALEATORIAS == busca.QUANTAS_VARREDURA == 8
    assert len(busca.GRADE) >= busca.QUANTAS_VARREDURA


# ===========================================================================
# O que B4 NAO pode parecer
# ===========================================================================


def test_o_enunciado_de_b4_declara_que_nao_houve_reflexao(
    conn: sqlite3.Connection, resultado
) -> None:
    """A linha mais facil de errar do modulo.

    O campo `enunciado` carrega, no braco do agente, a leitura de mercado
    dele. Se B4 escrevesse ali uma frase plausivel sobre deriva e
    volatilidade, o registro ficaria com duas afirmacoes indistinguiveis - uma
    pensada e uma gerada -, e a comparacao da fase perderia sentido no proprio
    dado que a sustenta.
    """
    for h in resultado.hipoteses[:3]:
        enunciado = conn.execute(
            "SELECT enunciado FROM hypothesis WHERE id = ?", (h.hypothesis_id,)
        ).fetchone()["enunciado"]
        assert "NAO COGNITIVO" in enunciado
        assert "Nenhuma reflexao produziu esta hipotese" in enunciado
        assert h.tecnica in enunciado


def test_a_metrica_de_b4_e_fixa_e_o_motivo_esta_escrito() -> None:
    """B4 nao escolhe a regua, e a assimetria e deliberada.

    Escolher um alvo falsificavel bom e trabalho cognitivo, e e parte do que a
    fase mede. Uma metrica variavel tambem deixaria B4 comprar sobrevivencia
    trocando de regua, que e o modo mais barato de o controle parecer bom.
    """
    from app.hipotese.schema import METRICAS

    assert busca.METRICA in METRICAS
    assert busca.METRICA != "patrimonio_final_cents", (
        "regra 14: desempenho sempre como excesso sobre baseline"
    )

    fonte = (APP / "b4" / "busca.py").read_text(encoding="utf-8")
    assert "excesso_sobre_b1_p50_cents" in fonte, (
        "a razao de NAO usar a metrica do B1 tem de estar escrita: ela seria a"
        " melhor, e o validador nao consegue ve-la"
    )


def test_b4_recusa_sem_separacao_por_finalidade(
    conn: sqlite3.Connection
) -> None:
    """A mesma recusa do ciclo do agente, pelo mesmo motivo (§8.5.1)."""
    from app.dataset import loader

    # Gravado direto, sem passar pela ingestao: `dataset_split` e apenas por
    # acrescimo, entao nao da para dividir e desfazer - e nao deveria dar.
    conn.execute(
        "INSERT INTO dataset (venue, symbol, timeframe, interval_ms, start_ms,"
        " end_ms, reserved_from_ms, bars, sha256, source, source_files_json,"
        " fetched_at, fidelity_level, price_scale_exp, volume_scale_exp)"
        " VALUES ('t','T','15m',900000,0,900000,900000,1,'h','t','[]','t',1,-2,-8)"
    )
    dataset_id = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    assert not loader.esta_dividido(conn, dataset_id)

    with pytest.raises(braco.SeparacaoAusente):
        braco.rodar(
            conn, dataset_id=dataset_id, config=ExperimentConfig(),
            config_version_id=1,
        )


# ===========================================================================
# A limitacao herdada, fixada para que o incremento 13 a encare
# ===========================================================================


def test_o_validador_ainda_nao_alcanca_o_b1_casado(
    conn: sqlite3.Connection, resultado
) -> None:
    """`excesso_sobre_b1_p50_cents` NAO e avaliavel pelo validador hoje.

    E a metrica que §14.4 gateia - "acima do p95 de B1" -, e por isso este
    teste existe: ele fixa a limitacao em vez de deixa-la ser redescoberta no
    incremento 13, quando o criterio virar portao.

    A causa: `promocao._b1_do_run` procura `baseline_result` no run da
    hipotese, e o B1 casado roda no **proprio run** desde o incremento 3 -
    historias economicas independentes. Nao existe ligacao entre os dois runs.

    Consertar exige ligar o run do B1 ao run que ele casa, e isso e trabalho do
    13, onde cada braco precisa do seu proprio B1 casado. Se este teste
    comecar a falhar, e porque alguem consertou - e ai ele deve ser trocado
    pela asercao contraria, nao apagado.
    """
    from app.validador import promocao

    run_id = resultado.hipoteses[0].run_id
    assert promocao._b1_do_run(conn, run_id) is None, (
        "o validador passou a ver o B1 casado: troque este teste pela asercao"
        " contraria e reveja `busca.METRICA`"
    )


# ===========================================================================
# O defeito que so B4 podia revelar: o credito ia para o bolso errado
# ===========================================================================


@pytest.fixture
def cenario_testavel(conn: sqlite3.Connection):
    """Janela em que as hipoteses de B4 CHEGAM a ser avaliadas.

    Com 3.000 barras o in-sample tem 900 e o Sharpe minimo testavel passa do
    teto do schema: nada e testavel, `avaliar_in_sample` nunca e alcancado, e
    **nenhum credito e cobrado**. Foi exatamente assim que o defeito de baixo
    ficou invisivel na primeira escrita destes testes.

    20.000 barras dao in-sample de 6.000, e ai o caminho inteiro roda.
    """
    from app.maos_rapidas import baselines

    cfg = ExperimentConfig()
    dataset_id = criar_dataset(conn, precos_passeio(20_000))
    baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        semente=cfg.default_seed,
    )
    return dataset_id, cfg


def test_o_credito_de_b4_sai_do_orcamento_de_b4(
    conn: sqlite3.Connection, cenario_testavel
) -> None:
    """`promocao._avaliar` cobrava `braco="agente"` FIXO, para toda hipotese.

    Enquanto houve um braco so, o defeito era invisivel. Com B4, testar o
    controle drenaria o orcamento do agente - e §14.3 exige "mesmo orcamento
    de creditos de teste" nos dois, o que so significa algo se os dois forem
    contados separado. Sem isto, a comparacao "por credito gasto" nao tem
    denominador por braco.

    O braco vem de `creditos.braco_da_hipotese`, DERIVADO da `agente_origem`
    da linha - nunca parametro do chamador, que poderia cobrar do bolso
    errado. E a mesma porta lateral que o incremento 10 fechou ao exigir que a
    transicao parta do estado lido do banco.
    """
    from app import creditos as creditos_mod

    dataset_id, cfg = cenario_testavel
    resultado = braco.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1
    )
    cobrados = sum(h.creditos_cobrados or 0 for h in resultado.hipoteses)
    assert cobrados > 0, (
        "nenhum credito cobrado: a janela nao chegou a permitir avaliacao, e"
        " este teste passaria por vacuidade"
    )

    de_b4 = creditos_mod.saldo(conn, braco="b4", config_version_id=1)
    assert de_b4.consumido == cobrados

    # E o orcamento do AGENTE nao foi tocado. Esta e a asercao que falhava.
    do_agente = creditos_mod.saldo(conn, braco="agente", config_version_id=1)
    assert do_agente is None or do_agente.consumido == 0, (
        "o teste de uma hipotese de B4 cobrou do orcamento do agente"
    )


def test_o_mapa_de_origem_para_braco_cobre_as_origens_que_existem() -> None:
    """Duas copias de um mapa fechado divergem. Esta e a conferencia.

    `ORIGEM_PARA_BRACO` esta em `app/creditos` com as origens como literais,
    para nao importar `app/hipotese` ali. O preco disso e este teste.
    """
    from app import creditos as creditos_mod

    origens = {
        hipotese_registro.AGENTE_ORIGEM,
        hipotese_registro.AGENTE_ORIGEM_B4,
    }
    assert set(creditos_mod.ORIGEM_PARA_BRACO) == origens
    assert set(creditos_mod.ORIGEM_PARA_BRACO.values()) == set(creditos_mod.BRACOS)


def test_origem_sem_braco_atribuido_e_recusada(
    conn: sqlite3.Connection, cenario_testavel
) -> None:
    """Nao cai no braco do agente por padrao.

    Uma origem nova sem braco cobraria de alguem, e cobrar do bolso errado em
    silencio e pior que recusar: o orcamento e o denominador da comparacao da
    fase.
    """
    from app import creditos as creditos_mod

    dataset_id, cfg = cenario_testavel
    r = braco.rodar(conn, dataset_id=dataset_id, config=cfg, config_version_id=1)
    hid = r.hipoteses[0].hypothesis_id

    # `hypothesis` e imutavel por gatilho, entao a origem estranha entra numa
    # linha nova - o que e como ela apareceria de verdade.
    conn.execute(
        "INSERT INTO hypothesis (run_id, agent_event_id, enunciado,"
        " agente_origem, timestamp_registro, metrica_primaria, efeito_minimo,"
        " n_minimo, sharpe_esperado_milesimos, criterio_parada,"
        " condicoes_validade_json, condicoes_falseamento_json, testavel,"
        " horizonte_barras, content_hash)"
        " SELECT run_id, agent_event_id, enunciado, 'especialidade_nova@1',"
        " timestamp_registro, metrica_primaria, efeito_minimo, n_minimo,"
        " sharpe_esperado_milesimos, criterio_parada, condicoes_validade_json,"
        " condicoes_falseamento_json, testavel, horizonte_barras,"
        " content_hash || 'x' FROM hypothesis WHERE id = ?",
        (hid,),
    )
    nova = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    with pytest.raises(ValueError, match="braco de creditos"):
        creditos_mod.braco_da_hipotese(conn, nova)


def test_b4_recusa_sem_o_b3_sob_a_mesma_config(
    conn: sqlite3.Connection, cenario_sem_baseline
) -> None:
    """A recusa vem ANTES de qualquer credito ser cobrado.

    A metrica primaria de B4 e `excesso_sobre_b3_cents`, e `veredito.observar`
    so aceita B3 produzido sob a MESMA config. Sem ele os dezesseis vereditos
    saem `None` - nao "refutada", nao "inconclusiva", **nada** -, e catorze
    creditos teriam sido gastos para produzir linhas que nao afirmam nada.

    Medido: foi esse exatamente o resultado antes desta recusa existir.
    """
    from app import creditos as creditos_mod

    dataset_id, cfg = cenario_sem_baseline
    with pytest.raises(braco.BaselineAusente, match="B3"):
        braco.rodar(
            conn, dataset_id=dataset_id, config=cfg, config_version_id=1
        )
    saldo = creditos_mod.saldo(conn, braco="b4", config_version_id=1)
    assert saldo is None or saldo.consumido == 0, (
        "a recusa cobrou credito: o check tem de vir antes de conceder e cobrar"
    )


def test_hipotese_que_nunca_operou_nao_recebe_parecer(
    conn: sqlite3.Connection, cenario_testavel
) -> None:
    """A busca aleatoria produz parametro degenerado, e isso e resultado.

    Uma regra que nunca disparou nao tem amostra, e o validador recusa avaliar
    em vez de emitir veredito sobre nada. Nenhum credito e cobrado: cobrar por
    um teste que nao aconteceu inflaria o denominador da comparacao da fase na
    direcao de fazer B4 parecer pior.
    """
    dataset_id, cfg = cenario_testavel
    r = braco.rodar(conn, dataset_id=dataset_id, config=cfg, config_version_id=1)

    sem_operar = [h for h in r.hipoteses if h.idas_e_voltas == 0]
    assert sem_operar, (
        "nenhuma candidata degenerada nesta semente: o teste nao esta"
        " exercitando o caminho que diz exercitar"
    )
    for h in sem_operar:
        assert h.veredito is None
        assert h.creditos_cobrados is None
        assert "amostra" in (h.motivo or "")


# ===========================================================================
# Rotas
# ===========================================================================


def test_b4_exige_token(client) -> None:
    """Sem excecao, como todo endpoint (ADR 0007)."""
    assert client.get("/api/b4", headers={"Authorization": ""}).status_code == 401
    assert client.post(
        "/api/b4", json={"author": "x"}, headers={"Authorization": ""}
    ).status_code == 401


def test_b4_sem_ter_rodado_responde_vazio(client) -> None:
    """Zero hipoteses, e nao 500: nao ter rodado nao e falha.

    A config vem inicializada no boot, entao `existe` e True e o que esta
    vazio e a lista - o que e a resposta certa para "o controle ja rodou?".
    """
    corpo = client.get("/api/b4").json()
    assert corpo["existe"] is True
    assert corpo["quantas"] == 0
    assert corpo["hipoteses"] == []


def test_b4_recusa_pela_rota_sem_separacao(
    client, conn: sqlite3.Connection
) -> None:
    """409 com o texto da excecao, e nao 500.

    A ordem que a rota exige e a mesma do painel: dataset separado, baselines
    rodados, e so entao o braco. Cada recusa e um 409 porque o pedido esta
    correto e o ESTADO nao permite - o que e diferente de pedido invalido.
    """
    resposta = client.post("/api/b4", json={"author": "teste"})
    assert resposta.status_code == 409, resposta.json()
    assert "dataset" in resposta.json()["detail"].lower()


def test_b4_pela_rota_roda_o_braco_inteiro(
    client, conn: sqlite3.Connection, cenario_b4
) -> None:
    """O caminho de ponta a ponta, pela mesma porta que o painel usa."""
    resposta = client.post("/api/b4", json={"author": "teste"})
    assert resposta.status_code == 201, resposta.json()
    corpo = resposta.json()

    assert corpo["braco"] == "b4"
    assert corpo["quantas"] == busca.QUANTAS
    assert corpo["reflexoes"] == 0
    assert len(corpo["digest_das_hipoteses"]) == 64
    assert len(corpo["hipoteses"]) == busca.QUANTAS

    # E o GET passa a ver o que o POST criou - a licao das tres vezes em que
    # um campo existia no POST e faltava no GET.
    estado = client.get("/api/b4").json()
    assert estado["existe"] is True
    assert estado["quantas"] == busca.QUANTAS
    assert estado["creditos"]["orcamento"] == 60
    assert estado["agente_origem"] == hipotese_registro.AGENTE_ORIGEM_B4


def test_b4_recusa_com_run_ativo(client, conn: sqlite3.Connection, cenario_b4) -> None:
    """Mesma trava do agente: run aberto congela a config."""
    from app.ledger.livro import abrir_run

    abrir_run(conn, config_version_id=1, seed_capital_usd_cents=100_000)
    resposta = client.post("/api/b4", json={"author": "teste"})
    assert resposta.status_code == 409
    assert "run ativo" in resposta.json()["detail"]


def test_o_export_traz_o_RESULTADO_de_b4_e_nao_so_os_ids(
    client, conn: sqlite3.Connection, cenario_b4
) -> None:
    """A parte `b4` do export tem de responder o que o braco CONCLUIU.

    A guarda do incremento 11b garante que a rota esta no pacote; ela nao
    garante que a rota diz algo util. `resumo` devolvia dezesseis ids, um
    hash e um contador de creditos - o export mostraria que B4 rodou sem
    dizer o resultado de nada, e a comparacao da fase e sobre resultado.

    Quarta vez que um campo existe no POST e falta no GET. Entra na primeira
    escrita porque `CAMPOS_QUE_JA_SUMIRAM` existe para a pergunta ser feita.
    """
    client.post("/api/b4", json={"author": "teste"})
    corpo = client.get("/api/relatorio/exportar").json()

    b = corpo["partes"]["b4"]
    assert b["quantas"] == busca.QUANTAS
    assert "sustentadas" in b and "sustentadas_por_credito_ppm" in b

    for h in b["hipoteses"]:
        # O que a hipotese AFIRMOU, e nao so o id dela.
        assert h["enunciado"] and h["metrica_primaria"]
        assert h["n_minimo"] and h["efeito_minimo"]
        # O que o validador CONCLUIU, recalculado.
        assert "parecer" in h
        assert h["parecer"] is None or "veredito" in h["parecer"]
        # E o registro datado de que houve teste, com o custo.
        assert isinstance(h["testes"], list)
        assert h["estado"] is not None


def test_o_lote_do_export_diz_de_qual_braco_cada_membro_e(
    client, conn: sqlite3.Connection, cenario_b4
) -> None:
    """Com 48 linhas misturadas, "quantas de cada lado" tem de ser respondivel.

    O lote e onde os dois bracos sao comparados (§14.3, e o criterio "supera
    B4 por credito consumido" de §14.4). Um membro que nao diz de onde veio
    torna a tabela ilegivel exatamente na pergunta que ela existe para
    responder.
    """
    client.post("/api/b4", json={"author": "teste"})
    corpo = client.get("/api/relatorio/exportar").json()

    membros = corpo["partes"]["lote"]["fechamento"]["membros"]
    assert membros, "o lote veio vazio: o teste passaria por vacuidade"
    assert all(m["agente_origem"] for m in membros)
    assert {m["agente_origem"] for m in membros} == {
        hipotese_registro.AGENTE_ORIGEM_B4
    }, "so B4 rodou neste cenario; o agente entraria como origem propria"


def test_o_json_do_export_e_legivel_por_uma_pessoa(client) -> None:
    """Indentado, e nao compacto.

    `JSONResponse` serializa com `separators=(",", ":")` - certo para resposta
    de API, errado para isto: o export existe para uma pessoa abrir num editor.
    Trezentos kB numa linha unica nao sao legiveis em editor nenhum, e o
    arquivo era justamente a forma de tirar o estado da tela **sem perder
    estrutura**.
    """
    resposta = client.get("/api/relatorio/exportar")
    assert resposta.status_code == 200
    texto = resposta.text

    assert "\n" in texto, "o export saiu numa linha unica"
    assert '\n  "fase"' in texto, "sem indentacao de dois espacos"
    assert texto.count("\n") > 100, (
        f"so {texto.count(chr(10))} linhas: a indentacao nao chegou ao fundo"
    )
    # E continua sendo JSON valido, com os acentos como caracteres e nao como
    # escapes - o texto e para ler.
    import json as _json

    _json.loads(texto)
    # O simbolo de secao aparece como CARACTERE, e nao como escape: e o que
    # `ensure_ascii=False` garante, e "\u00a714.3" nao se le.
    assert chr(0xA7) in texto, "os simbolos sairam escapados"
    assert "u00a7" not in texto


def test_o_post_de_b4_traz_uma_linha_de_resumo(
    client, conn: sqlite3.Connection, cenario_b4
) -> None:
    """`mensagem`, porque o corpo inteiro nao cabe na URL do painel.

    O painel passa a resposta do POST pela URL do redirect, cortada em 4.000
    caracteres. O corpo de B4 tem ~8.000: chegava truncado, nao parseavel, e a
    caixa mostrava um blob numa linha so. Aumentar o corte nao resolve -
    `encodeURIComponent` triplica JSON e o header de um redirect nao aguenta.

    Quem resume e a API. Escolher quais campos importam e decidir sobre o
    experimento, e isso nao acontece no painel (regra 19).
    """
    import json as _json

    corpo = client.post("/api/b4", json={"author": "teste"}).json()
    assert corpo["mensagem"]
    assert len(corpo["mensagem"]) < 200, "a linha de resumo tem de caber na tela"
    assert "zero reflexoes" in corpo["mensagem"]
    assert str(busca.QUANTAS) in corpo["mensagem"]

    # E o corpo inteiro de fato nao cabe - senao este campo seria desnecessario
    # e o teste passaria por vacuidade.
    assert len(_json.dumps(corpo)) > 4_000


# ===========================================================================
# O defeito que o primeiro B4 em producao revelou
# ===========================================================================


def test_a_rota_do_agente_nunca_mostra_um_run_de_b4(
    client, conn: sqlite3.Connection, cenario_b4, settings  # noqa: F811
) -> None:
    """O run 52 apareceu em `/api/agente` com o pre-registro de B4.

    `agente_estado` lia `MAX(run_id) FROM agent_event`, e ate o incremento 12
    isso bastava: so o cerebro emitia evento. B4 tambem emite - evento nao
    cognitivo, provedor nulo -, e a rota passou a mostrar o run do CONTROLE
    como se fosse o do agente: pre-registro dele, atribuicao dele, caminho
    dele.

    Pior: `braco.AGENT_ID = "b4-0001"` foi escrito COM UM COMENTARIO dizendo
    que impedia exatamente isso. Nao impedia - o filtro era sobre
    `agent_event`, e nao sobre o dono do run. Comentario afirmando protecao
    que nao existe e o padrao que este projeto ja registrou treze vezes, e
    esta fui eu quem escreveu, no mesmo incremento.
    """
    from app.cerebro import ciclo
    from tests.test_cerebro import (
        INTERPRETACAO_OK,
        PROPOSTA_OK,
        AdaptadorFalso,
    )

    dataset_id, cfg = cenario_b4

    # Primeiro o agente, DEPOIS o B4 - a ordem em que o defeito aparece.
    do_agente = ciclo.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        settings=settings, adaptador=AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK]),
    )
    resultado_b4 = braco.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1
    )
    runs_de_b4 = {h.run_id for h in resultado_b4.hipoteses}
    assert max(runs_de_b4) > do_agente.run_id, (
        "os runs de B4 tem de ser POSTERIORES, senao o teste nao exercita o"
        " caso em que o `MAX` os alcanca"
    )

    corpo = client.get("/api/agente").json()
    assert corpo["run_id"] == do_agente.run_id
    assert corpo["run_id"] not in runs_de_b4
    assert corpo["reflexoes"] > 0, "um run do agente tem reflexao"
    assert corpo["atribuicao"]["atribuivel_ao_agente"] is True
    assert "NAO COGNITIVO" not in (corpo["pre_registro"] or {}).get("enunciado", "")


def test_a_rota_do_agente_filtra_por_dono_do_run_e_nao_exclui_b4(
    conn: sqlite3.Connection,
) -> None:
    """Filtro POSITIVO: um terceiro braco nao volta a aparecer por esquecimento.

    Excluir `b4-0001` resolveria hoje e falharia calado no dia em que outro
    dono de run emitisse evento - que e como este defeito nasceu.
    """
    fonte = (APP / "api" / "rotas" / "agente.py").read_text(encoding="utf-8")
    assert "AGENT_ID_PADRAO" in fonte

    # A asercao e sobre o SQL, e nao sobre o arquivo: o comentario ACIMA da
    # consulta cita `b4-0001` para contar como o defeito apareceu, e proibir a
    # string no arquivo inteiro empurraria para apagar a explicacao - o mesmo
    # engano que a guarda de separacao do incremento 11 cometeu com as
    # docstrings.
    sql = fonte[fonte.index("SELECT MAX(e.run_id)"):]
    sql = sql[: sql.index(".fetchone()")]
    assert "r.agent_id = ?" in sql
    for negacao in ("agent_id <>", "agent_id !=", "agent_id NOT"):
        assert negacao not in sql, (
            f"o filtro exclui por nome ({negacao}); ele tem de INCLUIR o dono"
            " esperado, senao um braco novo volta a aparecer por esquecimento"
        )


def test_o_painel_acusa_quando_o_b1_casado_nao_casa(
    client, conn: sqlite3.Connection, cenario_b4, settings  # noqa: F811
) -> None:
    """A tela mostrou 37 idas e voltas ao lado de um controle de 70.

    `b1_do_agente` devolve o ultimo B1 casado gravado, globalmente: nao ha
    ligacao entre o run do B1 e o run que ele casa. A D19 existe exatamente
    para impedir comparar giros diferentes, e o defeito aparecia como uma
    tabela plausivel.

    Enquanto a ligacao nao existir (incremento 13), o campo DIZ quando nao
    casa - em vez de a tabela mentir.
    """
    from app.cerebro import ciclo
    from tests.test_cerebro import (
        INTERPRETACAO_OK,
        PROPOSTA_OK,
        AdaptadorFalso,
    )

    dataset_id, cfg = cenario_b4
    ciclo.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        settings=settings, adaptador=AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK]),
    )
    corpo = client.get("/api/agente").json()
    confere = corpo["b1_casado_confere"]
    assert confere is not None
    # No caminho normal ele CASA - o ciclo produz o B1 junto.
    assert confere["casa"] is True
    assert confere["operacoes_alvo"] == corpo["idas_e_voltas"]
    assert "D19" in confere["por_que_importa"]
