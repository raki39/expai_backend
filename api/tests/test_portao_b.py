"""Incremento 14 — Portão B e a auditoria que ele dispara (§14.4, §14.4.1).

> "O Portão B **não aprova um edge**. Ele avalia apenas evidência
> retrospectiva, em fidelidade 1–2, e por isso decide somente se existe uma
> estratégia candidata que mereça seguir para auditoria." — §14.4

O teste mais importante deste arquivo é o primeiro: **B não é calculado se A
não passou**. Não é convenção — é a diferença entre um portão e um rótulo.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.config.schema import ExperimentConfig
from tests.test_cerebro import settings  # noqa: F401


@pytest.fixture
def cenario_com_agente(conn: sqlite3.Connection, settings):  # noqa: F811
    """Dataset dividido, baselines, e um run do agente com hipótese.

    Janela de 20.000 barras: com 3.000 nada é testável e a hipótese nasce
    arquivada, então o Portão B não teria candidata — e o teste passaria por
    ausência em vez de por avaliação.
    """
    from app.cerebro import ciclo
    from app.maos_rapidas import baselines
    from tests.test_cerebro import (
        INTERPRETACAO_OK,
        PROPOSTA_OK,
        AdaptadorFalso,
    )
    from tests.test_maos_rapidas import criar_dataset, precos_passeio

    cfg = ExperimentConfig()
    dataset_id = criar_dataset(conn, precos_passeio(20_000))
    baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        semente=cfg.default_seed,
    )
    resultado = ciclo.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        settings=settings,
        adaptador=AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK]),
    )
    return dataset_id, cfg, resultado


def _b(conn, dataset_id, cfg, **kw):
    from app.relatorio import portao_b

    return portao_b.montar(
        conn, config_version_id=1, config=cfg, dataset_id=dataset_id, **kw
    )


# ===========================================================================
# CRITÉRIO 1 — B não é calculado se A não passou
# ===========================================================================


def test_sem_o_portao_A_o_B_nao_produz_numero_nenhum(
    conn: sqlite3.Connection, cenario_com_agente
) -> None:
    """R49, e a forma da recusa importa tanto quanto a recusa.

    O retorno **não contém critério nenhum** — nem parcial, nem "só para ver".
    Um número exibido é um número que alguém lê, e a fase inteira existe para
    não produzir este cedo demais.

    Neste cenário o A está pendente: A1b não rodou.
    """
    dataset_id, cfg, _ = cenario_com_agente
    b = _b(conn, dataset_id, cfg)
    assert b["avaliado"] is False
    assert "R49" in b["por_que"]
    assert "candidatas" not in b
    assert "criterios" not in b
    # E ele diz POR QUE o A não passou, com os nomes das condições.
    assert b["portao_a"]["passa"] is False
    assert b["portao_a"]["pendentes"]


def test_o_que_aprovar_nao_significa_aparece_mesmo_sem_avaliacao(
    conn: sqlite3.Connection, cenario_com_agente
) -> None:
    """§14.4.1 não depende de o portão ter sido calculado.

    A lista é fixa de propósito: derivar dos dados permitiria que ela
    encolhesse sozinha no dia em que algo passasse.
    """
    dataset_id, cfg, _ = cenario_com_agente
    b = _b(conn, dataset_id, cfg)
    texto = " ".join(b["o_que_aprovar_nao_significa"])
    assert "SUSPEITA DE DEFEITO" in texto
    assert "capital real" in texto
    assert "0C" in texto


# ===========================================================================
# Com o Portão A aprovado
# ===========================================================================


@pytest.fixture
def com_a_aprovado(conn: sqlite3.Connection, cenario_com_agente, monkeypatch):
    """Aprova o Portão A **no relatório**, e não no banco.

    O que se quer testar aqui é o comportamento do B quando o A passa. Forjar
    a aprovação no banco exigiria plantar 400 execuções de A1b — e o que
    estaria sendo exercitado seria a fixture, não o portão.

    O `monkeypatch` é sobre a fronteira entre os dois módulos, que é
    exatamente o que o critério 1 protege: `portao_b` pergunta ao `portao_a`, e
    aceita a resposta dele.
    """
    from app.relatorio import portao_a, portao_b

    real = portao_a.montar

    def aprovado(*a, **kw):
        return {**real(*a, **kw), "passa": True, "reprova": False,
                "pendente": False, "reprovando": [], "pendentes": []}

    monkeypatch.setattr(portao_b.portao_a_mod, "montar", aprovado)
    return cenario_com_agente


def test_os_seis_criterios_saem_de_consulta_e_None_nao_e_False(
    conn: sqlite3.Connection, com_a_aprovado
) -> None:
    """Cada critério é booleano ou `None`, e `None` tem motivo ao lado."""
    dataset_id, cfg, _ = com_a_aprovado
    b = _b(conn, dataset_id, cfg)
    assert b["avaliado"] is True
    assert b["quantas"] >= 1
    c = b["candidatas"][0]["criterios"]
    assert len(c) == 6
    for nome, valor in c.items():
        assert valor is None or isinstance(valor, bool), (nome, valor)
    assert c["b5_walk_forward_em_3_janelas"] is None
    assert b["candidatas"][0]["walk_forward"]["por_que"]


def test_a_leitura_nao_roda_o_walk_forward(
    conn: sqlite3.Connection, com_a_aprovado
) -> None:
    """O walk-forward **escreve**, e uma rota de leitura não pode escrever.

    Sem esta separação, cada carregamento do painel abriria runs — e o
    registro é append-only, então eles ficariam.
    """
    from app.validador import forward

    dataset_id, cfg, _ = com_a_aprovado
    antes = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM run WHERE agent_id = ?",
            (forward.AGENT_ID,),
        ).fetchone()["n"]
    )
    _b(conn, dataset_id, cfg, rodar_forward=False)
    depois = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM run WHERE agent_id = ?",
            (forward.AGENT_ID,),
        ).fetchone()["n"]
    )
    assert depois == antes == 0


def test_criterio_reprovado_impede_o_walk_forward_e_diz_por_que(
    conn: sqlite3.Connection, com_a_aprovado
) -> None:
    """`None` por "não foi medido" é diferente de `None` por falta de amostra.

    Rodar o walk-forward de uma candidata que já reprovou mudaria o custo e não
    a resposta — e deixaria o critério 5 com um `None` que sugere amostra
    insuficiente.
    """
    dataset_id, cfg, _ = com_a_aprovado
    b = _b(conn, dataset_id, cfg, rodar_forward=True)
    c = b["candidatas"][0]
    if c["reprovando"]:
        assert c["criterios"]["b5_walk_forward_em_3_janelas"] is None
        assert c["walk_forward"]["executado"] is False
        assert "ja reprovou" in c["walk_forward"]["por_que"]
        assert c["resultado"] == "rejeitado"


def test_o_resultado_tem_TRES_valores_e_inconclusivo_nao_e_sucesso(
    conn: sqlite3.Connection, com_a_aprovado
) -> None:
    """R51, e §14.4 escreve a consequência: não pode ser citado como sucesso."""
    from app.relatorio import portao_b

    dataset_id, cfg, _ = com_a_aprovado
    b = _b(conn, dataset_id, cfg)
    for c in b["candidatas"]:
        assert c["resultado"] in (
            portao_b.PASSOU, portao_b.REJEITADO, portao_b.INCONCLUSIVO
        )
        # Inconclusivo nunca entra em `passaram`.
        if c["resultado"] != portao_b.PASSOU:
            assert c["hypothesis_id"] not in b["passaram"]
    assert b["ha_candidata_digna_de_auditoria"] == bool(b["passaram"])


# ===========================================================================
# CRITÉRIO 2 (B5) — o walk-forward, executado pelo VALIDADOR
# ===========================================================================


def test_o_walk_forward_roda_nas_tres_janelas_com_baseline_da_janela(
    conn: sqlite3.Connection, cenario_com_agente
) -> None:
    """"Supera o B3" numa janela de teste é superar o B3 **daquela janela**.

    Usar o B3 da comparação — que rodou sobre o in-sample — compararia o
    desempenho de um período contra o de outro, e a diferença entre os
    períodos entraria no número como se fosse mérito da regra.
    """
    from app.dataset import janelas as janelas_mod
    from app.validador import forward

    dataset_id, cfg, resultado = cenario_com_agente
    assert resultado.hypothesis_id is not None
    # As janelas precisam existir: sem elas B5 não é computável.
    if not janelas_mod.ler(conn, dataset_id):
        janelas_mod.gerar(conn, dataset_id)

    r = forward.rodar(
        conn, hypothesis_id=resultado.hypothesis_id, dataset_id=dataset_id,
        config=cfg, config_version_id=1,
    )
    assert len(r.janelas) == 3
    for j in r.janelas:
        if j.run_id == 0:
            continue
        # O baseline é DA JANELA, e o `agent_id` diz isso.
        for nome, run_b in j.baselines.items():
            dono = conn.execute(
                "SELECT agent_id FROM run WHERE id = ?", (run_b,)
            ).fetchone()["agent_id"]
            assert dono == f"baseline-{nome}-wf{j.ordem}"
            # E nunca `baseline-B3`, que é o da comparação: `observar` procura
            # aquele nome exato, e um baseline de janela que atendesse por ele
            # seria encontrado pela busca global.
            assert dono not in ("baseline-B2", "baseline-B3")


def test_o_walk_forward_le_pelo_caminho_do_VALIDADOR(
    conn: sqlite3.Connection, cenario_com_agente
) -> None:
    """O agente não alcança estas barras, e a diferença é do SQL.

    Guarda de código: `forward.py` lê de `selado`, e não de
    `executor.carregar_janela` — que só devolve `in_sample`. Se alguém trocasse
    a fonte, o walk-forward passaria a testar sobre a mesma janela do treino e
    o número continuaria saindo.
    """
    import pathlib

    fonte = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app" / "validador" / "forward.py"
    ).read_text(encoding="utf-8")
    assert "selado.walk_forward(" in fonte
    assert "executor.carregar_janela(" not in fonte


def test_a_regra_do_walk_forward_e_a_da_HIPOTESE(
    conn: sqlite3.Connection, cenario_com_agente
) -> None:
    """Escolher a regra aqui seria ajustá-la ao período de teste.

    É o sobreajuste que a separação existe para impedir — e ele entraria pela
    porta de quem executa o teste, não pela do agente.
    """
    from app.dataset import janelas as janelas_mod
    from app.hipotese import registro as hipotese_registro
    from app.validador import forward

    dataset_id, cfg, resultado = cenario_com_agente
    if not janelas_mod.ler(conn, dataset_id):
        janelas_mod.gerar(conn, dataset_id)

    hip = hipotese_registro.por_id(conn, resultado.hypothesis_id)
    r = forward.rodar(
        conn, hypothesis_id=resultado.hypothesis_id, dataset_id=dataset_id,
        config=cfg, config_version_id=1,
    )
    assert r.rule_id == hip["rule_id"]


# ===========================================================================
# CRITÉRIO 5 — o roteiro de §14.4.1 é executável, não texto
# ===========================================================================


def test_as_cinco_mais_lucrativas_medem_CONCENTRACAO(
    conn: sqlite3.Connection, cenario_com_agente
) -> None:
    """"Resultado concentrado em poucas operações é o padrão típico de bug".

    O número que interessa não é a lista: é a fração do ganho bruto que veio
    das cinco maiores.
    """
    from app.relatorio import auditoria

    _, _, resultado = cenario_com_agente
    bloco = auditoria.cinco_mais_lucrativas(conn, resultado.run_id)
    assert len(bloco["operacoes"]) <= auditoria.QUANTAS_OPERACOES
    assert bloco["ganho_das_cinco_cents"] <= bloco["ganho_bruto_total_cents"]
    if bloco["ganho_bruto_total_cents"]:
        assert 0 < bloco["concentracao_ppm"] <= 1_000_000
    # E o módulo diz que NÃO substitui a revisão manual.
    assert "manual" in bloco["a_revisao_continua_manual"].lower()


def test_a_auditoria_de_leitura_nao_abre_run_nenhum(
    conn: sqlite3.Connection, cenario_com_agente
) -> None:
    """Dois dos quatro itens de §14.4.1 escrevem. A leitura não pode."""
    from app.relatorio import auditoria

    dataset_id, cfg, resultado = cenario_com_agente
    antes = int(conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"])
    saida = auditoria.montar(
        conn, hypothesis_id=resultado.hypothesis_id, dataset_id=dataset_id,
        config=cfg, config_version_id=1, executar=False,
    )
    depois = int(conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"])
    assert depois == antes
    assert saida["reexecucao"]["executada"] is False
    assert "append-only" in saida["reexecucao"]["por_que"]


def test_dobrar_custo_spread_e_slippage_piora_o_resultado(
    conn: sqlite3.Connection, cenario_com_agente
) -> None:
    """Se o resultado não piorar ao dobrar as três, elas não estavam sendo aplicadas.

    Este teste é a prova de que a reexecução de §14.4.1 mede alguma coisa: um
    bloco que devolvesse o mesmo número sob premissas dobradas estaria
    reexecutando sem trocar nada.
    """
    from app.relatorio import auditoria

    dataset_id, cfg, resultado = cenario_com_agente
    bloco = auditoria.sobrevive_ao_custo_dobrado(
        conn, hypothesis_id=resultado.hypothesis_id, dataset_id=dataset_id,
        config=cfg, config_version_id=1,
    )
    assert bloco["patrimonio_dobrado_cents"] < bloco["patrimonio_original_cents"]
    assert isinstance(bloco["sobrevive"], bool)


def test_a_semente_alterada_move_o_CONTROLE_e_nao_a_regra(
    conn: sqlite3.Connection, cenario_com_agente
) -> None:
    """A regra é determinística: reexecutá-la com outra semente é `f(x) = f(x)`.

    §14.4.1 pede "reexecutar com a semente alterada", e a leitura ingênua
    produziria um bloco que diz "sobreviveu" sempre. O que a semente move é o
    B1 casado, que sorteia os pares — e a pergunta que importa é se o resultado
    fica no mesmo lugar de uma distribuição sorteada de outro jeito.
    """
    from app.relatorio import auditoria

    dataset_id, cfg, resultado = cenario_com_agente
    bloco = auditoria.com_semente_alterada(
        conn, hypothesis_id=resultado.hypothesis_id, dataset_id=dataset_id,
        config=cfg, config_version_id=1, semente=cfg.default_seed + 7,
    )
    if not bloco["executada"]:
        pytest.skip(bloco["por_que"])
    assert bloco["semente_alternativa"] != bloco["semente_original"]
    # Duas distribuições diferentes, sorteadas de sementes diferentes.
    assert bloco["p50_alternativo_cents"] is not None
    assert "deterministica" in bloco["o_que_a_semente_move"]


def test_nada_vive_fora_do_modo_pessimista(
    conn: sqlite3.Connection, cenario_com_agente
) -> None:
    """As três perguntas do item 4, sobre o que ficou gravado."""
    from app.relatorio import auditoria

    _, cfg, resultado = cenario_com_agente
    bloco = auditoria.nao_depende_do_proibido(conn, resultado.run_id, cfg)
    assert bloco["ok"] is True
    assert bloco["execucoes_generosas"] == 0
    assert bloco["execucoes_sem_latencia"] == 0
    # E o módulo declara o que NÃO cobre, em vez de sugerir que cobre tudo.
    limite = bloco["limite"].lower()
    assert "volume" in limite and "horario" in limite


def test_o_periodo_reservado_NAO_e_trocado_e_isso_esta_declarado(
    conn: sqlite3.Connection, cenario_com_agente
) -> None:
    """Divergência de §14.4.1, levantada em vez de resolvida em silêncio.

    O documento pede "reexecutar com a semente alterada **e com o período
    reservado trocado**". Trocar a reserva consumiria o holdout, que tem uso
    único por hipótese (§8.5.1) — a auditoria aconteceria às custas do teste
    final que ela existe para proteger, e um holdout consumido não se recupera.
    """
    from app.relatorio import auditoria

    dataset_id, cfg, resultado = cenario_com_agente
    saida = auditoria.montar(
        conn, hypothesis_id=resultado.hypothesis_id, dataset_id=dataset_id,
        config=cfg, config_version_id=1, executar=True,
    )
    r = saida["reexecucao"]
    assert r["periodo_reservado_trocado"] is False
    assert "USO" in r["por_que_a_reserva_nao_e_trocada"]
    assert "8.5.1" in r["por_que_a_reserva_nao_e_trocada"]


def test_empate_por_credito_nao_e_o_mesmo_que_perder(
    conn: sqlite3.Connection, com_a_aprovado
) -> None:
    """O criterio 4 sai `false` nos dois casos, e eles levam a conclusoes opostas.

    §14.3 escreve o que o empate significa: *"se B4 empatar com o agente, isso
    nao mata o projeto - significa que o valor esta na infraestrutura de
    validacao, e a conclusao correta e remover o LLM do laco de geracao e
    reduzir o custo por agente em cerca de 85%"*.

    E o caso `0 vs 0` e mais forte ainda: nenhum braco produziu sobrevivente,
    entao a razao nao mediu qualidade de hipotese nenhuma. Dizer so "nao
    supera" a partir disso e verdade que nao informa - foi exatamente o que a
    tela mostrou no primeiro Portao B em producao.
    """
    dataset_id, cfg, _ = com_a_aprovado
    b = _b(conn, dataset_id, cfg)
    pc = b["por_credito"]
    if pc["agente_supera_b4"] is False and pc["empate"]:
        assert pc["o_que_o_empate_significa"]
        assert "14.3" in pc["o_que_o_empate_significa"]
        if pc["ambos_zerados"]:
            assert "nada passou" in pc["o_que_o_empate_significa"]


def test_a_resposta_da_fase_e_derivada_dos_dois_portoes(
    conn: sqlite3.Connection, cenario_com_agente
) -> None:
    """Criterio 8 do incremento 14: o relatorio declara o que a fase NAO respondeu.

    E a resposta em si e derivada, nunca digitada: ela e a conjuncao dos dois
    portoes, e cada um ja e a conjuncao de condicoes que saem de consulta.

    Com o A pendente, a fase nao respondeu nada - e o texto diz isso em vez de
    ficar em silencio.
    """
    dataset_id, cfg, _ = cenario_com_agente
    r = _b(conn, dataset_id, cfg)["resposta_da_0b"]
    assert r["portao_a_passou"] is False
    assert r["portao_b_avaliado"] is False
    assert "eliminatorio" in r["o_que_a_fase_respondeu"]
    assert r["e_o_que_isso_significa"] is None


def test_falhar_no_B_depois_de_passar_no_A_e_chamado_pelo_nome(
    conn: sqlite3.Connection, com_a_aprovado
) -> None:
    """§19.4: passar no A e falhar no B e "o cenario mais provavel, e
    perfeitamente aceitavel".

    Um relatorio que tratasse isso como fracasso estaria contrariando o
    documento no ponto em que ele e mais explicito - e este e o desfecho que a
    0B de fato teve.
    """
    dataset_id, cfg, _ = com_a_aprovado
    r = _b(conn, dataset_id, cfg)["resposta_da_0b"]
    assert r["portao_a_passou"] is True
    assert r["portao_b_avaliado"] is True
    if not r["ha_candidata"]:
        assert "19.4" in r["e_o_que_isso_significa"]
        assert "perfeitamente aceitavel" in r["e_o_que_isso_significa"]
        assert "protocolo que funciona" in r["e_o_que_isso_significa"]


def test_a_fase_declara_o_limite_do_DSR_em_vez_de_esconde_lo(
    conn: sqlite3.Connection, cenario_com_agente
) -> None:
    """As duas partes discordam sobre o que e "a amostra", e isso fica escrito.

    O veredito usa `n_efetivo` descontado por autocorrelacao (§8.3); o DSR usa
    o `n` bruto, que e o da formula publicada. A discordancia corre para o lado
    de APROVAR - o unico lugar deste projeto onde uma escolha erra nessa
    direcao - e por isso ela e declarada em vez de corrigida em silencio.
    """
    from app.relatorio import portao_b

    dataset_id, cfg, _ = cenario_com_agente
    r = _b(conn, dataset_id, cfg)["resposta_da_0b"]
    assert portao_b.LIMITE_DO_DSR in r["nao_respondeu"]
    assert "APROVAR" in portao_b.LIMITE_DO_DSR
    assert "publicado" in portao_b.LIMITE_DO_DSR
