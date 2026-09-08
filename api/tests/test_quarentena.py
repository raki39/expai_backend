"""A quarentena do forward (incremento 19, §8.5 e ADR 0034).

**Nenhuma candidata entra no forward da 0C**, e o caminho positivo é
exercitado por um **controle positivo sintético** que vive só aqui.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.quarentena import abordagem as abordagem_mod
from app.quarentena import admissao
from app.quarentena.veredito import (
    APROVADO,
    INCONCLUSIVO,
    REJEITADO,
    REQUISITOS,
    Limiares,
    Medicoes,
    avaliar,
    sem_candidata,
)

# ===========================================================================
# O CONTROLE POSITIVO SINTÉTICO
#
# ADR 0034, garantia 3. A única fraqueza da alternativa A é que, sem nada
# promovível, o caminho de promoção nunca roda - e caminho que nunca roda é o
# defeito que este projeto conta dezoito vezes.
#
# A saída NÃO é afrouxar a régua para o B3 passar. É este objeto: dados
# artificiais que satisfazem os limiares CONGELADOS, vivendo na suíte.
#
# Ele não produz evidência, não consome crédito, não entra em família e nunca
# aparece como candidata - porque ele nem passa por `admitir`. Ele alimenta
# `avaliar`, que é puro e não sabe de onde os dados vêm.
# ===========================================================================

LIMIARES = Limiares(
    periodo_minimo_barras=2_688,      # 28 dias a 15 min
    regimes_minimos=2,
    magnitude_minima_ppm=500_000,     # 50% do in-sample
    reserva_maxima_barras=8_064,      # 84 dias
)


def controle_positivo(**mudancas) -> Medicoes:
    """Medições artificiais que cumprem os SEIS. Só a suíte constrói isto."""
    base = dict(
        barras_observadas=3_000,
        n_efetivo=20_000.0,
        n_minimo=19_240,
        regimes_com_permanencia=("vol_baixa", "vol_alta"),
        direcao_declarada="alta",
        direcao_observada="alta",
        metrica_forward_cents=60_000,
        metrica_in_sample_cents=100_000,     # 60% do in-sample
        liquido_apos_custos_cents=45_000,
    )
    base.update(mudancas)
    return Medicoes(**base)  # type: ignore[arg-type]


def test_o_CONTROLE_POSITIVO_SINTETICO_atravessa_e_e_aprovado():
    """O caminho de promoção existe e roda - sem fraudar o experimento real.

    Se este teste falhasse, a quarentena seria um portão que nunca aprova, e
    um portão que nunca aprova é indistinguível de um portão quebrado.
    """
    v = avaliar(controle_positivo(), LIMIARES)
    assert v.resultado == APROVADO, v.motivo
    assert v.aprovado
    assert v.faltando == ()
    assert all(v.requisitos[k] is True for k in REQUISITOS)


def test_o_controle_positivo_NAO_passa_por_admitir():
    """É isso que o torna seguro.

    Se ele tivesse de ser admitido, `admitir` precisaria de um caminho que
    deixa passar - e aí haveria, em produção, um caminho capaz de produzir
    candidato admitido. Não há.
    """
    import inspect

    fonte = inspect.getsource(admissao)
    assert "sintetico" not in fonte.lower() or "nao ha" in fonte.lower()
    # A prova real: `admitir` não tem ramo que devolva admissão.
    assert "return Recusa(" in fonte
    assert "return Admissao" not in fonte and "admitida=True" not in fonte


def test_NENHUM_caminho_de_producao_constroi_candidato_admitido():
    """A guarda que sustenta a garantia 3, varrendo `app/` inteiro."""
    import re
    from pathlib import Path

    from tests._prosa import sql_sem_prosa

    proibidos = re.compile(
        r"controle_positivo|candidata_sintetica|forjar_candidata", re.IGNORECASE
    )
    culpados = [
        str(f.relative_to(Path("app").parent))
        for f in sorted(Path("app").rglob("*.py"))
        if proibidos.search(sql_sem_prosa(f))
    ]
    assert not culpados, (
        f"{culpados} constrói controle positivo em produção: ele tem de viver "
        f"só na suíte, senão deixa de ser sintético e passa a ser evidência"
    )
    # Não-vacuidade: a guarda casa a forma que procura.
    assert proibidos.search("def controle_positivo():")


# ===========================================================================
# Os seis requisitos, um a um (critério 1)
# ===========================================================================


@pytest.mark.parametrize(
    "requisito,mudanca",
    [
        ("periodo_tecnico_minimo", {"barras_observadas": 100}),
        ("n_efetivo_alcancado", {"n_efetivo": 100.0}),
        ("regimes_distintos", {"regimes_com_permanencia": ("vol_baixa",)}),
        ("magnitude_minima", {"metrica_forward_cents": 1_000}),
    ],
)
def test_cada_requisito_reprova_SOZINHO(requisito: str, mudanca: dict):
    """Um portão que nunca reprova não é portão - e ele tem de dizer QUAL."""
    v = avaliar(controle_positivo(**mudanca), LIMIARES)
    assert v.resultado != APROVADO
    assert v.requisitos[requisito] is False
    assert requisito in v.faltando
    assert requisito in v.motivo


def test_os_SEIS_estao_todos_presentes():
    """Nenhum requisito de §8.5 pode faltar da lista."""
    v = avaliar(controle_positivo(), LIMIARES)
    assert set(v.requisitos) == set(REQUISITOS)
    assert len(REQUISITOS) == 6


# ===========================================================================
# Critério 7: o forward que DEGRADA, e a quarentena diz qual falhou
# ===========================================================================


def test_um_forward_que_DEGRADA_e_a_quarentena_nomeia_o_requisito():
    """O teste que o plano exige: planta a degradação e cobra o nome.

    Magnitude de 10% do in-sample contra um mínimo de 50%: o resultado se
    manteve positivo, e mesmo assim não basta - é a diferença entre "deu
    lucro" e "o resultado se mantém".
    """
    v = avaliar(
        controle_positivo(metrica_forward_cents=10_000), LIMIARES
    )
    assert v.resultado == INCONCLUSIVO
    assert v.faltando == ("magnitude_minima",)
    assert v.detalhe["magnitude_ppm"] == 100_000
    assert v.detalhe["magnitude_minima_ppm"] == 500_000


def test_direcao_CONTRARIA_com_amostra_suficiente_e_REJEITADO():
    """Definitivo tem um teste: mais dado mudaria a resposta?

    Direção contrária com amostra suficiente não muda de sinal por esperar
    mais - e continuar esperando daria à hipótese um número ilimitado de
    tentativas.
    """
    v = avaliar(controle_positivo(direcao_observada="baixa"), LIMIARES)
    assert v.resultado == REJEITADO
    assert "nao inverte o sinal" in v.motivo


def test_negativo_apos_custos_com_amostra_suficiente_e_REJEITADO():
    v = avaliar(
        controle_positivo(liquido_apos_custos_cents=-1), LIMIARES
    )
    assert v.resultado == REJEITADO
    assert "FATO do ledger" in v.motivo


def test_direcao_contraria_SEM_amostra_ainda_e_inconclusivo():
    """Sem amostra, "contrário" pode ser ruído - e chamar de rejeitado seria
    afirmar o que ninguém mediu."""
    v = avaliar(
        controle_positivo(direcao_observada="baixa", n_efetivo=10.0), LIMIARES
    )
    assert v.resultado == INCONCLUSIVO


# ===========================================================================
# Critério 6: os três resultados continuam três, e inconclusivo nunca sobe
# ===========================================================================


def test_reserva_encerrada_SEM_cumprir_e_INCONCLUSIVO():
    """Nunca aprovação, nunca régua flexibilizada (garantia 6)."""
    v = avaliar(
        controle_positivo(
            barras_observadas=9_000,          # passou da reserva
            regimes_com_permanencia=("vol_baixa",),
        ),
        LIMIARES,
    )
    assert v.resultado == INCONCLUSIVO
    assert v.detalhe["reserva_encerrada"] is True
    assert "RESERVA ENCERRADA" in v.motivo
    assert "NUNCA aprovacao" in v.motivo
    assert "nao pode ser reduzido" in v.motivo


def test_SEM_CANDIDATA_tem_veredito_PROPRIO():
    """"Não houve candidata" é diferente de "a candidata não alcançou a
    amostra", e §14.4 exige que inconclusivo diga QUAL."""
    v = sem_candidata()
    assert v.resultado == INCONCLUSIVO
    assert not v.aprovado
    assert v.detalhe["sem_candidata"] is True
    assert "NENHUMA CANDIDATA ADMITIDA" in v.motivo
    assert "CONTROLE NEGATIVO" in v.motivo
    assert all(v.requisitos[k] is None for k in REQUISITOS)


def test_in_sample_NAO_POSITIVO_deixa_a_magnitude_indefinida():
    """"50% de um número negativo" seria uma comparação que passa quando o
    forward piora. `None` é "não se pode dizer", e não `True`."""
    v = avaliar(
        controle_positivo(metrica_in_sample_cents=-100), LIMIARES
    )
    assert v.requisitos["magnitude_minima"] is None
    assert v.detalhe["magnitude_ppm"] is None
    assert v.resultado != APROVADO


def test_direcao_NAO_DECLARADA_fica_None_e_nao_True():
    """As hipóteses 1 a 41 foram pré-registradas antes da taxonomia existir, e
    inventar uma direção para elas alteraria o pré-registro."""
    v = avaliar(controle_positivo(direcao_declarada=None), LIMIARES)
    assert v.requisitos["direcao_prevista"] is None
    assert v.resultado != APROVADO


# ===========================================================================
# Critério 2: nenhum limiar de dias em código de decisão (R73)
# ===========================================================================


def test_NENHUM_limiar_de_dias_no_codigo_de_decisao():
    """Todo número vem de `Limiares`, lido do banco e congelado.

    Uma constante de dias aqui seria um limiar que ninguém versionou - e
    mudá-lo não deixaria rastro em `config_change`.
    """
    import re
    from pathlib import Path

    from tests._prosa import sql_sem_prosa

    fonte = sql_sem_prosa(Path("app/quarentena/veredito.py"))
    # Números "redondos de calendário" que denunciariam um limiar embutido.
    suspeitos = re.findall(r"\b(?:28|30|60|84|90|672|2688|8064)\b", fonte)
    assert not suspeitos, (
        f"número de calendário no código de decisão: {suspeitos}. Os limiares "
        f"vivem em `quarentena_limiar`, congelados"
    )
    assert "Limiares" in fonte


# ===========================================================================
# ADR 0034, garantia 1: assinatura estrutural e linhagem
# ===========================================================================

P41 = {"familia": "cruzamento_medias", "rapida": 50, "lenta": 200,
       "position_fraction_bps": 8_000, "stop_loss_bps": 2_000}


def _hipotese_minima(conn: sqlite3.Connection, content_hash: str) -> int:
    """A linha mínima que a FK de `abordagem` exige.

    **Reusa o helper que a suíte já tem** (`tests/test_creditos._hipotese`) em
    vez de reinventar o `INSERT`: eu escrevi três versões dele aqui, e cada uma
    esbarrou numa coluna obrigatória diferente - `run_id`, depois
    `agent_event_id`. Duas cópias do mesmo `INSERT` divergem, e este projeto
    conta essa história em `baselines.condicoes` contra
    `contrato.condicoes_da_config`.
    """
    from app.ledger import livro
    from tests.test_creditos import _hipotese

    versao = conn.execute(
        "SELECT MIN(id) AS id FROM config_version"
    ).fetchone()["id"]
    run_id, _ = livro.abrir_run(
        conn, config_version_id=int(versao),
        seed_capital_usd_cents=100_000, agent_id=f"quarentena-{content_hash}",
    )
    livro.encerrar_run(conn, run_id, "concluido")
    return _hipotese(conn, run_id, hash_=content_hash)


def test_variacao_PARAMETRICA_e_a_MESMA_abordagem():
    """50/200 e 50/210 são o mesmo mecanismo.

    `content_hash` pega a cópia idêntica e não pega esta - era o único
    bloqueio que existia, e ele não bastava.
    """
    maquiada = {**P41, "lenta": 210}
    assert abordagem_mod.assinatura_de(P41) == abordagem_mod.assinatura_de(maquiada)


def test_variacao_TEXTUAL_e_a_MESMA_abordagem():
    """Reescrever a frase não muda o que a regra faz."""
    com_texto = {**P41, "enunciado": "uma tese completamente nova sobre o BTC"}
    assert abordagem_mod.assinatura_de(P41) == abordagem_mod.assinatura_de(com_texto)


def test_FAMILIA_diferente_e_abordagem_diferente():
    outra = {"familia": "banda_desvio", "janela": 20, "desvios": 2,
             "position_fraction_bps": 8_000, "stop_loss_bps": 2_000}
    assert abordagem_mod.assinatura_de(P41) != abordagem_mod.assinatura_de(outra)


def test_MECANISMO_diferente_na_mesma_familia_e_abordagem_diferente():
    """Tirar o stop muda a forma da regra, e não só o valor dela."""
    sem_stop = {**P41, "stop_loss_bps": None}
    assert abordagem_mod.assinatura_de(P41) != abordagem_mod.assinatura_de(sem_stop)


def test_a_assinatura_e_LEGIVEL_e_nao_um_hash():
    """Um humano olhando o registro tem de conseguir ver POR QUE duas
    hipóteses são a mesma abordagem."""
    a = abordagem_mod.assinatura_de(P41)
    assert "cruzamento_medias" in a
    assert "lenta" in a and "rapida" in a
    assert "200" not in a, "o VALOR não pode entrar na assinatura"


def test_abordagem_REJEITADA_bloqueia_a_variacao_maquiada(conn):
    """A linhagem: rejeitada uma vez, a variação herda o bloqueio."""
    hid = _hipotese_minima(conn, "h41")

    abordagem_mod.rejeitar(
        conn, params=P41, hypothesis_id=int(hid),
        motivo="Portao B rejeitou por cinco criterios independentes",
    )

    with pytest.raises(abordagem_mod.AbordagemRejeitada) as e:
        abordagem_mod.exigir_nao_rejeitada(conn, params={**P41, "lenta": 210})
    assert "DESCARTADA" in str(e.value)
    assert "reprojeto" in str(e.value) or "0B" in str(e.value)


def test_abordagem_rejeitada_NAO_REABRE(conn):
    """A volta legítima é um reprojeto pela 0B, com assinatura nova."""
    abordagem_mod.registrar(conn, params=P41)
    hid = _hipotese_minima(conn, "h-reabre")
    abordagem_mod.rejeitar(
        conn, params=P41, hypothesis_id=int(hid), motivo="Portao B",
    )
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute(
            "UPDATE abordagem SET estado = 'aberta' WHERE assinatura = ?",
            (abordagem_mod.assinatura_de(P41),),
        )
    assert "NAO reabre" in str(e.value)


def test_abordagem_nao_pode_ser_APAGADA(conn):
    abordagem_mod.registrar(conn, params=P41)
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("DELETE FROM abordagem")
    assert "devolveria a porta" in str(e.value)


# ===========================================================================
# A admissão: ela SEMPRE recusa, e nomeia a razão mais específica
# ===========================================================================


def test_admitir_recusa_TODA_candidata_na_0C(conn):
    r = admissao.admitir(
        conn, hypothesis_id=1, params=P41, agent_id="transacao@0c"
    )
    assert "NENHUMA CANDIDATA ENTRA" in r.motivo
    assert "ADR 0034" in r.motivo


def test_o_B3_e_recusado_como_BASELINE_e_nao_como_candidata(conn):
    """Critério 4: ele atravessa o encanamento e NÃO é promovido.

    E a razão nomeada é a certa: baseline. Dizer "nenhuma candidata entra na
    0C" seria verdade e esconderia que o B3 nunca poderia entrar de todo modo.
    """
    r = admissao.admitir(
        conn, hypothesis_id=1,
        params={"familia": "cruzamento_medias", "rapida": 20, "lenta": 50,
                "position_fraction_bps": 10_000, "stop_loss_bps": None},
        agent_id="baseline-B3",
    )
    assert "BASELINE" in r.motivo
    assert "CONTROLE NEGATIVO" in r.motivo
    assert "sem pre-registro" in r.motivo
    assert "contador do DSR" in r.motivo


def test_a_recusa_nomeia_a_razao_MAIS_ESPECIFICA(conn):
    """Abordagem descartada é mais específico que "nenhuma candidata entra"."""
    hid = _hipotese_minima(conn, "h-esp")
    abordagem_mod.rejeitar(
        conn, params=P41, hypothesis_id=int(hid), motivo="Portao B",
    )
    r = admissao.admitir(
        conn, hypothesis_id=1, params={**P41, "lenta": 210},
        agent_id="transacao@0c",
    )
    assert "DESCARTADA" in r.motivo
    assert "NENHUMA CANDIDATA ENTRA" not in r.motivo


def test_admitir_NAO_TEM_ramo_que_deixa_passar(conn):
    """A garantia estrutural: não há caso em que ele admita.

    E não é um `if fase == "0C"`: a recusa é o corpo inteiro da função. Quando
    a 0C acabar, alguém tem de vir escrever o caminho de admissão e decidir o
    que ele exige - um `if` transformaria essa decisão em consequência.
    """
    from pathlib import Path

    from tests._prosa import sql_sem_prosa

    # `sql_sem_prosa`, e não o texto cru: a primeira versão desta guarda
    # acusou o COMENTÁRIO que explica por que não há `if` sobre a fase - o
    # mesmo defeito que o incremento 11 registrou em `app/estatistica` e o 17
    # no detector de regimes. Uma guarda que proíbe a explicação de por que
    # algo é proibido empurra para apagar a explicação.
    # ESTRUTURAL, e não a palavra: a primeira versão procurava "fase" no
    # código e acusava a própria MENSAGEM de recusa, que cita "insumo da
    # fase". `sql_sem_prosa` mantém literais de uma linha de propósito - é
    # onde o SQL vive -, então a mensagem sobrevive à limpeza.
    #
    # A guarda certa é: o módulo NÃO IMPORTA `app.fase`. Se ele não consegue
    # ver a fase, não há como ramificar sobre ela - propriedade em vez de
    # inspeção de texto.
    codigo = sql_sem_prosa(Path("app/quarentena/admissao.py"))
    assert "import fase" not in codigo, (
        "a recusa não pode depender de um `if` sobre a fase: o módulo não "
        "pode nem enxergá-la"
    )
    assert " fase_mod" not in codigo and "fase.FASE" not in codigo
    # Não-vacuidade: a guarda casa a forma que procura.
    assert "import fase" in "from .. import fase"
    for agente in ("transacao@0c", "baseline-B3", "b4-0001", "qualquer"):
        r = admissao.admitir(
            conn, hypothesis_id=1, params=P41, agent_id=agente
        )
        assert isinstance(r, admissao.Recusa)


# ===========================================================================
# Garantia 4: os limiares congelados
# ===========================================================================


def test_os_limiares_sao_congelados_UMA_vez(conn):
    from app.config import service as config_service

    v = config_service.versao_atual(conn)
    a = admissao.congelar_limiares(
        conn, config_version_id=v.id, periodo_minimo_barras=2_688,
        regimes_minimos=2, magnitude_minima_ppm=500_000,
        reserva_maxima_barras=8_064,
    )
    b = admissao.congelar_limiares(
        conn, config_version_id=v.id, periodo_minimo_barras=1,
        regimes_minimos=1, magnitude_minima_ppm=1, reserva_maxima_barras=1,
    )
    assert a == b, "a segunda chamada mudou os limiares"
    assert b.periodo_minimo_barras == 2_688


def test_reduzir_limiar_e_recusado_pelo_BANCO(conn):
    from app.config import service as config_service

    v = config_service.versao_atual(conn)
    admissao.congelar_limiares(
        conn, config_version_id=v.id, periodo_minimo_barras=2_688,
        regimes_minimos=2, magnitude_minima_ppm=500_000,
        reserva_maxima_barras=8_064,
    )
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("UPDATE quarentena_limiar SET magnitude_minima_ppm = 1")
    assert "ajustar a regua ao resultado" in str(e.value)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM quarentena_limiar")


def test_avaliar_SEM_limiar_congelado_e_recusado(conn):
    """Congelar depois de ver o forward é escolher a régua olhando o número."""
    with pytest.raises(admissao.LimiarNaoCongelado) as e:
        admissao.exigir_limiares(conn, 999)
    assert "ANTES do primeiro tick" in str(e.value)
