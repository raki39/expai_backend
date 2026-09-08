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


# ===========================================================================
# CRITÉRIO 3: o in-sample vem do run CONGELADO, nunca recalculado (R74)
# ===========================================================================


def _regra(timeframe: str = "15m", lenta: int = 200):
    from app.regra.schema import CondicoesValidade, CruzamentoMedias, Regra

    return Regra(
        params=CruzamentoMedias(rapida=50, lenta=lenta),
        position_fraction_bps=8_000,
        stop_loss_bps=2_000,
        condicoes_validade=CondicoesValidade(
            venue="binance", symbol="BTCUSDT", timeframe=timeframe,
            fidelity_level=1, regimes_elegiveis=("vol_baixa", "vol_alta"),
            regimes_minimos=2, regime_permanencia_barras=672,
        ),
    )


@pytest.fixture
def cenario_congelavel(conn: sqlite3.Connection):
    """Uma hipótese e um run com dataset, prontos para congelar."""
    from app.ledger import livro
    from app.maos_rapidas import baselines, executor
    from app.regra import registro
    from tests.test_creditos import _hipotese
    from tests.test_maos_rapidas import precos_passeio
    from tests.test_simulador import criar_dataset

    dataset_id = criar_dataset(conn, precos_passeio(3_000))
    versao = conn.execute(
        "SELECT MIN(id) AS id FROM config_version"
    ).fetchone()["id"]
    run_id, _ = livro.abrir_run(
        conn, config_version_id=int(versao),
        seed_capital_usd_cents=100_000, agent_id="in-sample",
    )
    regra = _regra()
    rule_id = registro.registrar(conn, regra)
    executor.rodar(
        conn, run_id=run_id, dataset_id=dataset_id, regra=regra,
        rule_id=rule_id, config=baselines.regra_b3.__globals__["ExperimentConfig"](),
    )
    livro.encerrar_run(conn, run_id, "concluido")
    hid = _hipotese(conn, run_id, hash_="h-congelar")
    return {"hypothesis_id": hid, "run_id": run_id, "regra": regra}


def test_o_in_sample_e_CONGELADO_com_os_sete_campos(conn, cenario_congelavel):
    """Um id sozinho aponta para uma linha que pode ter sido reescrita ao
    redor. O que amarra o resultado é o CONJUNTO."""
    from app.quarentena import congelado as cong

    c = cong.congelar(
        conn, hypothesis_id=cenario_congelavel["hypothesis_id"],
        run_id=cenario_congelavel["run_id"], regra=cenario_congelavel["regra"],
        metrica_primaria="excesso_sobre_b3_cents", metrica_valor_cents=100_000,
    )
    assert c.run_digest and len(c.run_digest) == 64
    assert c.content_hash == "h-congelar"
    assert "cruzamento_medias" in c.abordagem_assinatura
    assert c.abordagem_versao == cong.ASSINATURA_VERSAO
    assert "+" in c.identidade_executavel, "config_hash + perfil"
    assert (c.dataset_sha256 is None) != (c.snapshot_sha256 is None), (
        "exatamente uma fonte, nunca as duas"
    )
    assert c.timeframe == "15m"


def test_o_congelado_NAO_SE_SUBSTITUI_por_um_run_mais_favoravel(
    conn, cenario_congelavel
):
    """A palavra do usuário: não pode ser substituído depois por outro mais
    favorável. Trocar a base de comparação é trocar o critério."""
    from app.ledger import livro
    from app.quarentena import congelado as cong

    cong.congelar(
        conn, hypothesis_id=cenario_congelavel["hypothesis_id"],
        run_id=cenario_congelavel["run_id"], regra=cenario_congelavel["regra"],
        metrica_primaria="excesso_sobre_b3_cents", metrica_valor_cents=100_000,
    )
    versao = conn.execute(
        "SELECT MIN(id) AS id FROM config_version"
    ).fetchone()["id"]
    outro, _ = livro.abrir_run(
        conn, config_version_id=int(versao),
        seed_capital_usd_cents=100_000, agent_id="in-sample-melhor",
    )
    livro.encerrar_run(conn, outro, "concluido")

    with pytest.raises(cong.JaCongelado) as e:
        cong.congelar(
            conn, hypothesis_id=cenario_congelavel["hypothesis_id"],
            run_id=outro, regra=cenario_congelavel["regra"],
            metrica_primaria="excesso_sobre_b3_cents",
            metrica_valor_cents=999_999,
        )
    assert "Nao se substitui" in str(e.value) or "nao se substitui" in str(e.value).lower()
    assert "mais favoravel" in str(e.value)


def test_o_congelado_e_IMUTAVEL_no_banco(conn, cenario_congelavel):
    from app.quarentena import congelado as cong

    cong.congelar(
        conn, hypothesis_id=cenario_congelavel["hypothesis_id"],
        run_id=cenario_congelavel["run_id"], regra=cenario_congelavel["regra"],
        metrica_primaria="excesso_sobre_b3_cents", metrica_valor_cents=100_000,
    )
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute(
            "UPDATE quarentena_congelado SET metrica_valor_cents = 1"
        )
    assert "olhando o resultado" in str(e.value)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM quarentena_congelado")


def test_conferir_RECUSA_divergencia_de_identidade(conn, cenario_congelavel):
    """Config ou perfil de calibração mudaram, e o preço executado com eles.

    Não é aviso: é recusa. Avaliar com divergência compararia o forward contra
    um in-sample que não é o que gerou a hipótese.
    """
    from app.calibracao import perfil as perfil_mod
    from app.quarentena import congelado as cong

    cong.congelar(
        conn, hypothesis_id=cenario_congelavel["hypothesis_id"],
        run_id=cenario_congelavel["run_id"], regra=cenario_congelavel["regra"],
        metrica_primaria="excesso_sobre_b3_cents", metrica_valor_cents=100_000,
    )
    assert cong.conferir(conn, cenario_congelavel["hypothesis_id"])

    # Um perfil aparece no run DEPOIS do congelamento. `run` é imutável, então
    # o caminho normal não permite isso - por SQL cru, para exercitar a
    # conferência.
    p = perfil_mod.gravar(
        conn, spread_bps_base_x1000=1_000,
        overrides=[perfil_mod.Override("vol_baixa", 1_674, 500, 163)],
    )
    conn.execute("DROP TRIGGER IF EXISTS run_sem_update")
    conn.execute(
        "UPDATE run SET calibracao_perfil_hash = ? WHERE id = ?",
        (p.hash, cenario_congelavel["run_id"]),
    )
    with pytest.raises(cong.DivergenciaDoCongelado) as e:
        cong.conferir(conn, cenario_congelavel["hypothesis_id"])
    assert "identidade_executavel" in str(e.value)
    assert "preco executado" in str(e.value)


def test_conferir_RECUSA_versao_de_assinatura_diferente(
    conn, cenario_congelavel, monkeypatch
):
    """Se o esquema da assinatura mudar, "cruzamento_medias com stop" passa a
    nomear outra coisa - e o congelado sobreviveria calado."""
    from app.quarentena import congelado as cong

    cong.congelar(
        conn, hypothesis_id=cenario_congelavel["hypothesis_id"],
        run_id=cenario_congelavel["run_id"], regra=cenario_congelavel["regra"],
        metrica_primaria="excesso_sobre_b3_cents", metrica_valor_cents=100_000,
    )
    monkeypatch.setattr(cong, "ASSINATURA_VERSAO", 2)
    with pytest.raises(cong.DivergenciaDoCongelado) as e:
        cong.conferir(conn, cenario_congelavel["hypothesis_id"])
    assert "abordagem_versao" in str(e.value)
    assert "nao descreve mais o mecanismo" in str(e.value)


def test_conferir_SEM_congelado_recusa_em_vez_de_recalcular(conn):
    from app.quarentena import congelado as cong

    with pytest.raises(cong.DivergenciaDoCongelado) as e:
        cong.conferir(conn, 999)
    assert "ANTES do primeiro tick" in str(e.value)


def test_a_avaliacao_NAO_recalcula_o_in_sample():
    """R74, como guarda de código: o valor vem do congelado.

    `veredito.avaliar` recebe `metrica_in_sample_cents` como MEDIÇÃO - ela não
    tem conexão, não tem `conn` e não consegue recalcular nada.
    """
    import inspect

    from app.quarentena import veredito

    assinatura = inspect.signature(veredito.avaliar)
    assert set(assinatura.parameters) == {"m", "lim"}
    assert "conn" not in inspect.getsource(veredito.avaliar)
    assert "sqlite3" not in inspect.getsource(veredito)


# ===========================================================================
# CRITÉRIO 5: timeframe cria hipótese nova, e NÃO abordagem nova (R75)
# ===========================================================================


def test_trocar_o_TIMEFRAME_cria_hipotese_nova():
    """`content_hash` cobre `condicoes_validade`, e o timeframe está lá."""
    a = _regra(timeframe="15m")
    b = _regra(timeframe="1h")
    assert a.hash() != b.hash(), (
        "mudar o timeframe tem de produzir hipótese nova, com crédito novo"
    )


def test_trocar_o_timeframe_NAO_cria_abordagem_nova():
    """A distinção que o usuário fixou.

    O mecanismo é o mesmo: cruzamento de médias com stop. Mudar a grade em que
    ele roda muda a hipótese - não muda o que a estratégia FAZ.
    """
    a = abordagem_mod.assinatura_de_regra(_regra(timeframe="15m"))
    b = abordagem_mod.assinatura_de_regra(_regra(timeframe="1h"))
    assert a == b, (
        "o timeframe entrou na assinatura estrutural: uma estratégia rejeitada "
        "trocaria só a grade e entraria como abordagem nova"
    )
    assert "timeframe" not in a and "15m" not in a and "1h" not in a


def test_a_rejeitada_NAO_entra_trocando_so_o_TIMEFRAME(conn):
    """O disfarce que a garantia 1 e o critério 5 fecham juntos.

    Ela precisa voltar à 0B, mantendo a linhagem e a contabilização das
    tentativas - e não reentrar aqui com outra grade.
    """
    hid = _hipotese_minima(conn, "h-tf")
    r15 = _regra(timeframe="15m")
    abordagem_mod.rejeitar(
        conn,
        params=r15.params.model_dump(mode="json"),
        extras={"position_fraction_bps": r15.position_fraction_bps,
                "stop_loss_bps": r15.stop_loss_bps},
        hypothesis_id=hid, motivo="Portao B: cinco criterios independentes",
    )

    r1h = _regra(timeframe="1h")
    with pytest.raises(abordagem_mod.AbordagemRejeitada) as e:
        abordagem_mod.exigir_nao_rejeitada(
            conn,
            params=r1h.params.model_dump(mode="json"),
            extras={"position_fraction_bps": r1h.position_fraction_bps,
                    "stop_loss_bps": r1h.stop_loss_bps},
        )
    assert "DESCARTADA" in str(e.value)
    assert "0B" in str(e.value)


# ===========================================================================
# Garantia 4: a assinatura é do SISTEMA, e o agente não a fabrica
# ===========================================================================


def test_a_assinatura_vem_da_REGRA_VALIDADA_e_nao_de_um_dict_livre():
    """O agente não pode declarar campo a mais: `extra="forbid"` na família e
    na regra, e `Params` é união DISCRIMINADA sobre o catálogo fechado."""
    import pydantic
    import pytest as _pytest

    from app.regra.schema import CruzamentoMedias, Regra

    with _pytest.raises(pydantic.ValidationError):
        CruzamentoMedias(rapida=50, lenta=200, campo_inventado=1)
    with _pytest.raises(pydantic.ValidationError):
        Regra(
            params=CruzamentoMedias(rapida=50, lenta=200),
            condicoes_validade=_regra().condicoes_validade,
            campo_inventado=1,
        )


def test_familia_FORA_do_catalogo_nao_fabrica_abordagem(conn):
    """Sem isto, qualquer texto novo produziria uma abordagem nova - e
    "abordagem nova" é o que o bloqueio existe para impedir que se fabrique."""
    with pytest.raises(abordagem_mod.FamiliaDesconhecida) as e:
        abordagem_mod.assinatura_de({"familia": "tese_genial_nova", "x": 1})
    assert "catalogo fechado" in str(e.value)


def test_o_catalogo_da_abordagem_bate_com_o_SCHEMA():
    """Duas listas fechadas em arquivos diferentes divergem.

    O incremento 11b registrou isso com as categorias de parada: a lista do
    Python e a do gatilho comparadas por teste, porque a divergência derruba o
    run com `IntegrityError` DEPOIS de o dinheiro ter saído.
    """
    from app.regra.schema import BandaDesvio, BreakoutCanal, CruzamentoMedias

    do_schema = {
        m.model_fields["familia"].default
        for m in (CruzamentoMedias, BandaDesvio, BreakoutCanal)
    }
    assert set(abordagem_mod.FAMILIAS_DO_CATALOGO) == do_schema


def test_campo_declarado_e_NAO_USADO_nao_fabrica_abordagem_nova():
    """`stop_loss_bps=None` é ausência, e o schema não aceita zero.

    Então não existe "declarado mas não usado" com valor neutro - a única
    diferença possível é presença contra ausência, e essa É de mecanismo.
    """
    import pydantic
    import pytest as _pytest

    from app.regra.schema import CruzamentoMedias, Regra

    with _pytest.raises(pydantic.ValidationError):
        Regra(
            params=CruzamentoMedias(rapida=50, lenta=200),
            stop_loss_bps=0,
            condicoes_validade=_regra().condicoes_validade,
        )

    com = abordagem_mod.assinatura_de_regra(_regra())
    sem = abordagem_mod.assinatura_de_regra(
        Regra(
            params=CruzamentoMedias(rapida=50, lenta=200),
            position_fraction_bps=8_000,
            condicoes_validade=_regra().condicoes_validade,
        )
    )
    assert com != sem, "tirar o stop muda o MECANISMO, e não só o valor"
    assert "stop_loss_bps" in com and "stop_loss_bps" not in sem


# ===========================================================================
# Garantia 5 do ADR 0034: o relatório DECLARA a ausência
# ===========================================================================

CABECALHO_Q = {"Authorization": "Bearer token-de-teste-0a"}


def test_o_relatorio_DECLARA_que_nenhuma_candidata_foi_admitida(client):
    """Uma ausência que ninguém declara vira silêncio, e silêncio é lido como
    esquecimento."""
    r = client.get("/api/relatorio/quarentena", headers=CABECALHO_Q)
    assert r.status_code == 200
    c = r.json()
    assert c["existe"] is True
    assert c["nenhuma_candidata_admitida"] is True
    assert c["candidatas_admitidas"] == 0
    assert "ADR 0034" in c["decisao"]
    assert "Portao B rejeitou" in c["por_que"]


def test_o_relatorio_diz_que_o_B3_foi_SO_controle_negativo(client):
    r = client.get("/api/relatorio/quarentena", headers=CABECALHO_Q)
    b3 = r.json()["b3"]
    assert b3["papel"] == "CONTROLE NEGATIVO do proprio encanamento"
    assert b3["tem_pre_registro"] is False
    assert b3["consome_credito"] is False
    assert b3["entra_em_familia"] is False
    assert b3["conta_no_dsr"] is False
    assert b3["pode_ser_promovido"] is False
    assert "estrutural" in b3["fronteira"]


def test_o_relatorio_lista_o_MOTIVO_de_cada_exclusao(client, conn):
    hid = _hipotese_minima(conn, "h-relatorio")
    r15 = _regra()
    abordagem_mod.rejeitar(
        conn,
        params=r15.params.model_dump(mode="json"),
        extras={"position_fraction_bps": r15.position_fraction_bps,
                "stop_loss_bps": r15.stop_loss_bps},
        hypothesis_id=hid,
        motivo="Portao B: cinco criterios independentes",
    )
    corpo = client.get("/api/relatorio/quarentena", headers=CABECALHO_Q).json()
    rejeitadas = corpo["abordagens_rejeitadas"]
    assert len(rejeitadas) == 1
    a = rejeitadas[0]
    assert "cruzamento_medias" in a["assinatura"]
    assert "cinco criterios" in a["motivo"]
    assert a["bloqueia_variacao_parametrica"] is True
    assert a["bloqueia_variacao_textual"] is True
    assert a["bloqueia_troca_de_timeframe"] is True
    assert "0B" in a["volta_legitima"]


def test_a_declaracao_e_DERIVADA_de_consulta_e_nao_uma_frase(client, conn):
    """Se fosse frase, sobreviveria intacta ao dia em que uma candidata
    entrasse - e é o mesmo argumento das onze condições do Portão A."""
    from app.quarentena import congelado as cong

    antes = client.get(
        "/api/relatorio/quarentena", headers=CABECALHO_Q
    ).json()
    assert antes["nenhuma_candidata_admitida"] is True

    # Um congelado aparece (só a suíte consegue: `admitir` recusa sempre).
    from app.ledger import livro
    from app.maos_rapidas import baselines, executor
    from app.regra import registro
    from tests.test_creditos import _hipotese
    from tests.test_maos_rapidas import precos_passeio
    from tests.test_simulador import criar_dataset

    dataset_id = criar_dataset(conn, precos_passeio(3_000))
    versao = conn.execute(
        "SELECT MIN(id) AS id FROM config_version"
    ).fetchone()["id"]
    run_id, _ = livro.abrir_run(
        conn, config_version_id=int(versao),
        seed_capital_usd_cents=100_000, agent_id="in-sample-declaracao",
    )
    regra = _regra()
    executor.rodar(
        conn, run_id=run_id, dataset_id=dataset_id, regra=regra,
        rule_id=registro.registrar(conn, regra),
        config=baselines.regra_b3.__globals__["ExperimentConfig"](),
    )
    livro.encerrar_run(conn, run_id, "concluido")
    hid = _hipotese(conn, run_id, hash_="h-declaracao")
    cong.congelar(
        conn, hypothesis_id=hid, run_id=run_id, regra=regra,
        metrica_primaria="excesso_sobre_b3_cents", metrica_valor_cents=1,
    )

    depois = client.get(
        "/api/relatorio/quarentena", headers=CABECALHO_Q
    ).json()
    assert depois["nenhuma_candidata_admitida"] is False, (
        "a declaração não acompanhou o dado: ela é frase, e não consulta"
    )
    assert depois["candidatas_admitidas"] == 1
    assert depois["veredito"] is None
    assert depois["conferencia_do_congelado"][0]["confere"] is True
