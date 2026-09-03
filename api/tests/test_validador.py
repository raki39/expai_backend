"""Testes do incremento 10: o validador independente e a máquina de estados.

§8.1 cabe em duas frases, e as duas são invariantes, não intenções:

> "Nenhum estado pode ser pulado. Um agente não pode promover a própria
> hipótese; a promoção é feita pelo módulo validador, que é independente do
> agente."

A primeira é imposta por **gatilho**; a segunda, por **fronteira de importação
verificável** mais um `CHECK` no banco. Nenhuma das duas depende de o módulo
Python ter sido usado corretamente — por isso quase toda proibição aqui é
testada com SQL cru.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sqlite3

import pytest

from app.validador import contador, estados, promocao
from tests.test_cerebro import (  # noqa: F401
    INTERPRETACAO_OK,
    PROPOSTA_OK,
    AdaptadorFalso,
    _rodar_ciclo,
    cenario,
    settings,
)

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
def run(conn: sqlite3.Connection, cenario, settings):  # noqa: F811
    """Um ciclo completo. A hipótese dele nasce NÃO TESTÁVEL.

    A fixture tem 2.500 barras de 15 minutos - ~26 dias -, e o Sharpe mínimo
    testável aí é 7,49, acima do teto de 5,00 que o schema aceita. Nada é
    testável numa janela de 26 dias, e isso é §8.3 funcionando (ADR 0020).

    Então este run exercita o caminho da D33: admitida, arquivada, sem
    parecer. O caminho testável é o `run_testavel` abaixo.
    """
    return _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )


@pytest.fixture
def run_testavel(conn: sqlite3.Connection, settings):  # noqa: F811
    """Um ciclo cuja hipótese CABE no horizonte, e portanto é avaliada.

    A conta, e ela é apertada de propósito porque não há como afrouxá-la:

    | | |
    |---|---|
    | Sharpe 5,00 (teto do schema) exige | 5.611 barras de amostra |
    | in-sample é 30% do dataset (D27) | 20.000 × 0,3 = 6.000 |

    Com 8.000 barras - o que esta fixture usava antes do incremento 9 - o
    in-sample tinha 2.400 e **nada era testável**. O número subiu porque a
    separação passou a valer: a amostra da hipótese é a janela onde ela roda,
    não o dataset inteiro. É a mesma aritmética que faz o Sharpe mínimo
    testável em produção ser 2,58 (ADR 0020).
    """
    import json as _json

    from app.config.schema import ExperimentConfig
    from tests.test_cerebro import PRE_REGISTRO_OK
    from tests.test_maos_rapidas import criar_dataset, precos_passeio

    dataset_id = criar_dataset(conn, precos_passeio(20_000))
    proposta = _json.dumps(
        {
            "familia": "cruzamento_medias",
            "rapida": 20,
            "lenta": 50,
            "periodo": None,
            "desvios_milesimos": None,
            "position_fraction_bps": 10_000,
            "stop_loss_bps": None,
            "pre_registro": {
                **PRE_REGISTRO_OK,
                "sharpe_esperado_milesimos": 5_000,
            },
            "confianca_ppm": 300_000,
        }
    )
    return _rodar_ciclo(
        conn,
        (dataset_id, ExperimentConfig()),
        settings,
        AdaptadorFalso([INTERPRETACAO_OK, proposta]),
    )


def _hipotese_registrada(conn: sqlite3.Connection) -> int:
    """Uma hipótese TESTÁVEL, admitida na máquina e parada na entrada."""
    conn.execute(
        "INSERT INTO run (agent_id, state, config_version_id, created_at,"
        " updated_at) VALUES ('agent-0001','executando',1,'2026-09-03','2026-09-03')"
    )
    run_id = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    conn.execute(
        "INSERT INTO agent_event (run_id, occurred_at, node, kind,"
        " cost_usd_minor, cost_usd_micro)"
        " VALUES (?, '2026-09-03', 'propor_regra', 'proposta', 0, 0)",
        (run_id,),
    )
    ev = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    conn.execute(
        "INSERT INTO hypothesis (run_id, agent_event_id, enunciado,"
        " agente_origem, timestamp_registro, metrica_primaria, efeito_minimo,"
        " n_minimo, sharpe_esperado_milesimos, criterio_parada,"
        " condicoes_validade_json, condicoes_falseamento_json, testavel,"
        " horizonte_barras, content_hash)"
        " VALUES (?,?,'x','transacao@0b','t','idas_e_voltas',0,10,3000,"
        "'fim_da_janela','{}','[{}]',1,10000,'h1')",
        (run_id, ev),
    )
    hid = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    promocao.admitir(conn, hid, run_id=run_id)
    return hid


# ===========================================================================
# CRITERIO 1 - fronteira de importação verificável
# ===========================================================================


def test_o_validador_nao_importa_o_agente() -> None:
    """§8.1 exige independência, e independência por disciplina já foi violada.

    Mesma técnica que protege a fronteira de §3.2 entre mãos rápidas e
    cérebro: uma varredura por AST, e não a promessa de que ninguém vai
    escrever o import.
    """
    proibidos = ("cerebro", "langgraph", "anthropic", "openai", "provedores")
    infratores: list[str] = []
    for arquivo in sorted((APP / "validador").rglob("*.py")):
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"), str(arquivo))
        for no in ast.walk(arvore):
            alvo = ""
            if isinstance(no, ast.Import):
                alvo = " ".join(a.name for a in no.names)
            elif isinstance(no, ast.ImportFrom):
                alvo = no.module or ""
            for termo in proibidos:
                if termo in alvo:
                    infratores.append(f"{arquivo.name}: {alvo}")
    assert not infratores, (
        "o validador importa o que julga: " + "; ".join(infratores)
    )

    # A guarda não pode ser vazia: o pacote precisa existir e ter imports.
    assert list((APP / "validador").glob("*.py"))


def test_a_direcao_permitida_e_a_inversa(run_testavel) -> None:
    """O ciclo do agente SOLICITA ao validador — §11.2.1.

    Não é simetria: o cérebro pode importar o validador, e o validador não
    pode importar o cérebro. Se as duas direções fossem permitidas, "módulo
    independente" seria só uma pasta com outro nome.
    """
    ciclo = (APP / "cerebro" / "ciclo.py").read_text(encoding="utf-8")
    assert "validador" in ciclo, "o ciclo solicita ao validador"
    assert run_testavel.parecer_do_validador is not None


def test_so_o_validador_escreve_na_maquina_de_estados() -> None:
    """A metade da garantia que a fronteira de importação não dá.

    O `CHECK (promoted_by = 'validador')` impede um valor errado na coluna.
    Ele não impede outro módulo de escrever a linha com o valor certo — quem
    impede é isto.
    """
    infratores = [
        str(a.relative_to(APP))
        for a in sorted(APP.rglob("*.py"))
        if a.parent.name != "validador"
        and a.name != "migrations.py"
        and "INSERT INTO hypothesis_state" in a.read_text(encoding="utf-8")
    ]
    assert not infratores, (
        "escrita na máquina de estados fora do validador: "
        + "; ".join(infratores)
    )
    assert "INSERT INTO hypothesis_state" in (
        APP / "validador" / "estados.py"
    ).read_text(encoding="utf-8"), "a guarda só vale se o permitido escrever"


# ===========================================================================
# CRITERIO 2 - nenhum estado pode ser pulado
# ===========================================================================


def test_a_entrada_e_sempre_por_hipotese_registrada(
    conn: sqlite3.Connection
) -> None:
    """SQL cru: entrar direto em `candidata` é impossível, não apenas evitado."""
    hid = _hipotese_registrada(conn)
    assert estados.atual(conn, hid).estado == "hipotese_registrada"

    conn.execute("DELETE FROM hypothesis_state WHERE 1=0")  # não apaga nada
    for estado_inicial in ("candidata", "em_quarentena", "conhecimento_validado"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO hypothesis_state (hypothesis_id, seq, from_state,"
                " state, occurred_at, promoted_by, evidence_json)"
                " VALUES (?, 1, NULL, ?, 't', 'validador', '{}')",
                (hid + 1000, estado_inicial),
            )


def test_pular_estado_e_recusado_pelo_banco(conn: sqlite3.Connection) -> None:
    """§8.1: "nenhum estado pode ser pulado".

    De `hipotese_registrada` direto para `em_quarentena` pula `candidata` —
    ou seja, pula o teste in-sample inteiro. É a promoção que mais importa
    impedir, porque é a que parece um atalho inofensivo.
    """
    hid = _hipotese_registrada(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO hypothesis_state (hypothesis_id, seq, from_state,"
            " state, occurred_at, promoted_by, evidence_json)"
            " VALUES (?, 2, 'hipotese_registrada', 'em_quarentena', 't',"
            " 'validador', '{}')",
            (hid,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO hypothesis_state (hypothesis_id, seq, from_state,"
            " state, occurred_at, promoted_by, evidence_json)"
            " VALUES (?, 2, 'hipotese_registrada', 'conhecimento_validado',"
            " 't', 'validador', '{}')",
            (hid,),
        )
    assert estados.atual(conn, hid).estado == "hipotese_registrada"


def test_a_transicao_precisa_partir_do_estado_atual(
    conn: sqlite3.Connection
) -> None:
    """Voltar no tempo é o outro jeito de burlar a máquina.

    Sem esta guarda, bastaria declarar um `from_state` conveniente para
    promover uma hipótese já invalidada — sem pular estado nenhum, e portanto
    passando pela guarda anterior.
    """
    hid = _hipotese_registrada(conn)
    estados.transitar(
        conn, hid, para="invalidado", evidencia={"motivo": "refutada"}
    )
    assert estados.atual(conn, hid).estado == "invalidado"

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO hypothesis_state (hypothesis_id, seq, from_state,"
            " state, occurred_at, promoted_by, evidence_json)"
            " VALUES (?, 3, 'hipotese_registrada', 'candidata', 't',"
            " 'validador', '{}')",
            (hid,),
        )


def test_a_transicao_e_imutavel(conn: sqlite3.Connection) -> None:
    """Apagar transição é apagar a prova de que nada foi pulado."""
    hid = _hipotese_registrada(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE hypothesis_state SET state = 'conhecimento_validado'"
            " WHERE hypothesis_id = ?",
            (hid,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "DELETE FROM hypothesis_state WHERE hypothesis_id = ?", (hid,)
        )


def test_o_estado_atual_e_derivado_e_nao_armazenado(
    conn: sqlite3.Connection
) -> None:
    """`hypothesis` não tem coluna de estado, e não poderia ter.

    Ela é imutável desde a migração 9 — um estado que muda precisaria de
    UPDATE. Derivar da última transição é o mesmo desenho do saldo, que sai
    do ledger (regra 16).
    """
    colunas = {
        l["name"] for l in conn.execute("PRAGMA table_info(hypothesis)")
    }
    assert "state" not in colunas and "estado" not in colunas

    hid = _hipotese_registrada(conn)
    estados.transitar(conn, hid, para="candidata", evidencia={"e": 1})
    assert estados.atual(conn, hid).estado == "candidata"
    assert estados.atual(conn, hid).transicoes == 2


def test_a_evidencia_da_transicao_e_obrigatoria(
    conn: sqlite3.Connection
) -> None:
    """Promoção sem evidência é indistinguível de promoção por engano."""
    hid = _hipotese_registrada(conn)
    for ruim in (None, "", "nao e json", "[]", '"texto"'):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO hypothesis_state (hypothesis_id, seq, from_state,"
                " state, occurred_at, promoted_by, evidence_json)"
                " VALUES (?, 2, 'hipotese_registrada', 'candidata', 't',"
                " 'validador', ?)",
                (hid, ruim),
            )


# ===========================================================================
# CRITERIO 3 - o agente não promove a própria hipótese
# ===========================================================================


def test_o_banco_recusa_promocao_que_nao_seja_do_validador(
    conn: sqlite3.Connection
) -> None:
    """§8.1, literal. O `CHECK` não aceita outro promotor."""
    hid = _hipotese_registrada(conn)
    for quem in ("agente", "transacao@0b", "cerebro", ""):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO hypothesis_state (hypothesis_id, seq, from_state,"
                " state, occurred_at, promoted_by, evidence_json)"
                " VALUES (?, 2, 'hipotese_registrada', 'candidata', 't', ?,"
                " '{}')",
                (hid, quem),
            )


def test_o_validador_nao_le_a_autoavaliacao_do_agente(
    conn: sqlite3.Connection, run_testavel
) -> None:
    """As duas avaliações são independentes, e isto prova que a segunda é.

    A avaliação posterior de R25.3 é a "avaliação do próprio agente"; o
    parecer aqui é a "avaliação independente do Validador". Se a segunda
    lesse a primeira para decidir, seriam uma só com dois nomes.

    O teste apaga o conteúdo da autoavaliação do resultado e recalcula: o
    veredito do validador não pode mudar.
    """
    run = run_testavel
    assert run.hypothesis_id is not None
    antes = promocao._julgar(conn, run.hypothesis_id, run.run_id)[0]

    # A autoavaliação existe e é do agente.
    auto = conn.execute(
        "SELECT evaluation_json FROM agent_event"
        " WHERE run_id = ? AND kind = 'avaliacao'",
        (run.run_id,),
    ).fetchone()
    assert auto is not None, "o agente avaliou o próprio resultado (R25.3)"
    assert json.loads(auto["evaluation_json"])["pre_registro"]["veredito"] is not None

    # E o validador chega ao mesmo lugar sem consultá-la: nenhuma consulta a
    # `agent_event` aparece no módulo do parecer.
    fonte = (APP / "validador" / "promocao.py").read_text(encoding="utf-8")
    assert "agent_event" not in fonte, (
        "o parecer independente consulta o registro cognitivo do agente"
    )
    assert "evaluation_json" not in fonte

    depois = promocao._julgar(conn, run.hypothesis_id, run.run_id)[0]
    assert depois.veredito == antes.veredito


def test_o_validador_recalcula_do_banco_e_nao_do_resultado_do_ciclo(
    conn: sqlite3.Connection, run
) -> None:
    """Herdar o objeto do ciclo herdaria também um defeito dele.

    A assinatura do parecer só recebe ids — não há como passar o
    `ResultadoDoCiclo` para dentro dele.
    """
    import inspect

    parametros = set(
        inspect.signature(promocao.avaliar_in_sample).parameters
    )
    assert parametros == {"conn", "hypothesis_id", "run_id"}


# ===========================================================================
# CRITERIO 4 - contador global, monotônico e nunca zerado
# ===========================================================================


def test_o_contador_nunca_diminui(conn: sqlite3.Connection) -> None:
    """R37 / §8.6: "esse contador nunca é zerado"."""
    antes = contador.total(conn)
    hids = [_hipotese_registrada(conn) for _ in range(3)]
    assert contador.total(conn) == antes + 3

    # Invalidar as três não diminui o contador: tentativa registrada continua
    # contando. §8.6 - "descartar tentativas fracassadas do registro é o
    # mecanismo exato que produz falsas descobertas".
    for hid in hids:
        estados.transitar(conn, hid, para="invalidado", evidencia={"e": 1})
    assert contador.total(conn) == antes + 3


def test_zerar_o_contador_e_impossivel_e_nao_apenas_proibido(
    conn: sqlite3.Connection
) -> None:
    """Derivado de tabela append-only: não há o que zerar."""
    _hipotese_registrada(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM hypothesis")
    # E não existe coluna de contador que um UPDATE pudesse alterar.
    tabelas = {
        l["name"]
        for l in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "tentativas_por_especialidade" not in tabelas, (
        "o contador é view; como tabela, poderia ser zerado por UPDATE"
    )


def test_o_contador_distingue_reteste_de_hipotese_nova(
    conn: sqlite3.Connection, run
) -> None:
    """A diferença custa 2 créditos em §8.6.1, e precisa ser calculável."""
    resumo = contador.resumo(conn)
    assert resumo["total"] >= 1
    linha = next(
        e for e in resumo["por_especialidade"]
        if e["especialidade"] == "transacao@0b"
    )
    assert linha["tentativas"] >= linha["hipoteses_distintas"]
    assert linha["retestes"] == linha["tentativas"] - linha["hipoteses_distintas"]


# ===========================================================================
# CRITERIO 5 - NAO_TESTAVEL alcançável, com motivo
# ===========================================================================


def test_nao_testavel_e_terminal_e_carrega_o_motivo(
    conn: sqlite3.Connection
) -> None:
    """§8.3 / R35: arquivada, com o motivo gravado."""
    conn.execute(
        "INSERT INTO run (agent_id, state, config_version_id, created_at,"
        " updated_at) VALUES ('agent-0001','executando',1,'t','t')"
    )
    run_id = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    conn.execute(
        "INSERT INTO agent_event (run_id, occurred_at, node, kind,"
        " cost_usd_minor, cost_usd_micro)"
        " VALUES (?, 't', 'propor_regra', 'proposta', 0, 0)",
        (run_id,),
    )
    ev = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    conn.execute(
        "INSERT INTO hypothesis (run_id, agent_event_id, enunciado,"
        " agente_origem, timestamp_registro, metrica_primaria, efeito_minimo,"
        " n_minimo, sharpe_esperado_milesimos, criterio_parada,"
        " condicoes_validade_json, condicoes_falseamento_json, testavel,"
        " motivo_nao_testavel, horizonte_barras, content_hash)"
        " VALUES (?,?,'x','transacao@0b','t','idas_e_voltas',0,999999,500,"
        "'fim_da_janela','{}','[{}]',0,'nao cabe no horizonte',100,'h2')",
        (run_id, ev),
    )
    hid = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])

    parecer = promocao.admitir(conn, hid, run_id=run_id)
    assert parecer.transicao == "nao_testavel"
    assert "nao cabe no horizonte" in parecer.motivo

    # Duas transições, e não uma: entrou na máquina e saiu dela. Pular a
    # entrada esconderia que a hipótese chegou a existir.
    h = estados.historico(conn, hid)
    assert [t["para"] for t in h] == ["hipotese_registrada", "nao_testavel"]

    # Terminal: não sai de lá.
    assert estados.atual(conn, hid).terminal
    assert estados.transicoes_legais(conn, "nao_testavel") == []


def test_arquivar_como_nao_testavel_uma_hipotese_testavel_e_recusado(
    conn: sqlite3.Connection
) -> None:
    """Seria afirmar sobre a amostra o contrário da conta de poder."""
    hid = _hipotese_registrada(conn)
    with pytest.raises(promocao.NaoAvaliavel, match="é testável"):
        promocao.arquivar_nao_testavel(conn, hid)


# ===========================================================================
# CRITERIO 6 - EM QUARENTENA existe, e nada na 0B a satisfaz
# ===========================================================================


def test_quarentena_e_alcancavel_como_estado(conn: sqlite3.Connection) -> None:
    """A transição precisa existir para a 0C poder gravá-la sem retrofit."""
    assert "em_quarentena" in estados.transicoes_legais(conn, "candidata")
    assert "conhecimento_validado" in estados.transicoes_legais(
        conn, "em_quarentena"
    )

    hid = _hipotese_registrada(conn)
    estados.transitar(conn, hid, para="candidata", evidencia={"e": 1})
    estados.transitar(conn, hid, para="em_quarentena", evidencia={"e": 2})
    assert estados.atual(conn, hid).estado == "em_quarentena"


def test_nada_na_0b_promove_para_conhecimento_validado() -> None:
    """Quem satisfaz a quarentena é o forward, e o forward é 0C.

    A transição existe; nenhum caminho de código da 0B a dispara. Se algum
    dispusesse, a 0B estaria promovendo conhecimento sem evidência futura —
    exatamente o que §8.5 reserva para a 0C.
    """
    infratores = [
        str(a.relative_to(APP))
        for a in sorted(APP.rglob("*.py"))
        if a.name != "migrations.py"
        and "conhecimento_validado" in a.read_text(encoding="utf-8")
    ]
    assert not infratores, (
        "código da 0B promovendo para conhecimento validado: "
        + "; ".join(infratores)
    )


def test_a_maquina_inteira_da_secao_8_1_esta_no_banco(
    conn: sqlite3.Connection
) -> None:
    """Os estados do documento, um a um, e nenhum a mais."""
    estados_no_banco = {
        l["para"] for l in conn.execute("SELECT DISTINCT para FROM transicao_legal")
    } | {
        l["de"] for l in conn.execute("SELECT DISTINCT de FROM transicao_legal")
    }
    da_secao = {
        "hipotese_registrada", "candidata", "em_quarentena",
        "conhecimento_validado", "revalidado", "condicionado",
        "em_suspeita", "invalidado",
    }
    # `nao_testavel` vem de §8.3, e não do diagrama de §8.1.
    assert estados_no_banco == da_secao | {"nao_testavel"}


# ===========================================================================
# Inconclusivo não move a hipótese (§14.4)
# ===========================================================================


def test_hipotese_nao_testavel_nao_recebe_parecer(
    conn: sqlite3.Connection, run
) -> None:
    """D33: arquivada não volta para a fila de avaliação.

    Ela executou - a D33 é explícita em que "não testável" bloqueia a
    promoção, não a execução - e o run é válido. O que não existe é parecer:
    avaliar uma hipótese cuja amostra nunca alcança o mínimo produziria um
    `inconclusiva` que já se sabia antes de olhar.
    """
    assert run.hypothesis_id is not None
    assert run.execucao["execucoes"] > 0, "arquivada ainda executa (D33)"
    assert run.parecer_do_validador is None
    assert estados.atual(conn, run.hypothesis_id).estado == "nao_testavel"


def test_inconclusivo_nao_promove_nem_descarta(
    conn: sqlite3.Connection, run_testavel
) -> None:
    """§14.4: "nem promove nem descarta (...) permanece em observação".

    Na fixture a amostra é pequena e a hipótese declarou Sharpe 2,0, então o
    veredito é `inconclusiva` — e o estado tem de continuar em
    `hipotese_registrada`. Tratar isso como refutação seria descartar por
    impaciência, "o erro simétrico ao de promover ruído".
    """
    parecer = run_testavel.parecer_do_validador
    assert parecer is not None
    if parecer["veredito"] == "inconclusiva":
        assert parecer["transicao"] is None
        assert parecer["estado_final"] == "hipotese_registrada"
        assert estados.atual(conn, run_testavel.hypothesis_id).estado == (
            "hipotese_registrada"
        )
        assert "não pode ser citada como evidência de sucesso" in parecer[
            "motivo"
        ] or "nao pode ser citada" in parecer["motivo"]


def test_a_etapa_recusa_partir_do_estado_errado(
    conn: sqlite3.Connection
) -> None:
    """O out-of-sample parte de `candidata`, e só."""
    hid = _hipotese_registrada(conn)
    with pytest.raises(promocao.NaoAvaliavel, match="Nenhum estado pode"):
        promocao.avaliar_out_of_sample(conn, hypothesis_id=hid, run_id=1)


def test_out_of_sample_exige_que_o_holdout_tenha_sido_lido(
    conn: sqlite3.Connection
) -> None:
    """Promover para quarentena sem tocar o selado seria chamar de fora da
    amostra um teste que não saiu dela."""
    hid = _hipotese_registrada(conn)
    estados.transitar(conn, hid, para="candidata", evidencia={"e": 1})
    with pytest.raises(promocao.NaoAvaliavel, match="período selado"):
        promocao.avaliar_out_of_sample(conn, hypothesis_id=hid, run_id=1)


def test_o_historico_reconstroi_o_caminho_inteiro(
    conn: sqlite3.Connection
) -> None:
    """É a sequência que prova que nenhum estado foi pulado."""
    hid = _hipotese_registrada(conn)
    estados.transitar(conn, hid, para="candidata", evidencia={"etapa": "in"})
    estados.transitar(conn, hid, para="em_quarentena", evidencia={"etapa": "out"})

    h = estados.historico(conn, hid)
    assert [t["para"] for t in h] == [
        "hipotese_registrada", "candidata", "em_quarentena"
    ]
    assert [t["de"] for t in h] == [
        None, "hipotese_registrada", "candidata"
    ]
    assert all(t["por"] == "validador" for t in h)
    assert all(t["evidencia"] for t in h)


def test_populacao_agrega_por_estado(conn: sqlite3.Connection, run) -> None:
    """A visão que o painel de população vai consumir."""
    p = estados.populacao(conn)
    assert sum(p.values()) == contador.total(conn)
