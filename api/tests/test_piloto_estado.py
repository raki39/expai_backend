"""O piloto: os DOIS bloqueadores, e a lacuna registrada SEPARADA dele.

> *"Acrescente uma projeção que mostre os dois possíveis bloqueadores: data
> mínima pelo calendário; data estimada pela contagem, usando a taxa válida das
> últimas 24/72 horas. 2026-09-18 17:17 UTC continua apenas como primeira
> possibilidade, não como promessa."* — o usuário, 2026-09-10

## O defeito que a projeção nova conserta

A versão anterior projetava o calendário como `agora + dias inteiros que
faltam`. A data **deslizava com a hora da leitura**: 17:17 numa leitura, 17:59
na seguinte, sobre o mesmo estado. O calendário é um instante fixo — primeira
válida + 14 dias corridos, a trava 1 de `piloto.derivar` —, e em produção ele é
**2026-09-18 16:15 UTC**.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3

from app.aovivo import bbo
from app.calibracao import piloto
from app.relatorio import piloto_estado as pe
from tests.test_bbo import CONTRATO, GRADE, SERIE, T0, amostra, ausente

DIA = 96  # instantes de grade num dia de 15 min
CALENDARIO = pe.DIAS_MINIMOS * DIA  # 1.344 instantes


def _gravar(conn: sqlite3.Connection, indices, validos) -> None:
    """Grava os instantes `indices` em ordem; valido se estiver em `validos`."""
    lote = [
        amostra(i, u=i + 1) if i in validos else ausente(i) for i in indices
    ]
    for k in range(0, len(lote), 50):
        bbo.receber(conn, SERIE, CONTRATO, lote[k : k + 50])


def _t(i: int) -> str:
    return pe._iso(T0 + i * GRADE)


# ---------------------------------------------------------------------------
# As travas sao UMA definicao
# ---------------------------------------------------------------------------


def test_as_travas_sao_IMPORTADAS_e_nao_copiadas():
    """Elas estavam copiadas em `piloto_estado` — a cópia é a que envelhece."""
    arvore = ast.parse(inspect.getsource(pe))
    atribuidos = {
        alvo.id
        for no in ast.walk(arvore)
        if isinstance(no, (ast.Assign, ast.AnnAssign))
        for alvo in (no.targets if isinstance(no, ast.Assign) else [no.target])
        if isinstance(alvo, ast.Name)
    }
    for nome in ("DIAS_MINIMOS", "OBSERVACOES_MINIMAS", "MS_POR_DIA"):
        assert nome not in atribuidos, f"{nome} voltou a ser copiado"
        assert getattr(pe, nome) == getattr(piloto, nome)


# ---------------------------------------------------------------------------
# O calendario e FIXO
# ---------------------------------------------------------------------------


def test_o_calendario_e_FIXO_e_nao_desliza_com_a_hora_da_leitura(conn, monkeypatch):
    """O defeito que produziu 17:17 numa leitura e 17:59 na seguinte."""
    _gravar(conn, range(200), set(range(200)))

    leituras = []
    # Dois relogios em lados opostos de uma virada de dia: a nao-vacuidade e
    # que o campo que DEPENDE do relogio muda, e os bloqueadores nao.
    for agora in (T0 + 3 * pe.MS_POR_DIA - 3_600_000,
                  T0 + 3 * pe.MS_POR_DIA + 3_600_000):
        monkeypatch.setattr(pe, "_agora_ms", lambda a=agora: a)
        leituras.append(pe.montar(conn))

    a, b = leituras
    assert a["dias"]["completos"] != b["dias"]["completos"], (
        "o relogio nao variou de verdade: o teste nao provaria nada"
    )
    ea = a["estimativa_OPERACIONAL_de_fechamento"]
    eb = b["estimativa_OPERACIONAL_de_fechamento"]
    assert ea["os_dois_bloqueadores"] == eb["os_dois_bloqueadores"]
    assert ea["os_dois_bloqueadores"]["calendario"]["data_minima"] == _t(CALENDARIO)
    # O que DEVE mudar com o relogio muda - e e publicado a parte.
    assert (
        ea["ancorada_em"]["defasagem_do_dado_horas_milesimos"]
        != eb["ancorada_em"]["defasagem_do_dado_horas_milesimos"]
    )
    assert ea["ancorada_em"]["alcance_do_dado"] == eb["ancorada_em"]["alcance_do_dado"]


# ---------------------------------------------------------------------------
# A contagem pelas DUAS taxas, e nenhuma escondida atras da outra
# ---------------------------------------------------------------------------


def test_a_contagem_projeta_pelas_DUAS_taxas_e_elas_podem_DISCORDAR(conn):
    """Aqui a de 24h diz "contagem bloqueia" e a de 72h diz "calendário".

    Publicar só a mais tarde esconderia exatamente isto: qual trava está perto
    de virar. Cem instantes todos válidos, depois cem alternados.
    """
    validos = set(range(100)) | {i for i in range(100, 200) if i % 2 == 0}
    _gravar(conn, range(200), validos)

    est = pe.montar(conn)["estimativa_OPERACIONAL_de_fechamento"]
    cont = est["os_dois_bloqueadores"]["contagem"]
    c24 = cont["pela_taxa_das_ultimas_24h"]
    c72 = cont["pela_taxa_das_ultimas_72h"]

    # 24h = os 96 instantes 104..199: 48 validos. Faltam 850 -> 1.700 instantes.
    assert (c24["instantes"], c24["validas"]) == (96, 48)
    assert c24["instantes_de_grade_necessarios"] == 1_700
    assert c24["data_estimada"] == _t(200 + 1_700)
    assert c24["bloqueador"] == "contagem"
    assert c24["primeira_possibilidade"] == _t(1_900)

    # 72h cobre tudo o que existe: 150 de 200. ceil(850 * 200 / 150) = 1.134.
    assert (c72["instantes"], c72["validas"]) == (200, 150)
    assert c72["instantes_de_grade_necessarios"] == 1_134
    assert c72["data_estimada"] == _t(200 + 1_134)
    assert c72["bloqueador"] == "calendario"
    assert c72["primeira_possibilidade"] == _t(CALENDARIO)
    # 10 instantes de 15 min = 2,5 h de folga.
    assert c72["folga_horas_milesimos"] == 2_500

    assert "POSSIBILIDADE" in " ".join(est)
    assert "NAO e afirmacao sobre calibracao" in est["o_que_isso_NAO_e"]


def test_sem_valida_na_janela_a_projecao_NAO_inventa_data(conn):
    """Taxa zero é "não fecha nunca a esta taxa", e não uma data distante."""
    _gravar(conn, range(200), set(range(50)))

    cont = pe.montar(conn)["estimativa_OPERACIONAL_de_fechamento"][
        "os_dois_bloqueadores"
    ]["contagem"]
    assert cont["pela_taxa_das_ultimas_24h"]["data_estimada"] is None
    assert "nao fecha nunca" in cont["pela_taxa_das_ultimas_24h"]["por_que_sem_data"]
    assert cont["pela_taxa_das_ultimas_72h"]["data_estimada"] is not None


def test_contagem_ALCANCADA_usa_a_mesma_regra_de_piloto_derivar(conn):
    """Não uma regra parecida: as duas datas batem com a janela oficial."""
    _gravar(conn, range(1_400), set(range(1_400)))

    janela = piloto.derivar(conn, SERIE, CONTRATO)
    bloq = pe.montar(conn)["estimativa_OPERACIONAL_de_fechamento"][
        "os_dois_bloqueadores"
    ]

    assert janela.fechada_por == "dias"
    assert bloq["calendario"]["data_minima"] == pe._iso(janela.ate_ms_exclusive)
    assert bloq["contagem"]["alcancada"] is True
    # A 1.000-esima valida esta no instante 999; a trava fecha uma grade depois.
    assert bloq["contagem"]["data"] == _t(1_000)
    assert bloq["contagem"]["bloqueador"] == "calendario"


# ---------------------------------------------------------------------------
# A lacuna registrada: separada do piloto, e CONFERIDA contra o dado
# ---------------------------------------------------------------------------

_REG = pe.LACUNAS_REGISTRADAS[0]
_I_INI = (_REG["ultima_valida_antes_ms"] - T0) // GRADE
_I_FIM = (_REG["primeira_valida_depois_ms"] - T0) // GRADE


def test_as_bordas_registradas_estao_na_GRADE():
    assert (_REG["ultima_valida_antes_ms"] - T0) % GRADE == 0
    assert (_REG["primeira_valida_depois_ms"] - T0) % GRADE == 0
    assert _I_FIM - _I_INI - 1 == 86


def test_a_lacuna_registrada_DESCREVE_o_dado_de_producao(conn):
    """O formato exato de produção: uma válida em cada borda, 86 ausentes."""
    _gravar(conn, range(_I_INI, _I_FIM + 1), {_I_INI, _I_FIM})

    r = pe.montar(conn)
    (lac,) = r["lacunas_registradas"]
    assert lac["o_registro_descreve_o_dado"] is True
    assert lac["impacto"]["instantes_de_grade_sem_validade"] == 86
    assert lac["impacto"]["instantes_esperados_entre_as_bordas"] == 86
    assert lac["impacto"]["duracao_horas_milesimos"] == 21_750
    assert lac["impacto"]["na_trava_de_calendario"].startswith("NENHUM")
    assert lac["a_volta_e_consistente_com_a_correcao"] is True
    assert "commit nao e deploy" in lac["o_que_essa_consistencia_NAO_prova"]
    # As quatro coisas que o usuario pediu, cada uma no seu campo.
    for campo in ("causa", "componente", "recuperacao", "impacto"):
        assert lac[campo], campo
    # E o piloto nao foi tocado, e nenhuma regra pedia que fosse.
    assert lac["regra_pre_registrada_que_exigiria_reinicio"] is None
    assert "ADR 0027" in lac["por_que_nenhuma"]
    assert "ADR 0032" in lac["por_que_nenhuma"]
    # A maior lacuna aponta para o registro que a explica.
    assert r["maior_lacuna"]["explicada_por"] == _REG["id"]


def test_o_registro_ACUSA_quando_para_de_descrever_o_dado(conn):
    """Não-vacuidade: uma válida no meio e o registro deixa de afirmar."""
    _gravar(conn, range(_I_INI, _I_FIM + 1), {_I_INI, _I_INI + 10, _I_FIM})

    (lac,) = pe.montar(conn)["lacunas_registradas"]
    assert lac["o_registro_descreve_o_dado"] is False
    assert lac["por_que_pode_nao_descrever"]


def test_lacuna_NAO_registrada_aparece_sem_explicacao(conn):
    """A maior lacuna sem registro diz que alguém precisa escrevê-lo."""
    _gravar(conn, range(201), {0, 200})

    ml = pe.montar(conn)["maior_lacuna"]
    assert ml["duracao_ms"] == 200 * GRADE
    assert ml["explicada_por"] is None


def test_sem_dado_o_registro_continua_publicado(conn):
    r = pe.montar(conn)
    assert r["disponivel"] is False
    (lac,) = r["lacunas_registradas"]
    assert lac["o_registro_descreve_o_dado"] is False


def test_montar_NAO_escreve_nada(conn):
    """Publicar o piloto é leitura. Nem o registro da lacuna grava."""
    _gravar(conn, range(200), set(range(0, 200, 3)))
    antes = conn.total_changes
    pe.montar(conn)
    assert conn.total_changes == antes
