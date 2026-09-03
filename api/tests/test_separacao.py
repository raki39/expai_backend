"""Testes do incremento 9: quatro conjuntos, purga, embargo e o holdout selado.

O peso destes testes esta em serem de ESTRUTURA, e nao de comportamento. A
secao 8.5.1 e explicita:

> "A separacao e garantida pela estrutura de dados e pelas permissoes da
> ferramenta, nao pela disciplina do agente (...) Um holdout que depende de
> boa vontade ja foi consumido."

Um teste que so verificasse "o loader lembrou de filtrar" provaria a
disciplina. Os daqui verificam que **nao existe chamada capaz de furar o
filtro** - o mesmo espirito dos criterios 4 e 5 do incremento 1.
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3

import pytest

from app.dataset import janelas as janelas_mod
from app.dataset import loader, purga, selado
from app.dataset import split as split_mod
from tests.test_cerebro import settings  # noqa: F401
from tests.test_dataset import (  # noqa: F401
    baixador_falso,
    config_curta,
    dois_meses,
    ingerir,
)

RAIZ = pathlib.Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
def dividido(conn: sqlite3.Connection, dois_meses):  # noqa: F811
    """Um dataset ingerido, ja com os quatro conjuntos e as janelas."""
    r = ingerir(conn, config_curta(), baixador=baixador_falso(dois_meses))
    return r


def _meses_com_movimento() -> dict[str, list]:
    """Os mesmos dois meses, com preco em passeio aleatorio.

    A fixture `dois_meses` gera preco CONSTANTE, e serve para os testes de
    estrutura - onde o valor da barra nao importa. Aqui importa: um
    cruzamento de medias sobre preco plano nunca cruza, nao ha execucao, e a
    sobreposicao amostral sairia `None` em vez de zero. `None` e `0` nao sao a
    mesma resposta, e o teste passaria a nao provar nada.
    """
    import calendar
    import random

    from tests.test_dataset import ESCALA, INTERVALO, ms
    from app.dataset.binance import Barra

    rng = random.Random(7)
    por_mes: dict[str, list] = {}
    preco = 50_000
    for mes in ("2024-09", "2024-10"):
        ano, m = (int(x) for x in mes.split("-"))
        inicio = ms(f"{ano:04d}-{m:02d}-01T00:00:00")
        barras = []
        for i in range(calendar.monthrange(ano, m)[1] * 96):
            preco = max(1_000, preco + rng.randint(-120, 120))
            p = preco * ESCALA
            barras.append(
                Barra(
                    open_time_ms=inicio + i * INTERVALO,
                    open=p, high=p + ESCALA, low=p - ESCALA, close=p,
                    volume=100 * ESCALA, quote_volume=100 * preco * ESCALA,
                    trades=100,
                )
            )
        por_mes[mes] = barras
    return por_mes


@pytest.fixture
def dividido_com_movimento(conn: sqlite3.Connection):
    """Dataset dividido cujo preco anda - entao a regra chega a operar."""
    return ingerir(
        conn, config_curta(), baixador=baixador_falso(_meses_com_movimento())
    )


# ===========================================================================
# CRITERIO 1 - os quatro conjuntos, contiguos e cronologicos
# ===========================================================================


def test_os_quatro_conjuntos_existem_e_cobrem_tudo_sem_sobrepor(
    conn: sqlite3.Connection, dividido
) -> None:
    """Contiguos e em ordem. Nenhuma barra em dois conjuntos nem em nenhum."""
    conjuntos = split_mod.ler(conn, dividido.dataset_id)
    assert [c.finalidade for c in conjuntos] == [
        "exploracao", "in_sample", "walk_forward", "holdout"
    ]

    # Semiaberto encadeado: o fim de um e o inicio do seguinte.
    for anterior, seguinte in zip(conjuntos, conjuntos[1:]):
        assert anterior.to_ms_exclusive == seguinte.from_ms, (
            f"buraco ou sobreposicao entre {anterior.finalidade} e"
            f" {seguinte.finalidade}"
        )

    total = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM bar WHERE dataset_id = ?",
            (dividido.dataset_id,),
        ).fetchone()["n"]
    )
    assert sum(c.bars for c in conjuntos) == total, (
        "as barras dos quatro conjuntos precisam somar o dataset inteiro:"
        " barra que nao esta em conjunto nenhum e barra que ninguem sabe se"
        " pode ler"
    )


def test_o_holdout_e_exatamente_a_reserva_carvada_na_ingestao(
    conn: sqlite3.Connection, dividido
) -> None:
    """O holdout nao PASSA A SER a reserva da D11: ele sempre foi.

    Se fosse um corte novo, seria um periodo escolhido depois de os dados
    existirem - e um holdout escolhido assim nao e holdout, e uma amostra com
    nome bonito.
    """
    holdout = split_mod.conjunto(conn, dividido.dataset_id, "holdout")
    assert holdout is not None
    assert holdout.from_ms == dividido.reserved_from_ms

    reservadas = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM bar WHERE dataset_id = ?"
            " AND open_time_ms >= ?",
            (dividido.dataset_id, dividido.reserved_from_ms),
        ).fetchone()["n"]
    )
    assert holdout.bars == reservadas


def test_a_divisao_e_imutavel(conn: sqlite3.Connection, dividido) -> None:
    """Mover a fronteira depois contamina o conjunto do outro lado.

    A tentacao aparece exatamente quando o resultado nao agrada, que e quando
    ela e mais cara - entao a proibicao mora no banco, nao na disciplina.
    """
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE dataset_split SET from_ms = from_ms + 1"
            " WHERE dataset_id = ? AND finalidade = 'holdout'",
            (dividido.dataset_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "DELETE FROM dataset_split WHERE dataset_id = ?",
            (dividido.dataset_id,),
        )


def test_o_banco_recusa_holdout_marcado_como_do_agente(
    conn: sqlite3.Connection, dividido
) -> None:
    """A permissao vive no DADO, e um INSERT cru nao a contorna."""
    for finalidade in ("holdout", "walk_forward"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dataset_split (dataset_id, finalidade, from_ms,"
                " to_ms_exclusive, bars, acesso) VALUES (?,?,1,2,0,'agente')",
                (dividido.dataset_id + 999, finalidade),
            )


# ===========================================================================
# CRITERIO 2 - nenhum caminho le o dataset direto; toda leitura declara finalidade
# ===========================================================================


def _modulos() -> list[pathlib.Path]:
    return sorted(RAIZ.rglob("*.py"))


def test_so_o_loader_e_o_selado_consultam_as_views_de_barra() -> None:
    """A fronteira e verificavel, e nao convencional.

    Mesmo mecanismo que ja prova a separacao de §3.2 entre maos rapidas e
    cerebro: uma varredura do codigo, e nao a promessa de que ninguem vai
    escrever a consulta em outro lugar.
    """
    permitidos = {
        RAIZ / "dataset" / "loader.py",
        RAIZ / "dataset" / "selado.py",
        RAIZ / "dataset" / "split.py",
        RAIZ / "dataset" / "janelas.py",
        RAIZ / "dataset" / "ingest.py",
        RAIZ / "migrations.py",
    }
    infratores: list[str] = []
    for arquivo in _modulos():
        if arquivo in permitidos:
            continue
        texto = arquivo.read_text(encoding="utf-8")
        for alvo in ("bar_por_finalidade", "bar_experimento", "FROM bar "):
            if alvo in texto:
                infratores.append(f"{arquivo.relative_to(RAIZ)}: {alvo}")
    assert not infratores, (
        "consulta a barra fora da fronteira do dataset: " + "; ".join(infratores)
    )

    # A guarda nao pode ser vazia. Se os proprios modulos da fronteira
    # deixassem de conter a consulta, este teste passaria por nao haver nada
    # que ele proibisse - que e como `BLOCOS` sobreviveu declarado e nunca
    # lido, sob um comentario dizendo que havia teste conferindo.
    assert "bar_por_finalidade" in (
        RAIZ / "dataset" / "loader.py"
    ).read_text(encoding="utf-8")
    assert "bar_por_finalidade" in (
        RAIZ / "dataset" / "selado.py"
    ).read_text(encoding="utf-8")


def test_nenhum_modulo_fora_do_selado_menciona_holdout_em_consulta() -> None:
    """O conjunto selado tem UM leitor, e ele grava o uso antes de ler."""
    infratores = [
        str(a.relative_to(RAIZ))
        for a in _modulos()
        if a.name not in ("selado.py", "split.py", "janelas.py", "migrations.py")
        and "'holdout'" in a.read_text(encoding="utf-8")
    ]
    assert not infratores, (
        "'holdout' citado como valor fora do modulo do validador: "
        + "; ".join(infratores)
    )
    assert "'holdout'" in (RAIZ / "dataset" / "selado.py").read_text(
        encoding="utf-8"
    ), "a guarda so vale se o modulo permitido de fato contiver o termo"


def test_carregar_exige_finalidade(conn: sqlite3.Connection, dividido) -> None:
    """R26: toda leitura de barra declara de que conjunto veio.

    Sem valor padrao, pelo mesmo motivo que `decision_ts_ms` nao tem: padrao e
    a forma mais comum de esquecer.
    """
    with pytest.raises(TypeError):
        loader.carregar(  # type: ignore[call-arg]
            conn, dividido.dataset_id, decision_ts_ms=10**15
        )


def test_o_caminho_do_agente_recusa_conjunto_do_validador(
    conn: sqlite3.Connection, dividido
) -> None:
    """Falha ALTO, e nao devolvendo zero barras.

    Uma lista vazia seria lida como "nao ha dado", que e coisa diferente de
    "voce nao pode ver isto" - e a diferenca entre as duas e justamente o que
    faria alguem procurar o bug no lugar errado.
    """
    for proibida in ("walk_forward", "holdout"):
        with pytest.raises(split_mod.FinalidadeProibida, match=proibida):
            loader.carregar(
                conn,
                dividido.dataset_id,
                decision_ts_ms=10**15,
                finalidade=proibida,
            )


def test_a_consulta_do_agente_traz_o_acesso_como_literal() -> None:
    """Nenhum parametro amplia o alcance.

    Se `acesso` fosse `:acesso` em vez de `'agente'`, bastaria um argumento
    para o mesmo codigo devolver o holdout - e o argumento errado nao levanta
    excecao, devolve dados.
    """
    assert "acesso = 'agente'" in loader._SQL_AGENTE
    assert ":acesso" not in loader._SQL_AGENTE
    assert "acesso = 'validador'" in selado._SQL_VALIDADOR
    assert ":acesso" not in selado._SQL_VALIDADOR


def test_o_agente_nunca_recebe_barra_de_fora_do_seu_conjunto(
    conn: sqlite3.Connection, dividido
) -> None:
    """Testado pelo DADO devolvido, e nao pela consulta escrita."""
    for finalidade in split_mod.FINALIDADES_DO_AGENTE:
        c = split_mod.conjunto(conn, dividido.dataset_id, finalidade)
        barras = loader.carregar(
            conn, dividido.dataset_id, decision_ts_ms=10**15,
            finalidade=finalidade,
        )
        assert barras
        assert min(b.open_time_ms for b in barras) >= c.from_ms
        assert max(b.open_time_ms for b in barras) < c.to_ms_exclusive
        assert max(b.open_time_ms for b in barras) < dividido.reserved_from_ms


# ===========================================================================
# CRITERIOS 3 e 4 - o holdout so pelo validador, e uma vez por hipotese
# ===========================================================================


@pytest.fixture
def hipotese(conn: sqlite3.Connection, dividido) -> int:
    """Uma hipotese minima a que pendurar o acesso ao holdout."""
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
    event_id = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    conn.execute(
        "INSERT INTO hypothesis (run_id, agent_event_id, enunciado,"
        " agente_origem, timestamp_registro, metrica_primaria, efeito_minimo,"
        " n_minimo, sharpe_esperado_milesimos, criterio_parada,"
        " condicoes_validade_json, condicoes_falseamento_json, testavel,"
        " horizonte_barras, content_hash)"
        " VALUES (?,?,'x','transacao@0b','t','idas_e_voltas',0,1,3000,"
        "'fim_da_janela','{}','[{}]',1,10,'h')",
        (run_id, event_id),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])


def test_o_validador_le_o_holdout_e_o_uso_fica_registrado(
    conn: sqlite3.Connection, dividido, hipotese
) -> None:
    barras = selado.holdout(
        conn,
        dividido.dataset_id,
        hypothesis_id=hipotese,
        finalidade_declarada="teste final da hipotese",
    )
    assert barras
    assert min(b.open_time_ms for b in barras) >= dividido.reserved_from_ms

    usos = selado.usos_do_holdout(conn, dividido.dataset_id)
    assert len(usos) == 1
    assert usos[0]["hypothesis_id"] == hipotese
    assert usos[0]["creditos"] == 5, "peso de out-of-sample, secao 8.6.1"


def test_o_holdout_e_usado_uma_vez_so_por_hipotese(
    conn: sqlite3.Connection, dividido, hipotese
) -> None:
    """R28. `UNIQUE` no banco e a regra inteira - nao ha contador em Python."""
    selado.holdout(
        conn, dividido.dataset_id, hypothesis_id=hipotese,
        finalidade_declarada="primeira e unica",
    )
    with pytest.raises(selado.HoldoutJaConsumido, match="ja leu o holdout"):
        selado.holdout(
            conn, dividido.dataset_id, hypothesis_id=hipotese,
            finalidade_declarada="segunda tentativa",
        )
    assert len(selado.usos_do_holdout(conn, dividido.dataset_id)) == 1


def test_o_registro_de_uso_do_holdout_e_imutavel(
    conn: sqlite3.Connection, dividido, hipotese
) -> None:
    """Apagar o uso e reusar o dado mais escasso sem que nada acuse."""
    selado.holdout(
        conn, dividido.dataset_id, hypothesis_id=hipotese,
        finalidade_declarada="teste final",
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM holdout_access WHERE hypothesis_id = ?", (hipotese,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE holdout_access SET creditos = 0 WHERE hypothesis_id = ?",
            (hipotese,),
        )


def test_o_acesso_ao_holdout_exige_finalidade_declarada(
    conn: sqlite3.Connection, dividido, hipotese
) -> None:
    """Secao 8.5.1: o acesso declara finalidade. Sem ela nao ha o que auditar."""
    with pytest.raises(ValueError, match="declara finalidade"):
        selado.holdout(
            conn, dividido.dataset_id, hypothesis_id=hipotese,
            finalidade_declarada="   ",
        )
    # E a recusa acontece ANTES de gravar: a hipotese nao gastou o acesso.
    assert not selado.ja_consumiu(conn, hipotese)


# ===========================================================================
# CRITERIO 5 - purga derivada do catalogo, embargo de 1%
# ===========================================================================


def test_a_purga_sai_do_maior_lookback_do_catalogo() -> None:
    """A D28 supos 200. O catalogo permite 400, e derivar pegou o erro.

    `CruzamentoMedias.lenta` aceita ate 400 - a mesma familia do B3. Uma purga
    de 200 deixaria passar metade do alcance da familia mais usada, e o
    vazamento nao apareceria em teste nenhum porque o numero pareceria
    deliberado.
    """
    barras, origem = purga.maior_lookback()
    assert barras == 400
    assert "CruzamentoMedias.lenta" in origem


def test_a_purga_acompanha_o_catalogo_em_vez_de_ser_constante() -> None:
    """Se uma familia ganhar lookback maior, a purga sobe sozinha.

    E o teste que impede a oitava ocorrencia do padrao: um numero que
    descrevia o catalogo e parou de descrever quando ele mudou.
    """
    from pydantic import Field

    from app.regra.schema import CruzamentoMedias

    original = CruzamentoMedias.model_fields["lenta"]
    antes, _ = purga.maior_lookback()
    try:
        CruzamentoMedias.model_fields["lenta"] = Field(ge=3, le=900)
        CruzamentoMedias.model_rebuild(force=True)
        depois, origem = purga.maior_lookback()
        assert depois == 900, "a purga precisa seguir o catalogo"
        assert "900" in origem
    finally:
        CruzamentoMedias.model_fields["lenta"] = original
        CruzamentoMedias.model_rebuild(force=True)
    assert purga.maior_lookback()[0] == antes


def test_o_embargo_e_um_por_cento_arredondado_para_cima() -> None:
    """Para cima: embargo menor deixa dependencia atravessar a fronteira."""
    assert purga.separacao(10_000).embargo_barras == 100
    assert purga.separacao(10_001).embargo_barras == 101
    assert purga.separacao(1).embargo_barras == 1


# ===========================================================================
# CRITERIO 6 - tres janelas independentes, sem barra dos dois lados
# ===========================================================================


def test_ao_menos_tres_janelas_de_walk_forward(
    conn: sqlite3.Connection, dividido
) -> None:
    """Secao 14.4, criterio B5: "pelo menos 3 janelas independentes"."""
    js = janelas_mod.ler(conn, dividido.dataset_id)
    assert len(js) >= janelas_mod.JANELAS_MINIMAS
    with pytest.raises(janelas_mod.JanelasImpossiveis, match="ao menos 3"):
        janelas_mod.planejar(conn, dividido.dataset_id, quantas=2)


def test_nenhuma_barra_de_teste_aparece_no_treino_da_mesma_janela(
    conn: sqlite3.Connection, dividido
) -> None:
    """Derivado do que ficou GRAVADO, nao do que o gerador pretendia."""
    conferido = janelas_mod.conferir_sem_vazamento(conn, dividido.dataset_id)
    assert conferido["conferido"] is True, conferido["problemas"]
    assert conferido["purga_barras"] == 400
    assert conferido["embargo_barras"] >= 1


def test_o_intervalo_entre_treino_e_teste_e_a_purga_mais_o_embargo(
    conn: sqlite3.Connection, dividido
) -> None:
    """Sem esse intervalo, a primeira barra de teste ja viu o treino.

    Uma media de 400 periodos calculada na primeira barra de teste usa as 400
    anteriores a ela. Se forem de treino, o teste nao e fora da amostra - e
    nao de um jeito sutil, e o indicador funcionando como projetado sobre
    dados do lado errado.
    """
    aberturas = [
        int(l["open_time_ms"])
        for l in conn.execute(
            "SELECT open_time_ms FROM bar WHERE dataset_id = ? ORDER BY open_time_ms",
            (dividido.dataset_id,),
        )
    ]
    posicao = {ms: i for i, ms in enumerate(aberturas)}
    for j in janelas_mod.ler(conn, dividido.dataset_id):
        removidas = posicao[j.teste_de_ms] - posicao[j.treino_ate_ms]
        assert removidas >= j.purga_barras + j.embargo_barras


def test_as_janelas_sao_imutaveis(conn: sqlite3.Connection, dividido) -> None:
    """Mover a fronteira depois de ver o resultado e o vazamento a mao."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE walk_forward_window SET purga_barras = 0 WHERE dataset_id = ?",
            (dividido.dataset_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "DELETE FROM walk_forward_window WHERE dataset_id = ?",
            (dividido.dataset_id,),
        )


def test_as_janelas_sao_reproduziveis(conn: sqlite3.Connection, dividido) -> None:
    """`planejar` duas vezes da o mesmo plano: nenhuma aleatoriedade no meio."""
    a = janelas_mod.planejar(conn, dividido.dataset_id)
    b = janelas_mod.planejar(conn, dividido.dataset_id)
    assert [j.como_dict() for j in a] == [j.como_dict() for j in b]
    # E o gravado e igual ao planejado.
    gravadas = janelas_mod.ler(conn, dividido.dataset_id)
    assert [j.como_dict() for j in gravadas] == [j.como_dict() for j in a]


# ===========================================================================
# CRITERIO 7 - suite de vazamento (R31, criterio A3 do Portao A)
# ===========================================================================


def test_nenhuma_leitura_devolve_barra_posterior_ao_timestamp_da_decisao(
    conn: sqlite3.Connection, dividido
) -> None:
    """R31 / A3: "verificado por teste automatizado, nao por inspecao".

    Varre a janela do agente inteira, barra a barra, e exige que nenhuma
    chamada devolva algo que ainda nao havia fechado naquele instante.
    """
    c = split_mod.conjunto(conn, dividido.dataset_id, "in_sample")
    todas = loader.carregar(
        conn, dividido.dataset_id, decision_ts_ms=10**15, finalidade="in_sample"
    )
    assert len(todas) > 10

    for barra_ref in todas[::7]:
        visiveis = loader.carregar(
            conn,
            dividido.dataset_id,
            decision_ts_ms=barra_ref.close_time_ms,
            finalidade="in_sample",
        )
        assert all(b.close_time_ms <= barra_ref.close_time_ms for b in visiveis)
        assert all(b.open_time_ms >= c.from_ms for b in visiveis)


def test_o_conjunto_de_exploracao_termina_antes_do_in_sample_comecar(
    conn: sqlite3.Connection, dividido
) -> None:
    """O cerebro observa exploracao e as maos executam in_sample (D27).

    Se os dois se sobrepusessem, o resultado do agente voltaria a ser em
    amostra - que e exatamente o que a D22 declarava na 0A por nao haver
    separacao, e o que a separacao existe para acabar.
    """
    exp = split_mod.conjunto(conn, dividido.dataset_id, "exploracao")
    ins = split_mod.conjunto(conn, dividido.dataset_id, "in_sample")
    assert exp.to_ms_exclusive <= ins.from_ms

    observadas = loader.carregar(
        conn, dividido.dataset_id, decision_ts_ms=10**15, finalidade="exploracao"
    )
    executadas = loader.carregar(
        conn, dividido.dataset_id, decision_ts_ms=10**15, finalidade="in_sample"
    )
    assert not (
        {b.open_time_ms for b in observadas} & {b.open_time_ms for b in executadas}
    ), "nenhuma barra pode estar nos dois conjuntos"


def test_o_walk_forward_nao_alcanca_o_holdout(
    conn: sqlite3.Connection, dividido
) -> None:
    """O vazamento mais caro que este sistema pode ter."""
    barras = selado.walk_forward(conn, dividido.dataset_id)
    assert barras
    assert max(b.open_time_ms for b in barras) < dividido.reserved_from_ms


def test_o_ast_prova_que_o_loader_nao_importa_o_cerebro() -> None:
    """A fronteira de §3.2 continua valendo depois do incremento 9.

    O modulo que decide o que pode ser lido nao pode depender de quem le -
    senao "permissao" vira uma conversa entre partes interessadas.
    """
    for arquivo in (RAIZ / "dataset").glob("*.py"):
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"), str(arquivo))
        for no in ast.walk(arvore):
            alvo = ""
            if isinstance(no, ast.Import):
                alvo = " ".join(a.name for a in no.names)
            elif isinstance(no, ast.ImportFrom):
                alvo = no.module or ""
            assert "cerebro" not in alvo and "langgraph" not in alvo, (
                f"{arquivo.name} importa {alvo}: o modulo que concede acesso"
                " a dado nao pode depender de quem pede acesso"
            )


def test_a_sobreposicao_amostral_caiu_a_zero_sem_a_conta_mudar(
    conn: sqlite3.Connection, dividido_com_movimento, settings  # noqa: F811
) -> None:
    """O efeito da divisao, medido pelo campo que ja existia.

    `sobreposicao_amostral` foi escrita no incremento 5 e dava 100% - o
    cerebro observava a janela que executava (D22), e a 0A declarava isso como
    numero em vez de prosa justamente para nao envelhecer.

    Com os quatro conjuntos ela da **zero**, e nenhuma linha da funcao mudou.
    E a prova de que a divisao e real e nao decorativa: um `dataset_split`
    que ninguem lesse deixaria este numero em 100%.
    """
    from app.cerebro import ciclo
    from app.config.schema import ExperimentConfig
    from tests.test_cerebro import (
        INTERPRETACAO_OK,
        PROPOSTA_OK,
        AdaptadorFalso,
    )

    resultado = ciclo.rodar(
        conn,
        dataset_id=dividido_com_movimento.dataset_id,
        config=ExperimentConfig(),
        config_version_id=1,
        settings=settings,
        adaptador=AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK]),
    )
    s = resultado.sobreposicao
    assert s["sobreposicao_bps"] == 0, (
        f"o cerebro viu {s['observado_de_ms']}..{s['observado_ate_ms']} e a"
        f" execucao rodou {s['executado_de_ms']}..{s['executado_ate_ms']}:"
        " os conjuntos voltaram a se sobrepor"
    )
    assert s["em_amostra"] is False
    assert s["observado_ate_ms"] <= s["executado_de_ms"]


# ===========================================================================
# O DEFEITO QUE A PERGUNTA DO USUARIO REVELOU
#
# Nona ocorrencia do padrao, e minha. `split.criar` e `janelas.gerar` so eram
# chamados no caminho de ingestao NOVA. O dataset de PRODUCAO foi ingerido no
# incremento 1, muito antes da migracao 10 - ele nunca passaria por la.
#
# O efeito seria mudo: `esta_dividido` False, loader no fallback, cerebro e
# maos rapidas de volta a mesma janela, `sobreposicao_amostral` de volta aos
# 100%, e os quatro conjuntos da secao 8.5.1 existindo no schema SEM SEPARAR
# NADA. E reingerir nao consertava: o caminho `ja_existia` retornava antes.
#
# Nenhum teste pegaria, porque todos ingerem do zero com as migracoes ja
# aplicadas. Os tres abaixo existem para que isso deixe de ser verdade.
# ===========================================================================


def test_dataset_ingerido_antes_da_migracao_ganha_a_divisao(
    conn: sqlite3.Connection, dois_meses  # noqa: F811
) -> None:
    """Simula o dataset de producao: existe, e nao tem divisao.

    Apagar as linhas de `dataset_split` reproduz exatamente o estado de um
    dataset ingerido antes do incremento 9 - e `garantir_separacao` tem de
    devolve-lo ao estado correto.
    """
    from app.dataset.ingest import garantir_separacao

    r = ingerir(conn, config_curta(), baixador=baixador_falso(dois_meses))
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("DROP TRIGGER dataset_split_sem_delete")
    conn.execute("DELETE FROM dataset_split WHERE dataset_id = ?", (r.dataset_id,))
    conn.execute("PRAGMA writable_schema = OFF")
    assert not loader.esta_dividido(conn, r.dataset_id)

    garantir_separacao(conn, r.dataset_id)

    assert loader.esta_dividido(conn, r.dataset_id)
    conjuntos = split_mod.ler(conn, r.dataset_id)
    assert [c.finalidade for c in conjuntos] == [
        "exploracao", "in_sample", "walk_forward", "holdout"
    ]
    # E o holdout continua sendo a MESMA reserva: recriar a divisao nao move
    # a fronteira selada.
    assert conjuntos[-1].from_ms == r.reserved_from_ms


def test_reingerir_um_dataset_existente_cria_a_divisao(
    conn: sqlite3.Connection, dois_meses  # noqa: F811
) -> None:
    """O caminho `ja_existia` tambem garante a separacao.

    Era o buraco: ele retornava antes de chegar na criacao, entao reingerir -
    a coisa mais natural a fazer para consertar - nao consertava nada.
    """
    r = ingerir(conn, config_curta(), baixador=baixador_falso(dois_meses))
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("DROP TRIGGER dataset_split_sem_delete")
    conn.execute("DELETE FROM dataset_split WHERE dataset_id = ?", (r.dataset_id,))
    conn.execute("PRAGMA writable_schema = OFF")

    de_novo = ingerir(conn, config_curta(), baixador=baixador_falso(dois_meses))
    assert de_novo.ja_existia is True
    assert de_novo.dataset_id == r.dataset_id
    assert loader.esta_dividido(conn, r.dataset_id), (
        "reingerir precisa garantir a separacao: e o unico caminho pelo qual"
        " um dataset ja existente passa de novo"
    )


def test_o_ciclo_da_0b_recusa_rodar_sem_separacao(
    conn: sqlite3.Connection, settings  # noqa: F811
) -> None:
    """A metade que importa mais: recusar alto em vez de cair no fallback.

    O fallback de `loader.carregar` existe para que os runs da 0A continuem
    reproduziveis (R12), e so para isso. Deixar o CICLO cair nele produziria
    um run que parece 0B e e 0A - e ninguem saberia que aquele resultado nao
    vale.

    Um resultado 0B sem separacao e pior que resultado nenhum.
    """
    from app.cerebro import ciclo
    from app.config.schema import ExperimentConfig
    from tests.test_cerebro import (
        INTERPRETACAO_OK,
        PROPOSTA_OK,
        AdaptadorFalso,
    )
    from tests.test_maos_rapidas import criar_dataset, precos_passeio

    # `dividir=False` reproduz o dataset legado.
    dataset_id = criar_dataset(conn, precos_passeio(2_500), dividir=False)
    assert not loader.esta_dividido(conn, dataset_id)

    with pytest.raises(ciclo.SeparacaoAusente, match="parece 0B e e 0A"):
        ciclo.rodar(
            conn,
            dataset_id=dataset_id,
            config=ExperimentConfig(),
            config_version_id=1,
            settings=settings,
            adaptador=AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK]),
        )

    # E nenhum run foi aberto: a recusa acontece ANTES de gravar qualquer
    # coisa. Um run pela metade seria pior que a recusa.
    assert (
        int(conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"]) == 0
    )
