"""Testes do incremento 21: o relatorio da fase 0C, que nasce PROVISORIO.

> *"Pode iniciar o incremento 21 como gerador de relatorio, mas o relatorio
> permanece provisorio/aguardando evidencia ate: o piloto real fechar; a
> calibracao e a revalidacao rodarem; os criterios restantes do incremento 18
> serem realmente cumpridos."* - o usuario, 2026-09-08

O que este arquivo tem de provar e diferente do de costume, e a diferenca e o
ponto do incremento: **que a maquina roda inteira agora, e que o veredito
espera.** As duas metades sao testaveis, e uma sem a outra nao vale nada -

    so a primeira: um relatorio que gera e afirma sobre evidencia incompleta
    so a segunda: um relatorio que nao gera, e sera escrito no dia em que
                  houver numero para olhar
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from app.aovivo import bbo
from app.hipotese import dimensionamento as dim
from app.relatorio import fase_0c
from tests.test_cerebro import settings  # noqa: F401


# ---------------------------------------------------------------------------
# 1. A maquina roda AGORA, com todos os gates abertos
# ---------------------------------------------------------------------------


def test_o_relatorio_e_gerado_com_todos_os_gates_abertos(conn):
    """A primeira metade. Ele nao recusa: ele gera e se rotula.

    Recusar seria a alternativa que deixa o relatorio para depois - e um
    relatorio escrito no dia em que ha numero para olhar e um relatorio escrito
    PARA o numero.
    """
    r = fase_0c.montar(conn, potencia_ppm=dim.POTENCIA_ALVO_PPM)
    assert r["fase"] == "0C"
    assert r["gerado_em"]
    assert r["pergunta"]
    assert r["gates_de_evidencia"]
    assert r["nao_responde"]
    assert "viabilidade" in r


def test_o_estado_e_provisorio_e_vem_ANTES_dos_numeros(conn):
    """A segunda metade, e a posicao do campo importa.

    Um relatorio provisorio cujo rotulo aparece no fim e um relatorio que sera
    lido como definitivo - a pessoa ja formou a conclusao quando chega la.
    """
    r = fase_0c.montar(conn, potencia_ppm=dim.POTENCIA_ALVO_PPM)
    assert r["estado"] == fase_0c.PROVISORIO
    chaves = list(r)
    assert chaves.index("estado") < chaves.index("viabilidade")
    assert chaves.index("estado") < chaves.index("resposta_da_0c")


def test_a_resposta_da_fase_e_None_com_o_motivo_e_nunca_False(conn):
    """`None` porque nao foi medido, e nao porque a fase falhou.

    A 0A tratava `None` como neutro e ali estava certo (D23). Aqui a
    consequencia e direta: um `False` afirmaria que a 0C **nao fecha**, quando
    ela apenas nao terminou - e sao coisas diferentes que este projeto ja
    confundiu em `amostra_suficiente`.
    """
    r = fase_0c.montar(conn, potencia_ppm=dim.POTENCIA_ALVO_PPM)
    assert r["resposta_da_0c"] is None
    assert r["resposta_da_0c"] is not False
    assert r["por_que_sem_resposta"]
    for gate in r["pendentes"]:
        assert gate in r["por_que_sem_resposta"]


# ---------------------------------------------------------------------------
# 2. Os tres gates, e cada um DERIVADO
# ---------------------------------------------------------------------------


def test_os_tres_gates_sao_exatamente_os_que_o_usuario_nomeou(conn):
    """Tres, e nao dois nem quatro. A lista e da instrucao dele."""
    r = fase_0c.montar(conn, potencia_ppm=dim.POTENCIA_ALVO_PPM)
    assert set(r["gates_de_evidencia"]) == {
        "piloto_fechado",
        "calibracao_e_revalidacao",
        "incremento_18_cumprido",
    }


def test_cada_gate_aberto_diz_POR_QUE_bloqueia(conn):
    """Um gate que so diz `false` manda procurar; um que diz por que, informa.

    E o mesmo desenho do relatorio da 0A, que quando alguma condicao e falsa
    **diz qual** - em vez de devolver um booleano sozinho.
    """
    r = fase_0c.montar(conn, potencia_ppm=dim.POTENCIA_ALVO_PPM)
    for nome, gate in r["gates_de_evidencia"].items():
        assert gate["cumprido"] is False, f"{nome} nao deveria estar cumprido"
        assert gate["por_que_bloqueia"], (
            f"o gate {nome} bloqueia e nao diz por que"
        )


def test_o_gate_do_piloto_le_a_janela_e_NUNCA_a_fecha(conn):
    """Ele consulta. Fechar a janela de dentro de um relatorio seria fecha-la
    porque alguem abriu uma tela.

    `piloto.fechar` grava, e `piloto.ler`/`derivar` nao. O relatorio usa os
    dois que nao gravam, e este teste confere que nada apareceu na tabela.
    """
    antes = conn.execute("SELECT COUNT(*) AS n FROM janela_piloto").fetchone()["n"]
    fase_0c.montar(conn, potencia_ppm=dim.POTENCIA_ALVO_PPM)
    depois = conn.execute("SELECT COUNT(*) AS n FROM janela_piloto").fetchone()["n"]
    assert antes == depois == 0


def test_o_gate_da_calibracao_exige_as_DUAS(conn):
    """Calibracao **e** revalidacao. Uma so nao autoriza afirmar fidelidade.

    A D45 exige as duas em periodos DISJUNTOS, e a revalidacao exige o limite
    inferior do IC (`LB95(p10(E2)) >= 0`), nao o ponto. Contar so calibracoes
    deixaria uma fase com ajuste e sem confirmacao parecer completa.
    """
    gate = fase_0c._gate_calibracao_e_revalidacao(conn)
    assert gate["cumprido"] is False
    assert gate["calibracoes_aplicadas"] == 0
    assert gate["revalidacoes_consumidas"] == 0
    assert "DISJUNTOS" in gate["por_que_bloqueia"]


def test_o_gate_do_incremento_18_deriva_do_MESMO_fato_que_o_do_piloto(conn):
    """Uma fonte, e nao duas que podem divergir.

    Os criterios 5, 6 e 7 dependem do piloto real. Derivar cada um de uma
    consulta propria faria os dois blocos discordarem no dia em que uma delas
    mudasse - e este projeto conta essa historia em `n_efetivo` do run 30.
    """
    r = fase_0c.montar(conn, potencia_ppm=dim.POTENCIA_ALVO_PPM)
    piloto_ok = r["gates_de_evidencia"]["piloto_fechado"]["cumprido"]
    inc18 = r["gates_de_evidencia"]["incremento_18_cumprido"]
    assert inc18["cumprido"] == piloto_ok
    assert set(inc18["criterios"].values()) == {piloto_ok}
    assert len(inc18["criterios"]) == 3


def test_o_estado_vira_DEFINITIVO_quando_os_tres_fecham(conn, monkeypatch):
    """A guarda de nao-vacuidade: o relatorio TEM de conseguir sair definitivo.

    Sem isto, tudo acima passaria com uma funcao que devolve `provisorio`
    sempre - e um relatorio que nunca fecha nao e cauteloso, e inutil.

    Verificado pelo caminho dos gates, e nao trocando o rotulo: o que muda o
    estado e a evidencia.
    """
    cheio = {"cumprido": True, "por_que_bloqueia": None}
    monkeypatch.setattr(fase_0c, "_gate_piloto", lambda _c: dict(cheio))
    monkeypatch.setattr(
        fase_0c, "_gate_calibracao_e_revalidacao", lambda _c: dict(cheio)
    )
    monkeypatch.setattr(
        fase_0c, "_gate_incremento_18", lambda _c: dict(cheio)
    )
    r = fase_0c.montar(conn, potencia_ppm=dim.POTENCIA_ALVO_PPM)
    assert r["estado"] == fase_0c.DEFINITIVO
    assert r["pendentes"] == []
    assert r["resposta_da_0c"] is not None
    assert r["por_que_sem_resposta"] is None


# ---------------------------------------------------------------------------
# 3. A decisao de capacidade NAO e um gate
# ---------------------------------------------------------------------------


def test_a_decisao_de_capacidade_nao_bloqueia_o_relatorio(conn):
    """A distincao exata que o usuario fez.

    > "A decisao de capacidade experimental fica como bloqueante absoluto antes
    > de qualquer nova hipotese, nao como bloqueante da construcao do relatorio
    > da 0C."

    Se ela fosse gate, a unica forma de ver o numero que a sustenta seria
    decidir primeiro - a ordem invertida.
    """
    assert dim.DECISAO_DE_CAPACIDADE_EXPERIMENTAL is None
    r = fase_0c.montar(conn, potencia_ppm=dim.POTENCIA_ALVO_PPM)
    assert "decisao_de_capacidade" not in r["gates_de_evidencia"]
    bloco = r["decisao_de_capacidade"]
    assert bloco["escolhida"] is None
    assert "hipotese nova" in bloco["bloqueia"]
    assert "relatorio" in bloco["nao_bloqueia"]


# ---------------------------------------------------------------------------
# 4. O que a 0C nao responde, e a resposta a mais que ela ganhou
# ---------------------------------------------------------------------------


def test_a_lista_do_que_a_0c_nao_responde_e_FIXA():
    """Uma proibicao que se calcula dos dados pode desaparecer sozinha.

    Mesmo desenho de `A_0B_NAO_RESPONDE` e da lista da 0A.
    """
    assert isinstance(fase_0c.A_0C_NAO_RESPONDE, list)
    assert len(fase_0c.A_0C_NAO_RESPONDE) >= 6
    junto = " ".join(fase_0c.A_0C_NAO_RESPONDE)
    assert "place_order" in junto and "NAO PROVISIONADA" in junto
    assert "maker" in junto
    assert "FDR online" in junto


def test_a_incapacidade_MEDIDA_da_D48_esta_na_lista():
    """E ela e diferente de uma ausencia - o ponto que o plano nao previa.

    "Nada sobre book" diz o que nao foi feito. "O desenho nao consegue testar o
    efeito minimo" diz o que **nao da** para fazer com esta regua e este
    horizonte, e vem com o deficit em barras ao lado.
    """
    junto = " ".join(fase_0c.A_0C_NAO_RESPONDE)
    assert "NAO CONSEGUE testar o efeito minimo" in junto
    assert "incapacidade MEDIDA" in junto
    assert "D48" in junto


def test_o_rebaixamento_do_CUSUM_esta_declarado_na_lista():
    """O orcamento de <= 10% do ADR 0035 nao e entregue, e o relatorio diz.

    Um relatorio de fase que citasse o monitor sem dizer que ele e diagnostico
    deixaria "nenhum alarme" ser lido como "nada degradou".
    """
    junto = " ".join(fase_0c.A_0C_NAO_RESPONDE)
    assert "DIAGNOSTICO" in junto
    assert "7,5%" in junto and "18,4%" in junto


# ---------------------------------------------------------------------------
# 5. A viabilidade entra INTEIRA, e a rota existe
# ---------------------------------------------------------------------------


def test_a_viabilidade_entra_inteira_e_nao_por_referencia(conn):
    """Ela carrega a unica conclusao que a fase ja tem.

    Uma conclusao sobre o que NAO da para medir e a que mais facilmente se
    perde entre rotas: quem le o relatorio da fase nao vai abrir outra tela
    para descobrir que o desenho nao alcanca o efeito minimo.
    """
    r = fase_0c.montar(conn, potencia_ppm=dim.POTENCIA_ALVO_PPM)
    via = r["viabilidade"]
    assert "disponivel" in via
    assert "conclusao" in via


def test_a_rota_da_fase_responde_e_declara_o_estado(client):
    r = client.get("/api/relatorio/fase-0c")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["estado"] == fase_0c.PROVISORIO
    assert corpo["o_que_provisorio_quer_dizer"]
    assert fase_0c.PROVISORIO in corpo["o_que_provisorio_quer_dizer"]


def test_a_rota_entra_no_export(client):
    """O pacote inteiro tem de carregar o fechamento da fase.

    Um export que mostra os pedacos sem o fechamento pede que o leitor monte a
    resposta sozinho - e foi por exportar JSON e ler a mao que os tres
    primeiros runs da 0B foram conferidos.
    """
    r = client.get("/api/relatorio/exportar")
    assert r.status_code == 200
    partes = r.json()["partes"]
    assert "fase_0c" in partes


# ---------------------------------------------------------------------------
# 6. Uma definicao para o contrato, e nao tres
# ---------------------------------------------------------------------------


def test_o_contrato_padrao_tem_UMA_definicao():
    """`bbo@1` estava literal em duas rotas, e eu quase escrevi a terceira.

    Contrato e o objeto que amarra `execution_reference` e `latency_bars` a
    semantica de execucao vigente. Trocar de contrato e mudanca material - e um
    nome com tres donos e um nome que divergira.
    """
    from app.api.rotas import calibracao as rota_calibracao

    assert bbo.CONTRATO_PADRAO == "bbo@1"
    assert rota_calibracao.CONTRATO_PADRAO is bbo.CONTRATO_PADRAO
    assert fase_0c.CONTRATO_PADRAO is bbo.CONTRATO_PADRAO


def test_o_nome_do_contrato_casa_com_o_que_a_MIGRACAO_grava(conn):
    """A constante e a linha do banco tem de concordar.

    Sem isto, renomear a constante deixaria `piloto.ler` procurando um contrato
    que nao existe - e o gate do piloto ficaria eternamente aberto, por um
    motivo que nao e falta de evidencia.
    """
    linha = conn.execute(
        "SELECT contrato FROM bbo_contrato WHERE contrato = ?",
        (bbo.CONTRATO_PADRAO,),
    ).fetchone()
    assert linha is not None, (
        f"nao ha linha em `bbo_contrato` para {bbo.CONTRATO_PADRAO!r}: a"
        " constante e a migracao deixaram de concordar"
    )


def test_nenhum_modulo_novo_reintroduz_o_literal_do_contrato():
    """A guarda da forma, e nao do sintoma.

    Ela varre `app/` por `"bbo@1"` fora dos dois lugares que podem te-lo: o
    modulo que o define e a migracao que grava a linha. Consertar o sintoma num
    lugar e nao varrer a forma foi como o `SELECT MAX(run_id)` reapareceu na
    tela seguinte.
    """
    from tests._prosa import sql_sem_prosa

    raiz = pathlib.Path(__file__).resolve().parents[1] / "app"
    permitidos = {"aovivo/bbo.py", "migrations.py"}
    acusados = [
        caminho.relative_to(raiz).as_posix()
        for caminho in raiz.rglob("*.py")
        if caminho.relative_to(raiz).as_posix() not in permitidos
        and "bbo@1" in sql_sem_prosa(caminho)
    ]
    assert not acusados, (
        f"o literal do contrato voltou em {acusados}: importe"
        " `bbo.CONTRATO_PADRAO`"
    )
    # E a guarda nao e vazia: ela encontra o literal onde ele legitimamente esta.
    assert "bbo@1" in sql_sem_prosa(raiz / "aovivo" / "bbo.py")


# ---------------------------------------------------------------------------
# 7. Os gates NOMINAIS, e o desfecho que nao espera decisao
# ---------------------------------------------------------------------------


def test_os_gates_que_seguram_a_resposta_sao_NOMEADOS(conn):
    """> "mostre nominalmente quais sao os tres gates que mantem
    > `resposta_da_0c = None`" — o usuario, 2026-09-08

    "2 gates abertos" manda procurar quais; a lista informa. E cada entrada
    carrega o motivo do proprio gate, para que a leitura nao precise
    atravessar o documento.
    """
    r = fase_0c.montar(conn, potencia_ppm=dim.POTENCIA_ALVO_PPM)
    nomeados = r["gates_que_seguram_a_resposta"]
    assert {g["gate"] for g in nomeados} == set(r["pendentes"])
    assert len(nomeados) == 3
    for g in nomeados:
        assert g["gate"] in r["gates_de_evidencia"]
        assert g["por_que_bloqueia"], f"{g['gate']} nomeado sem motivo"


def test_a_0C_JA_PODE_terminar_como_sem_candidata(conn):
    """**A garantia que o usuario exigiu.**

    > "A decisao futura de capacidade deve bloquear novas hipoteses, nao
    > impedir que a 0C termine honestamente como sem candidata/inconclusiva."

    O desfecho sobre EDGE nao depende de gate nenhum: a D38 decidiu que nenhuma
    candidata entra, e isso e fato do banco. Os gates dizem se o SIMULADOR esta
    calibrado — pergunta diferente.
    """
    assert dim.DECISAO_DE_CAPACIDADE_EXPERIMENTAL is None
    r = fase_0c.montar(conn, potencia_ppm=dim.POTENCIA_ALVO_PPM)
    d = r["desfecho_antecipado"]

    assert d["sobre_edge"] == "SEM CANDIDATA - inconclusiva"
    assert d["candidatas_admitidas_no_forward"] == 0
    assert "encanamento" in d["por_que"].lower()
    assert "14.5" in d["e_isto_e_o_desfecho_previsto"]
    # E ele diz, no proprio campo, que nao espera nem gate nem decisao.
    assert "bloqueia hipotese NOVA" in d["nao_depende_de_gate_nem_de_decisao"]


def test_o_desfecho_e_DERIVADO_e_muda_sozinho(conn):
    """Se uma candidata for admitida, o campo diz outra coisa sem reescrita.

    Uma frase fixa dizendo "sem candidata" sobreviveria intacta ao dia em que
    houvesse uma — que é o padrão que este projeto conta vinte e oito vezes.
    """
    antes = fase_0c._desfecho_sem_candidata(conn)
    assert antes["sobre_edge"] is not None

    # Uma candidata admitida de verdade, com as chaves que o banco exige - e
    # nao um `monkeypatch` da consulta: o ponto do teste e que o campo sai do
    # BANCO, e trocar a consulta por um substituto testaria outra coisa.
    hid, run_id = _admitir_candidata(conn)
    assert hid and run_id
    depois = fase_0c._desfecho_sem_candidata(conn)
    assert depois["candidatas_admitidas_no_forward"] == 1
    assert depois["sobre_edge"] is None, (
        "com candidata admitida o desfecho nao pode continuar dizendo"
        " 'sem candidata'"
    )


def _admitir_candidata(conn: sqlite3.Connection) -> tuple[int, int]:
    """Uma linha em `quarentena_congelado`, com tudo que o banco exige.

    Existe para que o teste do desfecho exercite a CONSULTA, e nao um duble
    dela. As chaves estrangeiras sao reais: run, evento e hipotese.
    """
    conn.execute(
        "INSERT INTO run (agent_id, state, config_version_id, created_at,"
        " updated_at) VALUES ('agent-0001','concluido',1,'2026-09-08','x')"
    )
    run_id = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    conn.execute(
        "INSERT INTO agent_event (run_id, occurred_at, node, kind,"
        " cost_usd_minor, cost_usd_micro)"
        " VALUES (?,'2026-09-08','propor_regra','proposta',0,0)",
        (run_id,),
    )
    ev = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    conn.execute(
        "INSERT INTO hypothesis (run_id, agent_event_id, enunciado,"
        " agente_origem, timestamp_registro, metrica_primaria, efeito_minimo,"
        " n_minimo, sharpe_esperado_milesimos, criterio_parada,"
        " condicoes_validade_json, condicoes_falseamento_json, testavel,"
        " horizonte_barras, content_hash)"
        " VALUES (?,?,'uma abordagem','transacao@0b','2026-09-08',"
        "'excesso_sobre_b3_cents',50000,19240,2700,'n_minimo_alcancado',"
        "'{}','[{\"m\":1}]',1,21024,'hash-quarentena')",
        (run_id, ev),
    )
    hid = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    conn.execute(
        "INSERT INTO abordagem (assinatura, familia, forma_json, estado,"
        " criado_em) VALUES ('assinatura', 'cruzamento_medias', '{}',"
        " 'aberta', '2026-09-08')"
    )
    conn.execute(
        "INSERT INTO quarentena_congelado (hypothesis_id, run_id, run_digest,"
        " content_hash, abordagem_assinatura, abordagem_versao,"
        " identidade_executavel, dataset_sha256, timeframe, metrica_primaria,"
        " metrica_valor_cents, congelado_em)"
        " VALUES (?, ?, 'd', 'hash-quarentena', 'assinatura', 1, 'ident',"
        " 'ds', '15m', 'excesso_sobre_b3_cents', 1, '2026-09-08')",
        (hid, run_id),
    )
    return hid, run_id


def test_a_integridade_entra_INTEIRA_no_relatorio(conn):
    """> "inclua o resultado da reancoragem e de /api/diagnostico/integridade"

    Inteira, e nao por referencia: um relatorio de fase que afirma sobre
    resultado sem dizer se o substrato que os produziu continua de pe pede
    confianca no lugar de prova.

    E a reancoragem esta ali dentro — `todas_com_hash_integro` e a pergunta
    "alguem reescreveu historico?", cuja resposta correta e `false`, porque a
    reancoragem NAO toca as versoes antigas (ADR 0033).
    """
    r = fase_0c.montar(conn, potencia_ppm=dim.POTENCIA_ALVO_PPM)
    integridade = r["integridade"]
    assert "migracoes" in integridade or "schema_version" in integridade
    # O bloco tem de ser o MESMO que a rota de diagnostico publica.
    from app.relatorio import integridade as mod

    direto = mod.montar(conn)
    assert set(integridade) == set(direto), (
        "o relatorio da fase publica um subconjunto da integridade: duas"
        " versoes do mesmo bloco divergem"
    )


def test_a_integridade_do_relatorio_e_a_da_rota_sao_A_MESMA(client):
    """Uma definicao. Duas montagens do mesmo diagnostico divergiriam."""
    da_rota = client.get("/api/diagnostico/integridade")
    da_fase = client.get("/api/relatorio/fase-0c")
    assert da_rota.status_code == 200 and da_fase.status_code == 200
    a = da_rota.json()
    b = da_fase.json()["integridade"]
    # `gerado_em` difere por serem duas chamadas; o resto tem de bater.
    for campo in set(a) & set(b):
        if "gerado" in campo or "em" == campo:
            continue
        assert a[campo] == b[campo], f"integridade divergiu em {campo}"
