"""Testes do incremento 7: o fechamento da 0A.

O que este arquivo tem de provar nao e que o relatorio *imprime* as coisas
certas - e que cada afirmacao dele **vem do banco**. Um relatorio de
fechamento que repete prosa escrita a mao seria a forma mais elegante de
enganar o proprio autor: ele diria "o ciclo fecha" com a mesma confianca
tivesse fechado ou nao.

Por isso quase todo teste daqui monta um run de verdade, apaga a prosa da
equacao e compara numero com numero.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.cerebro import avaliacao, ciclo
from app.config.schema import ExperimentConfig
from app.ledger.livro import carteira
from app.maos_rapidas import baselines
from app.relatorio import montar, reprodutibilidade, texto, vinculo
from tests.test_cerebro import (
    INTERPRETACAO_OK,
    PROPOSTA_OK,
    AdaptadorFalso,
    _rodar_ciclo,
    cenario,  # noqa: F401
    settings,  # noqa: F401
)


@pytest.fixture
def run_do_agente(conn: sqlite3.Connection, cenario, settings):  # noqa: F811
    """Um run completo com cerebro, execucoes e avaliacao."""
    return _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )


# ===========================================================================
# CRITERIO 6 - reconstrucao do caminho e vinculo nos DOIS sentidos (R25.1, R25.2)
# ===========================================================================


def test_de_uma_execucao_qualquer_se_chega_ao_evento_cognitivo(
    conn: sqlite3.Connection, run_do_agente
) -> None:
    """A consulta que a R25.2 nomeia como prova, no sentido reverso."""
    execucoes = [
        int(l["id"])
        for l in conn.execute(
            "SELECT id FROM execution WHERE run_id = ? ORDER BY id",
            (run_do_agente.run_id,),
        )
    ]
    assert execucoes, "o run precisa ter executado algo"

    # QUALQUER uma, nao a primeira: um vinculo que so funciona para a
    # primeira execucao nao e vinculo, e coincidencia de ordenacao.
    for execution_id in (execucoes[0], execucoes[len(execucoes) // 2], execucoes[-1]):
        volta = vinculo.da_execucao_ao_evento(conn, execution_id)
        assert volta["existe"]
        assert volta["autorizada_por"] is not None
        assert volta["motivo"] is None
        # A cadeia sobe ate o primeiro no do grafo.
        nos = [e["node"] for e in volta["cadeia_cognitiva"]]
        assert nos[-1] == "observar"
        assert "propor_regra" in nos
        # E a regra que autorizou e a mesma que a execucao aponta.
        assert volta["regra"]["id"] == volta["execucao"]["rule_id"]


def test_do_evento_se_chega_ao_custo_a_regra_e_as_execucoes(
    conn: sqlite3.Connection, run_do_agente
) -> None:
    """Sentido direto: decisao -> custo -> regra -> execucoes -> resultado."""
    alguma = conn.execute(
        "SELECT id FROM execution WHERE run_id = ? LIMIT 1", (run_do_agente.run_id,)
    ).fetchone()
    evento = vinculo.da_execucao_ao_evento(conn, int(alguma["id"]))["autorizada_por"]

    ida = vinculo.do_evento_ao_resultado(conn, int(evento))
    assert ida["existe"]
    assert ida["proposta"]["status"] == "aceita"
    assert ida["regra"]["regra_hash"] == run_do_agente.regra_hash
    assert ida["execucoes"]["quantas"] == run_do_agente.execucao["execucoes"]
    # O custo lido do LEDGER, e nao do campo do evento.
    assert ida["custo"]["custo_simulado_minor"] > 0


def test_a_ida_e_a_volta_fecham_no_mesmo_ponto(
    conn: sqlite3.Connection, run_do_agente
) -> None:
    """Um vinculo que funciona num sentido so passaria nos dois testes acima."""
    conferido = vinculo.conferir_ida_e_volta(conn, run_do_agente.run_id)
    assert conferido["conferido"] is True, conferido
    assert conferido["regra_hash"] == run_do_agente.regra_hash
    assert conferido["profundidade_da_cadeia"] >= 3


def test_regra_padrao_diz_que_nao_houve_evento_em_vez_de_lista_vazia(
    conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """D23: com o teto em zero nao ha decisao, e isso e resposta, nao lacuna.

    Uma lista vazia aqui seria lida como vinculo quebrado. O motivo escrito e
    a diferenca entre "nao existe" e "nao encontrei".
    """
    dataset_id, cfg = cenario
    resultado = ciclo.rodar(
        conn,
        dataset_id=dataset_id,
        config=cfg.model_copy(update={"max_llm_calls_per_run": 0}),
        config_version_id=1,
        settings=settings,
        adaptador=AdaptadorFalso([]),
    )
    assert resultado.reflexoes == 0
    alguma = conn.execute(
        "SELECT id FROM execution WHERE run_id = ? LIMIT 1", (resultado.run_id,)
    ).fetchone()
    volta = vinculo.da_execucao_ao_evento(conn, int(alguma["id"]))
    assert volta["autorizada_por"] is None
    assert "regra padrao" in volta["motivo"]
    assert "D23" in volta["motivo"]


def test_execucao_inexistente_responde_em_vez_de_estourar(
    conn: sqlite3.Connection
) -> None:
    fora = vinculo.da_execucao_ao_evento(conn, 999_999)
    assert fora["existe"] is False and "nao existe" in fora["motivo"]


# ===========================================================================
# CRITERIO 7 - a avaliacao e evento NOVO, filho da decisao (R25.3)
# ===========================================================================


def test_avaliacao_e_filha_da_decisao_e_a_decisao_fica_byte_a_byte_igual(
    conn: sqlite3.Connection, run_do_agente
) -> None:
    """A metade que importa da regra 17: nada do passado e reescrito."""
    run_id = run_do_agente.run_id
    decisao_id = avaliacao.evento_da_decisao(conn, run_id)
    assert decisao_id is not None

    antes = dict(
        conn.execute("SELECT * FROM agent_event WHERE id = ?", (decisao_id,)).fetchone()
    )
    assert antes["expectation"], "a expectativa e declarada ANTES da execucao"
    assert antes["confidence_ppm"] is not None, "e a confianca junto com ela"

    filho = conn.execute(
        "SELECT * FROM agent_event WHERE parent_event_id = ? AND kind = 'avaliacao'",
        (decisao_id,),
    ).fetchone()
    assert filho is not None
    assert int(filho["id"]) == run_do_agente.avaliacao_event_id
    assert filho["evaluation_json"]

    # Byte a byte: a linha da decisao nao mudou em coluna nenhuma.
    depois = dict(
        conn.execute("SELECT * FROM agent_event WHERE id = ?", (decisao_id,)).fetchone()
    )
    assert depois == antes


def test_o_banco_recusa_editar_a_decisao_para_registrar_a_avaliacao(
    conn: sqlite3.Connection, run_do_agente
) -> None:
    """A alternativa errada tem de ser impossivel, nao apenas evitada.

    SQL cru de proposito: se a proibicao dependesse de o modulo Python ter
    sido usado, um defeito nele mascararia a ausencia da regra no banco.
    """
    decisao_id = avaliacao.evento_da_decisao(conn, run_do_agente.run_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE agent_event SET expectation = 'na verdade eu esperava outra coisa'"
            " WHERE id = ?",
            (decisao_id,),
        )


def test_avaliacao_sem_pai_ou_sem_payload_e_recusada_pelo_banco(
    conn: sqlite3.Connection, run_do_agente
) -> None:
    """As duas metades da invariante da migracao 8, impostas por gatilho."""
    run_id = run_do_agente.run_id
    pai = avaliacao.evento_da_decisao(conn, run_id)

    # Sem pai: avaliacao de nada.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO agent_event (run_id, occurred_at, node, kind,"
            " evaluation_json) VALUES (?, datetime('now'), 'avaliar_resultado',"
            " 'avaliacao', '{}')",
            (run_id,),
        )
    # Com pai, sem payload: nao registra o que comparou.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO agent_event (run_id, parent_event_id, occurred_at, node,"
            " kind) VALUES (?, ?, datetime('now'), 'avaliar_resultado','avaliacao')",
            (run_id, pai),
        )
    # Payload em evento que nao e avaliacao: campo descrevendo outra coisa.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO agent_event (run_id, occurred_at, node, kind,"
            " evaluation_json) VALUES (?, datetime('now'), 'observar',"
            " 'observacao', '{}')",
            (run_id,),
        )


def test_a_avaliacao_nao_copia_a_expectativa_declarada(
    conn: sqlite3.Connection, run_do_agente
) -> None:
    """Duas gravacoes da mesma afirmacao e a forma como este projeto erra.

    O declarado vive na decisao. A avaliacao chega nele pela ARESTA, que e
    justamente o vinculo que a R25.3 exige que exista.
    """
    lido = avaliacao.do_run(conn, run_do_agente.run_id)
    assert lido is not None
    payload = lido["comparacao"]
    texto = json.dumps(payload, ensure_ascii=False)
    assert lido["decisao"]["expectativa"] not in texto
    assert lido["decisao"]["expectativa"], "e o pai que carrega a expectativa"
    assert lido["decisao"]["confianca_ppm"] is not None


def test_a_avaliacao_nao_inventa_veredito_sobre_texto_livre(
    conn: sqlite3.Connection, run_do_agente
) -> None:
    """`None` com motivo escrito, e nao `False` que ninguem calculou."""
    lido = avaliacao.do_run(conn, run_do_agente.run_id)
    assert lido["comparacao"]["veredito_da_expectativa"] is None
    assert "regra 12" in lido["comparacao"]["por_que_sem_veredito"]


def test_sem_expectativa_declarada_nao_ha_avaliacao(
    conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """Pendurar comparacao em quem nao afirmou nada seria invencao."""
    dataset_id, cfg = cenario
    resultado = ciclo.rodar(
        conn,
        dataset_id=dataset_id,
        config=cfg.model_copy(update={"max_llm_calls_per_run": 0}),
        config_version_id=1,
        settings=settings,
        adaptador=AdaptadorFalso([]),
    )
    assert resultado.avaliacao_event_id is None
    assert avaliacao.do_run(conn, resultado.run_id) is None


@pytest.mark.parametrize(
    "patrimonio, esperada",
    [
        (10, "abaixo_p5"),
        (150, "entre_p5_e_p50"),
        (500, "entre_p50_e_p95"),
        (5_000, "acima_p95"),
    ],
)
def test_as_quatro_faixas_contra_o_acaso(patrimonio: int, esperada: str) -> None:
    """Com tres faixas, "pior que 95% do acaso" recebia a frase do mediano."""
    b1 = {"p5": 100, "p50": 200, "p95": 1_000}
    assert avaliacao.faixa_contra_o_acaso(patrimonio, b1) == esperada


def test_sem_controle_a_faixa_diz_isso_e_nao_finge_uma() -> None:
    assert avaliacao.faixa_contra_o_acaso(500, None) == "sem_controle"


# ===========================================================================
# CRITERIO 2 - prova de reprodutibilidade, tres digests
# ===========================================================================


def test_a_prova_tem_as_duas_metades_e_o_hash_estavel(
    conn: sqlite3.Connection, cenario  # noqa: F811
) -> None:
    """Mesma semente da o mesmo digest; outra semente da outro; hash igual."""
    dataset_id, cfg = cenario
    prova = reprodutibilidade.provar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1
    )
    assert prova["mesma_semente"]["iguais"] is True
    assert prova["mesma_semente"]["digest_a"] == prova["mesma_semente"]["digest_b"]
    assert prova["semente_diferente"]["difere_do_primeiro"] is True
    assert prova["semente_diferente"]["digest"] != prova["mesma_semente"]["digest_a"]
    assert prova["config_hash_igual_nas_tres"] is True
    assert prova["provado"] is True

    # Os TRES digests aparecem, que e o que o criterio pede literalmente.
    tres = {
        prova["mesma_semente"]["digest_a"],
        prova["mesma_semente"]["digest_b"],
        prova["semente_diferente"]["digest"],
    }
    assert len(tres) == 2, "dois iguais e um diferente"


def test_a_prova_e_reconstruida_dos_lancamentos_e_nao_guardada(
    conn: sqlite3.Connection, cenario  # noqa: F811
) -> None:
    """Um resumo gravado a parte seria a segunda fonte de verdade da regra 16."""
    dataset_id, cfg = cenario
    assert reprodutibilidade.ultima_prova(conn) is None  # antes de existir

    rodada = reprodutibilidade.provar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1
    )
    relida = reprodutibilidade.ultima_prova(conn)
    assert relida is not None
    assert relida["reconstruida_dos_lancamentos"] is True
    # Recalculado dos lancamentos, chega no mesmo digest da primeira vez.
    assert relida["mesma_semente"]["digest_a"] == rodada["mesma_semente"]["digest_a"]
    assert relida["semente_diferente"]["digest"] == rodada["semente_diferente"]["digest"]
    assert relida["provado"] is True


def test_cada_passada_da_prova_tem_carteira_propria(
    conn: sqlite3.Connection, cenario  # noqa: F811
) -> None:
    """Passadas dividindo caixa dariam digests diferentes por interferencia.

    Regressao do defeito que o incremento 3 revelou no 2: contas globais em
    vez de por run. Aqui ele apareceria como "a reprodutibilidade falhou".
    """
    dataset_id, cfg = cenario
    prova = reprodutibilidade.provar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1
    )
    a, b = prova["mesma_semente"]["runs"]
    caixa_a = carteira(conn, run_id=a)["simulado_usd"]["caixa_minor"]
    caixa_b = carteira(conn, run_id=b)["simulado_usd"]["caixa_minor"]
    assert caixa_a == caixa_b, "mesma semente, mesmo caixa - e por run"


# ===========================================================================
# CRITERIO 1 e 3 - o relatorio afirma com dados do banco; integridade contabil
# ===========================================================================


def test_o_relatorio_afirma_as_seis_coisas_com_numero(
    conn: sqlite3.Connection, run_do_agente, cenario  # noqa: F811
) -> None:
    """Criterio 1: observou, refletiu, propos, executou, custou, comparou."""
    dataset_id, cfg = cenario
    reprodutibilidade.provar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1
    )
    r = montar.montar(conn, run_do_agente.run_id)
    assert r["existe"]

    # observou: janela e dataset com hash
    assert r["observou"]["dataset_sha256"]
    assert r["observou"]["barras_disponiveis"] > 0
    assert r["observou"]["janela_observada_de_ms"] is not None

    # refletiu: reflexoes com custo
    assert r["refletiu"]["quantas"] >= 1
    assert r["refletiu"]["gasto"]["gasto_micro"] > 0
    assert all(x["provider"] for x in r["refletiu"]["reflexoes"])

    # propos: hash e JSON da regra
    assert r["propos"]["regra"]["hash"] == run_do_agente.regra_hash
    assert isinstance(r["propos"]["regra"]["params"], dict)

    # executou: contagem de ordens e execucoes
    assert r["executou"]["execucoes"] == run_do_agente.execucao["execucoes"]
    assert r["executou"]["compras"] and r["executou"]["vendas"]
    assert r["executou"]["digest"]

    # custos: nos dois livros
    assert r["custos"]["livro_simulado_usd"]["caixa_minor"] is not None
    assert r["custos"]["cambio_do_run"], "a ponte entre os livros e a taxa gravada"

    # comparado: numeros e excesso sobre baseline
    assert r["comparado"]["existe"]
    assert "excesso_sobre_b1_p50_cents" in r["comparado"]


def test_a_integridade_contabil_e_conferida_e_nao_afirmada(
    conn: sqlite3.Connection, run_do_agente
) -> None:
    """Criterio 3: debitos igual a creditos, saldo derivado igual ao exibido."""
    r = montar.montar(conn, run_do_agente.run_id)
    integridade = r["integridade"]
    assert integridade["partidas_dobradas_violadas"] == []
    assert integridade["saldos_divergentes"] == []
    assert not any(integridade["vinculo_inferencia"].values())
    assert integridade["arredondamento_do_custo_divergente"] == []
    assert integridade["config_hash_ainda_descreve"] is True
    assert integridade["ok"] is True


def test_a_resposta_da_0a_e_derivada_e_nao_uma_frase(
    conn: sqlite3.Connection, run_do_agente, cenario  # noqa: F811
) -> None:
    """A unica forma de "o ciclo fecha" nao ser autoelogio e ser calculado."""
    dataset_id, cfg = cenario
    reprodutibilidade.provar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1
    )
    r = montar.montar(conn, run_do_agente.run_id)
    resposta = r["resposta_da_0a"]

    assert resposta["fecha"] is True, resposta["faltando"]
    assert resposta["faltando"] == []
    # E cada condicao e um booleano que saiu de uma consulta, nao um adjetivo.
    for nome, valor in resposta["condicoes"].items():
        assert valor in (True, False, None), (nome, valor)


def test_uma_condicao_falsa_derruba_a_resposta_e_diz_qual(
    conn: sqlite3.Connection, run_do_agente
) -> None:
    """Se "fecha" fosse frase, sobreviveria a qualquer regressao futura.

    Aqui a prova de reprodutibilidade NAO foi rodada. A resposta tem de ser
    "nao fecha", nomeando exatamente o que falta - e nao um "sim" otimista.
    """
    r = montar.montar(conn, run_do_agente.run_id)
    resposta = r["resposta_da_0a"]
    assert resposta["fecha"] is False
    assert "reprodutibilidade_provada" in resposta["faltando"]
    assert "Problema de engenharia" in resposta["se_nao_fecha"]


def test_o_relatorio_le_a_config_do_run_e_nao_a_vigente(
    conn: sqlite3.Connection, run_do_agente, cenario, settings  # noqa: F811
) -> None:
    """O defeito de `condicoes_validade`, um nivel acima.

    Um relatorio que lesse a config vigente descreveria um run antigo com
    parametros que nao o produziram - e nada no texto acusaria.
    """
    versao_do_run = montar.montar(conn, run_do_agente.run_id)["config"]["version_id"]
    assert versao_do_run == 1

    # Nasce uma versao nova depois do run.
    conn.execute(
        "INSERT INTO config_version (created_at, author, payload_json,"
        " config_hash, material) VALUES (datetime('now'),'teste','{}','outro',1)"
    )
    depois = montar.montar(conn, run_do_agente.run_id)["config"]
    assert depois["version_id"] == versao_do_run
    assert depois["config_hash"] != "outro"


def test_o_que_nao_foi_concluido_e_texto_fixo(conn: sqlite3.Connection) -> None:
    """Criterio 4: derivar a lista de limites deixaria ela encolher sozinha."""
    r = montar.montar(conn)  # sem run nenhum, e a lista tem de vir igual
    assert r["nao_concluido"] == montar.NAO_CONCLUIDO
    junto = " ".join(montar.NAO_CONCLUIDO)
    for exigido in (
        "Nenhuma conclusao estatistica",
        "Nenhum conhecimento promovido",
        "neutro@1",
        "walk-forward",
        "holdout",
        "Portao A",
        "EM AMOSTRA",
    ):
        assert exigido in junto


def test_sem_run_o_relatorio_diz_isso_em_vez_de_estourar(
    conn: sqlite3.Connection
) -> None:
    r = montar.montar(conn)
    assert r["existe"] is False
    assert "nenhum run" in r["motivo"]


def test_zero_reflexoes_nao_reprova_o_run_por_ausencia_permitida(
    conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """D23: com o teto em zero nao ha custo de decisao a registrar.

    Tratar esse `None` como falso reprovaria o run pela falta de algo que a
    propria decisao permite - a confusao entre "nao sei" e "nao" que a secao
    5.2 proibe no custo, repetida um nivel acima.
    """
    dataset_id, cfg = cenario
    sem_cerebro = cfg.model_copy(update={"max_llm_calls_per_run": 0})
    resultado = ciclo.rodar(
        conn, dataset_id=dataset_id, config=sem_cerebro, config_version_id=1,
        settings=settings, adaptador=AdaptadorFalso([]),
    )
    r = montar.montar(conn, resultado.run_id)
    condicoes = r["resposta_da_0a"]["condicoes"]
    assert condicoes["o_cerebro_falou"] is False
    assert condicoes["custo_por_decisao_registrado"] is None
    assert condicoes["custos_nos_dois_livros"] is None
    # E o relatorio diz que nao houve cerebro, em vez de omitir.
    assert r["refletiu"]["houve_cerebro"] is False
    assert "o_cerebro_falou" in r["resposta_da_0a"]["faltando"]


# ===========================================================================
# O texto: so formata, nunca calcula
# ===========================================================================


def test_o_markdown_traz_as_dez_secoes_e_a_resposta(
    conn: sqlite3.Connection, run_do_agente, cenario, tmp_path  # noqa: F811
) -> None:
    dataset_id, cfg = cenario
    reprodutibilidade.provar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1
    )
    r = montar.montar(conn, run_do_agente.run_id)
    md = texto.markdown(r)

    # Guardado para inspecao humana quando o teste roda com -s.
    (tmp_path / "relatorio.md").write_text(md, encoding="utf-8")

    for secao in (
        "# Relatorio de fechamento da Fase 0A",
        "## A pergunta da 0A",
        "## 1. Observou",
        "## 2. Refletiu",
        "## 3. Propos regra",
        "## 4. Executou",
        "## 5. Registrou custos nos dois livros",
        "## 6. Comparado a B1, B2 e B3",
        "## 7. Avaliou o resultado",
        "## 8. Caminho percorrido",
        "## 9. Integridade contabil",
        "## 10. Reprodutibilidade",
        "## O que a 0A nao conclui",
    ):
        assert secao in md, secao

    assert "o ciclo basico fecha?" in md
    assert run_do_agente.regra_hash in md
    assert r["executou"]["digest"] in md
    assert r["reprodutibilidade"]["mesma_semente"]["digest_a"] in md


def test_o_markdown_nao_calcula_nada(conn: sqlite3.Connection) -> None:
    """Uma soma no formatador faria o texto e o JSON divergirem.

    O Markdown e justamente a versao que alguem le e acredita, e uma conta
    escondida ali seria a forma mais discreta de burlar o criterio 1 - que
    exige afirmacao com dados do banco.
    """
    import ast
    import pathlib

    arvore = ast.parse(pathlib.Path(texto.__file__).read_text(encoding="utf-8"))

    # As unicas contas permitidas sao conversoes de UNIDADE por divisor
    # constante: centavos -> dolar (100), ppm -> porcento (10_000),
    # bps -> porcento (100), ms -> segundos (1000).
    divisores_de_unidade = {100, 1_000, 10_000}
    proibidas: list[str] = []

    for no in ast.walk(arvore):
        if not isinstance(no, ast.BinOp) or isinstance(no.op, ast.Mod):
            continue
        if (
            isinstance(no.op, ast.Div)
            and isinstance(no.right, ast.Constant)
            and no.right.value in divisores_de_unidade
        ):
            continue
        # `+` com string constante e concatenacao de texto, nao aritmetica. O
        # AST nao distingue os dois por si, e tratar todo `+` como conta faria
        # o guarda acusar a juncao final das linhas do relatorio.
        ehtexto = isinstance(no.op, ast.Add) and (
            isinstance(getattr(no.right, "value", None), str)
            or isinstance(getattr(no.left, "value", None), str)
        )
        if ehtexto:
            continue
        proibidas.append(f"linha {no.lineno}: {type(no.op).__name__}")

    assert not proibidas, proibidas


def test_o_texto_diz_indisponivel_e_nunca_zero(conn: sqlite3.Connection) -> None:
    """"Nao sei" e "foi zero" sao afirmacoes diferentes (secao 5.2)."""
    assert texto._usd(None) == "indisponivel"
    assert texto._brl(None) == "indisponivel"
    assert texto._usd(0) == "US$ 0,00"
    # E o "nao se aplica" de uma condicao que a D23 permite faltar.
    assert texto._sim_nao(None) == "nao se aplica"
    assert texto._sim_nao(False) == "NAO"


# ===========================================================================
# As rotas: uma funcao so monta o relatorio, e nenhuma delas vaza segredo
# ===========================================================================


def test_a_rota_e_o_arquivo_saem_da_mesma_funcao(
    client, conn: sqlite3.Connection, cenario, settings, tmp_path  # noqa: F811
) -> None:
    """Dois geradores independentes poderiam discordar - e este e o pior
    documento do sistema para duas versoes da verdade."""
    from app import relatorio as pacote

    dataset_id, cfg = cenario
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    destino = tmp_path / "fechamento.md"
    pacote.escrever(conn, destino, run_id=resultado.run_id)

    da_rota = client.get(
        "/api/relatorio/markdown", params={"run_id": resultado.run_id}
    ).text
    do_arquivo = destino.read_text(encoding="utf-8")

    # O carimbo de geracao e a unica diferenca legitima entre os dois.
    sem_carimbo = lambda t: "\n".join(  # noqa: E731
        l for l in t.split("\n") if not l.startswith("Gerado em")
    )
    assert sem_carimbo(da_rota) == sem_carimbo(do_arquivo)


def test_a_rota_do_relatorio_responde_sem_run(client) -> None:
    corpo = client.get("/api/relatorio").json()
    assert corpo["existe"] is False
    assert corpo["nao_concluido"]


def test_as_rotas_de_vinculo_navegam_nos_dois_sentidos(
    client, conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    execucao = conn.execute(
        "SELECT id FROM execution WHERE run_id = ? LIMIT 1", (resultado.run_id,)
    ).fetchone()["id"]

    volta = client.get(f"/api/vinculo/execucao/{execucao}").json()
    assert volta["autorizada_por"]

    ida = client.get(f"/api/vinculo/evento/{volta['autorizada_por']}").json()
    assert ida["execucoes"]["quantas"] > 0
    assert ida["regra"]["regra_hash"] == resultado.regra_hash


def test_a_prova_de_reprodutibilidade_pela_rota_nao_chama_provedor(
    client, conn: sqlite3.Connection, cenario  # noqa: F811
) -> None:
    """A prova nao pode custar dinheiro nem depender do cache estar quente."""
    antes = conn.execute(
        "SELECT COUNT(*) AS n FROM agent_event WHERE provider IS NOT NULL"
    ).fetchone()["n"]

    corpo = client.post("/api/reprodutibilidade", json={}).json()
    assert corpo["provado"] is True

    depois = conn.execute(
        "SELECT COUNT(*) AS n FROM agent_event WHERE provider IS NOT NULL"
    ).fetchone()["n"]
    assert depois == antes, "nenhuma reflexao aconteceu"
    # E nada saiu do livro real.
    assert conn.execute(
        "SELECT COALESCE(SUM(e.amount_minor), 0) AS brl FROM ledger_entry e"
        " JOIN account a ON a.id = e.account_id"
        " WHERE a.code = 'real.despesa.inferencia'"
    ).fetchone()["brl"] == 0


def test_a_prova_e_recusada_com_run_ativo(
    client, conn: sqlite3.Connection, cenario  # noqa: F811
) -> None:
    """Tres runs novos no meio de um run aberto embaralhariam a leitura."""
    client.post("/api/run", json={"author": "teste"})
    assert client.post("/api/reprodutibilidade", json={}).status_code == 409


def test_nenhuma_rota_do_fechamento_expoe_a_chave(
    client, conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """Regra 15: segredo nunca aparece em log, em /api/health ou em pagina."""
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    execucao = conn.execute(
        "SELECT id FROM execution WHERE run_id = ? LIMIT 1", (resultado.run_id,)
    ).fetchone()["id"]
    evento = conn.execute(
        "SELECT id FROM agent_event WHERE run_id = ? LIMIT 1", (resultado.run_id,)
    ).fetchone()["id"]

    chaves = [
        settings.anthropic_api_key.get_secret_value(),
        settings.openai_api_key.get_secret_value(),
    ]
    for caminho in (
        "/api/relatorio",
        "/api/relatorio/markdown",
        f"/api/vinculo/execucao/{execucao}",
        f"/api/vinculo/evento/{evento}",
    ):
        corpo = client.get(caminho).text
        for chave in chaves:
            if chave:
                assert chave not in corpo, caminho


def test_a_comparacao_usa_uma_unidade_so(
    conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """36 execucoes e 18 idas e volta sao o mesmo giro, e nao o dobro.

    Defeito real desta tabela: o agente aparecia com `execucoes` (linhas de
    `execution`) na mesma coluna em que B1 aparecia com `operacoes_alvo`
    (idas e voltas). O leitor concluiria que o controle girou metade -
    exatamente o contrario do que a D19 existe para garantir.
    """
    dataset_id, cfg = cenario
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        semente=cfg.default_seed,
    )
    r = montar.montar(conn, resultado.run_id)

    # A unidade do agente e a mesma que o ciclo usou para casar o B1.
    assert r["executou"]["idas_e_voltas"] == resultado.execucao["operacoes"]
    assert r["executou"]["execucoes"] == resultado.execucao["execucoes"]
    assert r["executou"]["execucoes"] > r["executou"]["idas_e_voltas"]

    b1 = r["comparado"]["b1_casado_com_o_agente"]
    assert b1["operacoes_alvo"] == r["executou"]["idas_e_voltas"], (
        "D19: o controle casa o giro do que ele controla"
    )
    # E os baselines reportam na mesma unidade, nunca em linhas de execucao.
    for marcador in ("B2", "B3"):
        bloco = r["comparado"][marcador]
        assert bloco["idas_e_voltas"] * 2 <= bloco["execucoes"] + 1


def test_o_readme_descreve_todas_as_rotas() -> None:
    """Criterio 5: o README de operacao tem de descrever a api que existe.

    Este teste nasceu de um defeito: a tabela listava 6 endpoints quando
    havia 26. Uma tabela de endpoints que ninguem confere e mais uma forma do
    padrao que este projeto ja corrigiu seis vezes - descrevia a api, parou de
    descrever, e nada indicou a mudanca.

    Confere nos DOIS sentidos: rota sem linha no README, e linha no README sem
    rota correspondente. So o primeiro sentido deixaria a tabela acumular
    endpoints que deixaram de existir.
    """
    import pathlib
    import re

    from app.api.routes import router

    readme = (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    documentadas = set(re.findall(r"\|\s*`(/api/[^`]+)`\s*\|", readme))
    reais = {r.path for r in router.routes}

    faltando = sorted(reais - documentadas)
    sobrando = sorted(documentadas - reais)
    assert not faltando, f"rotas sem linha no README: {faltando}"
    assert not sobrando, f"linhas no README sem rota: {sobrando}"


# ===========================================================================
# O export: um arquivo com o estado inteiro, sem leitor novo e sem segredo
# ===========================================================================


def test_o_export_reune_as_telas_e_baixa_como_arquivo(
    client, conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    resposta = client.get("/api/exportar")
    assert resposta.status_code == 200
    assert "attachment" in resposta.headers["content-disposition"]
    assert ".json" in resposta.headers["content-disposition"]

    corpo = resposta.json()
    assert corpo["fase"] == "0A"
    assert corpo["partes_que_falharam"] == {}, corpo["partes_que_falharam"]
    for parte in (
        "health", "config", "config_history", "dataset", "ledger",
        "ledger_transacoes", "simulador", "execucoes", "comparacao", "curva",
        "agente", "relatorio", "sentinelas",
    ):
        assert parte in corpo["partes"], parte


def test_o_export_nao_tem_leitor_proprio(conn: sqlite3.Connection) -> None:
    """Reimplementar as consultas criaria um segundo jeito de responder.

    E no dia em que os dois discordassem, o arquivo exportado seria a versao
    que alguem leva para analisar sem ter como conferir (regra 16).

    A varredura e por AST: dentro da funcao `exportar` nao pode haver SQL nem
    aritmetica - ela so chama as funcoes de rota que ja servem cada tela.
    """
    import ast
    import pathlib

    fonte = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "api" / "routes.py"
    ).read_text(encoding="utf-8")
    arvore = ast.parse(fonte)
    alvo = next(
        no
        for no in ast.walk(arvore)
        if isinstance(no, ast.FunctionDef) and no.name == "exportar"
    )

    for no in ast.walk(alvo):
        if isinstance(no, ast.Constant) and isinstance(no.value, str):
            texto = no.value.upper()
            assert "SELECT " not in texto, f"SQL dentro de exportar: {no.value!r}"
        # As operacoes ARITMETICAS, nomeadas uma a uma. Excluir por lista
        # negativa marcava `int | None` da assinatura como conta: `|` e
        # BinOp tambem, e anotacao de tipo nao e calculo.
        if isinstance(no, ast.BinOp) and isinstance(
            no.op, (ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Pow)
        ):
            raise AssertionError(f"conta dentro de exportar, linha {no.lineno}")


def test_o_export_nao_carrega_segredo(
    client, conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """Regra 15: segredo nunca aparece em log, em /api/health ou em pagina.

    O export e o caso mais perigoso dos tres: e um arquivo feito para ser
    ENVIADO a outra pessoa. Um vazamento aqui sai da maquina junto.
    """
    _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    bruto = client.get("/api/exportar").text

    for chave in (
        settings.anthropic_api_key.get_secret_value(),
        settings.openai_api_key.get_secret_value(),
        settings.api_service_token.get_secret_value(),
    ):
        if chave:
            assert chave not in bruto

    # E o que aparece sobre credencial e PRESENCA, nunca valor.
    corpo = client.get("/api/exportar").json()
    credenciais = corpo["partes"]["health"]["credenciais_configuradas"]
    assert set(map(type, credenciais.values())) == {bool}


def test_uma_tela_quebrada_nao_derruba_o_export_inteiro(
    client, conn: sqlite3.Connection, monkeypatch
) -> None:
    """E justamente quando algo quebrou que alguem exporta o estado.

    Um export vazio por causa de uma tela e pior que um que diz qual tela.
    """
    from app.api import routes

    def explode(*_args, **_kwargs):
        raise RuntimeError("tela quebrada de proposito")

    monkeypatch.setattr(routes, "simulador_estado", explode)
    corpo = client.get("/api/exportar").json()

    assert "simulador" in corpo["partes_que_falharam"]
    assert "tela quebrada" in corpo["partes_que_falharam"]["simulador"]
    # E o resto veio inteiro.
    assert "ledger" in corpo["partes"]
    assert "health" in corpo["partes"]


def test_ha_uma_definicao_so_de_ida_e_volta(
    client, conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """Havia duas, e elas divergem exatamente no caso que importa.

    `COUNT(*) / 2` sobre as execucoes e a contagem de compras dao o mesmo
    numero enquanto toda compra fecha. Num run que termina COMPRADO, a
    divisao arredonda para baixo e some com a ida que ficou aberta.
    """
    from app.maos_rapidas import executor as maos

    dataset_id, cfg = cenario
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        semente=cfg.default_seed,
    )
    run_id = resultado.run_id
    verdade = maos.idas_e_voltas(conn, run_id)

    assert montar.montar(conn, run_id)["executou"]["idas_e_voltas"] == verdade
    assert client.get("/api/agente").json()["operacoes"] == verdade

    resumo = baselines.resumo_comparacao(conn)
    for marcador in ("B2", "B3"):
        bloco = resumo[marcador]
        assert bloco["idas_e_voltas"] == maos.idas_e_voltas(conn, bloco["run_id"])

    # E a divergencia que motivou a unificacao: com uma compra sem venda, a
    # divisao por dois perde a ida aberta.
    execucoes = conn.execute(
        "SELECT COUNT(*) AS n FROM execution WHERE run_id = ?", (run_id,)
    ).fetchone()["n"]
    conn.execute(
        "INSERT INTO execution (run_id, dataset_id, decision_bar_ms,"
        " execution_bar_ms, side, quantity_sats, price_ref, price_exec,"
        " notional_ref_cents, fee_cents, spread_cents, slippage_cents,"
        " penalty_cents, fidelity_level, ledger_transaction_id, rule_id)"
        " SELECT run_id, dataset_id, decision_bar_ms + 1, execution_bar_ms + 1,"
        " 'compra', quantity_sats, price_ref, price_exec, notional_ref_cents,"
        " fee_cents, spread_cents, slippage_cents, penalty_cents,"
        " fidelity_level, ledger_transaction_id, rule_id"
        " FROM execution WHERE run_id = ? AND side = 'compra' LIMIT 1",
        (run_id,),
    )
    assert maos.idas_e_voltas(conn, run_id) == verdade + 1
    assert (execucoes + 1) // 2 == verdade, "a divisao por dois perde a ida aberta"


def test_a_faixa_contra_o_acaso_vem_da_api(
    client, conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """Classificar e decidir, e decidir sobre o experimento nao e do painel."""
    _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    corpo = client.get("/api/agente").json()
    assert corpo["faixa"] in (
        "abaixo_p5", "entre_p5_e_p50", "entre_p50_e_p95", "acima_p95",
        "sem_controle",
    )


def test_a_rota_do_agente_informa_quantas_reflexoes_houve(
    client, conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """Por D23, zero reflexoes significa que o agente E o B3.

    E afirmacao forte demais para o painel produzir por acidente, a partir de
    um campo que a api nao mandou. O painel mostrava 0 num run com duas
    reflexoes exatamente assim - `?? 0` transformando ausencia em zero, que e
    a confusao que a secao 5.2 proibe no custo.
    """
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    corpo = client.get("/api/agente").json()
    assert corpo["reflexoes"] == resultado.reflexoes
    assert corpo["reflexoes"] > 0
