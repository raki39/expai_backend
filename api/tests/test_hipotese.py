"""Testes do incremento 8: o pre-registro da secao 8.2.

O que este arquivo tem de provar nao e que o modulo Python recusa o que deve
recusar - e que o **banco** recusa. Por isso quase toda proibicao aqui e
testada com SQL cru: se a regra morasse no Python, um defeito nele mascararia
a ausencia dela no schema, e a suite passaria a provar que o modulo esta
correto em vez de provar que o dado esta protegido.

E o mesmo desenho das partidas dobradas do incremento 2.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from pydantic import ValidationError

from app.hipotese import poder
from app.hipotese import registro as hipotese_registro
from app.hipotese.schema import PreRegistroBruto, hash_do_conteudo

QUINZE_MIN_MS = 900_000

CONDICOES = {
    "venue": "binance",
    "symbol": "BTCUSDT",
    "timeframe": "15m",
    "fidelity_level": 1,
}

BRUTO_OK = {
    "enunciado": "cruzamento lento opera pouco e supera a mediana do acaso",
    "metrica_primaria": "excesso_sobre_b1_p50_cents",
    "efeito_minimo": 5_000,
    "sharpe_esperado_milesimos": 3_000,
    "criterio_parada": "fim_da_janela",
    "condicoes_falseamento": [
        {
            "metrica": "excesso_sobre_b1_p50_cents",
            "comparador": "menor_que",
            "valor": 5_000,
        }
    ],
}


def _pre(**mudancas) -> PreRegistroBruto:
    return PreRegistroBruto.model_validate({**BRUTO_OK, **mudancas})


@pytest.fixture
def evento(conn: sqlite3.Connection) -> tuple[int, int]:
    """Um run e um evento cognitivo aos quais pendurar a hipotese."""
    conn.execute(
        "INSERT INTO run (agent_id, state, config_version_id, created_at,"
        " updated_at) VALUES ('agent-0001','executando',1,'2026-09-03',"
        "'2026-09-03')"
    )
    run_id = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    conn.execute(
        "INSERT INTO agent_event (run_id, occurred_at, node, kind,"
        " cost_usd_minor, cost_usd_micro)"
        " VALUES (?, '2026-09-03', 'propor_regra', 'proposta', 0, 0)",
        (run_id,),
    )
    event_id = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    return run_id, event_id


def _inserir_cru(conn: sqlite3.Connection, evento, **campos) -> None:
    """INSERT direto, sem passar pelo modulo. E o ponto do teste."""
    run_id, event_id = evento
    linha = {
        "run_id": run_id,
        "agent_event_id": event_id,
        "enunciado": "uma afirmacao qualquer",
        "agente_origem": "transacao@0b",
        "timestamp_registro": "2026-09-03T00:00:00+00:00",
        "metrica_primaria": "excesso_sobre_b1_p50_cents",
        "efeito_minimo": 0,
        "n_minimo": 100,
        "sharpe_esperado_milesimos": 3_000,
        "criterio_parada": "fim_da_janela",
        "condicoes_validade_json": json.dumps(CONDICOES),
        "condicoes_falseamento_json": json.dumps(
            [
                {
                    "metrica": "excesso_sobre_b1_p50_cents",
                    "comparador": "menor_que",
                    "valor": 0,
                }
            ]
        ),
        "testavel": 1,
        "motivo_nao_testavel": None,
        "horizonte_barras": 50_000,
        "rule_id": None,
        "supersedes": None,
        "content_hash": "abc123",
        **campos,
    }
    colunas = ", ".join(linha)
    marcas = ", ".join("?" for _ in linha)
    conn.execute(
        f"INSERT INTO hypothesis ({colunas}) VALUES ({marcas})",
        tuple(linha.values()),
    )


# ===========================================================================
# CRITERIO 5 - `n_minimo` calculado por poder, conferido contra o DOCUMENTO
# ===========================================================================


@pytest.mark.parametrize(
    "sharpe_milesimos,anos_esperados",
    [(500, 16.0), (1_000, 4.0), (2_000, 1.0), (3_000, 0.4444)],
)
def test_n_minimo_reproduz_a_tabela_da_secao_8_3(
    sharpe_milesimos: int, anos_esperados: float
) -> None:
    """A secao 8.3 publica a tabela; a implementacao tem de bater com ela.

    | Sharpe | tempo para t > 2 |
    |---|---|
    | 0,5 | ~16 anos | 1,0 | ~4 anos | 2,0 | ~1 ano | 3,0 | ~5 meses |

    Conferir contra o documento, e nao contra o proprio resultado, e a
    diferenca entre um teste e um carimbo.
    """
    n = poder.n_minimo(
        sharpe_milesimos=sharpe_milesimos, duracao_barra_ms=QUINZE_MIN_MS
    )
    anos = n / poder.barras_por_ano(QUINZE_MIN_MS)
    assert abs(anos - anos_esperados) < 0.01


def test_barras_por_ano_vem_do_timeframe_e_nao_de_uma_constante() -> None:
    """Fixar 35.064 faria o numero parar de descrever se o timeframe mudasse."""
    assert poder.barras_por_ano(QUINZE_MIN_MS) == 35_064
    assert poder.barras_por_ano(3_600_000) == 8_766        # 1h
    assert poder.barras_por_ano(86_400_000) == 365         # 1d


def test_sharpe_declarado_maior_pede_menos_amostra() -> None:
    """A relacao que o prompt promete ao modelo tem de ser verdade."""
    anterior = None
    for s in (1_000, 1_500, 2_000, 3_000, 5_000):
        n = poder.n_minimo(sharpe_milesimos=s, duracao_barra_ms=QUINZE_MIN_MS)
        if anterior is not None:
            assert n < anterior, "Sharpe maior tem de exigir amostra menor"
        anterior = n


def test_sharpe_minimo_testavel_e_o_menor_que_de_fato_cabe() -> None:
    """Minimalidade: um milesimo abaixo dele ja nao cabe.

    Sem esta conferencia, um `sharpe_minimo_testavel` conservador demais
    passaria despercebido e o prompt mentiria ao modelo por excesso.
    """
    for horizonte in (2_500, 21_024, 56_064, 70_080):
        s = poder.sharpe_minimo_testavel(
            duracao_barra_ms=QUINZE_MIN_MS, horizonte_barras=horizonte
        )
        cabe = poder.n_minimo(
            sharpe_milesimos=s, duracao_barra_ms=QUINZE_MIN_MS
        )
        nao_cabe = poder.n_minimo(
            sharpe_milesimos=s - 1, duracao_barra_ms=QUINZE_MIN_MS
        )
        assert cabe <= horizonte < nao_cabe


def test_o_numero_desconfortavel_da_fase_esta_medido() -> None:
    """No in-sample da D27, so Sharpe >= 2,58 e testavel.

    Este teste existe para o numero nao poder mudar em silencio. Ele nao e
    defeito da conta: e a secao 8.3 dizendo, com numero, que "apenas efeitos
    grandes sao detectaveis no horizonte do projeto". Se algum dia ele cair,
    foi porque a janela ou o timeframe mudou - e isso e material.
    """
    assert (
        poder.sharpe_minimo_testavel(
            duracao_barra_ms=QUINZE_MIN_MS, horizonte_barras=21_024
        )
        == 2_583
    )


def test_autocorrelacao_desconta_a_amostra_e_nunca_a_premia() -> None:
    """Secao 8.3: mil candles autocorrelacionados nao sao mil observacoes.

    E o limite por cima: autocorrelacao negativa nao credita amostra extra.
    Descontar quando ha dependencia e conservador; premiar quando parece nao
    haver e apostar numa estimativa ruidosa (regra 9).
    """
    persistente = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19] * 5
    ruido = [5, -5] * 25

    com_dependencia = poder.efetivo_de_bruto(persistente, 1_000)
    assert com_dependencia.autocorrelacao_ppm > 0
    assert com_dependencia.efetivo < 1_000

    alternante = poder.efetivo_de_bruto(ruido, 1_000)
    assert alternante.autocorrelacao_ppm < 0
    assert alternante.efetivo <= 1_000, "autocorrelacao negativa nao premia"

    # Serie curta ou constante: sem evidencia de dependencia, sem desconto
    # inventado.
    assert poder.efetivo_de_bruto([], 500).efetivo == 500
    assert poder.efetivo_de_bruto([7, 7, 7, 7], 500).efetivo == 500


def test_hipotese_que_nao_cabe_no_horizonte_nasce_nao_testavel(
    conn: sqlite3.Connection, evento
) -> None:
    """R35 / secao 8.3: arquivada com motivo, em vez de testada mal."""
    run_id, event_id = evento
    hid, testavel = hipotese_registro.registrar(
        conn,
        run_id=run_id,
        agent_event_id=event_id,
        bruto=_pre(sharpe_esperado_milesimos=500),  # 16 anos de amostra
        condicoes_validade=CONDICOES,
        duracao_barra_ms=QUINZE_MIN_MS,
        horizonte_barras=21_024,
    )
    assert testavel is False
    lido = hipotese_registro.por_id(conn, hid)
    assert lido["testavel"] is False
    assert "nao cabe no horizonte" in lido["motivo_nao_testavel"]
    assert lido["n_minimo"] > lido["horizonte_barras"]


def test_o_banco_recusa_nao_testavel_disfarcada_de_testavel(
    conn: sqlite3.Connection, evento
) -> None:
    """A metade da triagem que o schema consegue impor sozinho.

    Se o calculo de poder for contornado algum dia, a linha nao entra.
    """
    with pytest.raises(sqlite3.IntegrityError):
        _inserir_cru(
            conn, evento, n_minimo=500_000, horizonte_barras=1_000, testavel=1
        )


def test_motivo_e_obrigatorio_quando_nao_testavel_e_proibido_quando_e(
    conn: sqlite3.Connection, evento
) -> None:
    """`None` e `False` de novo: um motivo sobrando descreve o que nao existe."""
    with pytest.raises(sqlite3.IntegrityError):
        _inserir_cru(
            conn, evento, testavel=0, motivo_nao_testavel=None,
            n_minimo=500_000, horizonte_barras=1_000,
        )
    with pytest.raises(sqlite3.IntegrityError):
        _inserir_cru(
            conn, evento, testavel=1, motivo_nao_testavel="sobrando"
        )


# ===========================================================================
# CRITERIO 1 - os dez campos, e o falseamento imposto pelo BANCO
# ===========================================================================


def test_o_banco_recusa_hipotese_sem_condicao_de_falseamento(
    conn: sqlite3.Connection, evento
) -> None:
    """Secao 8.2: "uma hipotese que nao pode ser refutada nao entra".

    `NOT NULL` sozinho nao cumpre isso, e e por isso que o teste passa pelos
    tres casos: nulo, vazio e array vazio. Os dois ultimos passariam por
    `NOT NULL` sem refutar coisa alguma.
    """
    for valor in (None, "", "[]"):
        with pytest.raises(sqlite3.IntegrityError):
            _inserir_cru(conn, evento, condicoes_falseamento_json=valor)


def test_o_banco_recusa_falseamento_que_nao_e_json_de_array(
    conn: sqlite3.Connection, evento
) -> None:
    for valor in ("nao e json", '{"metrica": "x"}', '"texto"'):
        with pytest.raises(sqlite3.IntegrityError):
            _inserir_cru(conn, evento, condicoes_falseamento_json=valor)


def test_o_banco_recusa_metrica_fora_do_enum_fechado(
    conn: sqlite3.Connection, evento
) -> None:
    """Metrica livre tornaria a familia estatistica incoerente (secao 8.6)."""
    with pytest.raises(sqlite3.IntegrityError):
        _inserir_cru(conn, evento, metrica_primaria="sharpe_bonito")


def test_o_banco_recusa_enunciado_e_agente_vazios(
    conn: sqlite3.Connection, evento
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _inserir_cru(conn, evento, enunciado="")
    with pytest.raises(sqlite3.IntegrityError):
        _inserir_cru(conn, evento, agente_origem="")


def test_os_dez_campos_da_secao_8_2_estao_todos_gravados(
    conn: sqlite3.Connection, evento
) -> None:
    """Um a um, contra a tabela do documento."""
    run_id, event_id = evento
    hid, _ = hipotese_registro.registrar(
        conn,
        run_id=run_id,
        agent_event_id=event_id,
        bruto=_pre(),
        condicoes_validade=CONDICOES,
        duracao_barra_ms=QUINZE_MIN_MS,
        horizonte_barras=56_064,
    )
    h = hipotese_registro.por_id(conn, hid)

    assert h["id"] == hid                                    # 1
    assert h["enunciado"] == BRUTO_OK["enunciado"]           # 2
    assert h["agente_origem"] == "transacao@0b"              # 3
    assert h["timestamp_registro"]                           # 4
    assert h["metrica_primaria"] == "excesso_sobre_b1_p50_cents"  # 5
    assert h["efeito_minimo"] == 5_000                       # 6
    assert h["n_minimo"] == 15_584                           # 7 - calculado
    assert h["criterio_parada"] == "fim_da_janela"           # 8
    assert h["condicoes_validade"] == CONDICOES              # 9
    assert len(h["condicoes_falseamento"]) == 1              # 10


def test_condicoes_validade_vem_da_config_e_nao_do_modelo() -> None:
    """Deixar o modelo declarar a propria procedencia e o oposto de procedencia.

    Mesma decisao que ja valia no `contrato.py` da 0A. O teste e por ausencia:
    o campo nao existe no que o modelo pode responder.
    """
    assert "condicoes_validade" not in PreRegistroBruto.model_fields
    assert "agente_origem" not in PreRegistroBruto.model_fields
    assert "n_minimo" not in PreRegistroBruto.model_fields


# ===========================================================================
# CRITERIO 2 - imutabilidade por gatilho, correcao por registro novo
# ===========================================================================


def test_pre_registro_recusa_update_e_delete(
    conn: sqlite3.Connection, evento
) -> None:
    """Secao 8.2: "o pre-registro e imutavel".

    O UPDATE proibido e o que importa: ele e o unico caminho pelo qual a
    metrica poderia ser ajustada depois de o resultado aparecer, que e
    exatamente o que o pre-registro existe para impedir.
    """
    run_id, event_id = evento
    hid, _ = hipotese_registro.registrar(
        conn, run_id=run_id, agent_event_id=event_id, bruto=_pre(),
        condicoes_validade=CONDICOES, duracao_barra_ms=QUINZE_MIN_MS,
        horizonte_barras=56_064,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE hypothesis SET efeito_minimo = 0 WHERE id = ?", (hid,)
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM hypothesis WHERE id = ?", (hid,))


def test_correcao_e_registro_novo_com_supersedes(
    conn: sqlite3.Connection, evento
) -> None:
    """O mesmo desenho do estorno no ledger: nada some, tudo se acrescenta."""
    run_id, event_id = evento
    primeira, _ = hipotese_registro.registrar(
        conn, run_id=run_id, agent_event_id=event_id, bruto=_pre(),
        condicoes_validade=CONDICOES, duracao_barra_ms=QUINZE_MIN_MS,
        horizonte_barras=56_064,
    )
    segunda, _ = hipotese_registro.registrar(
        conn, run_id=run_id, agent_event_id=event_id,
        bruto=_pre(efeito_minimo=9_000, condicoes_falseamento=[
            {
                "metrica": "excesso_sobre_b1_p50_cents",
                "comparador": "menor_que",
                "valor": 9_000,
            }
        ]),
        condicoes_validade=CONDICOES, duracao_barra_ms=QUINZE_MIN_MS,
        horizonte_barras=56_064, supersedes=primeira,
    )
    assert hipotese_registro.por_id(conn, segunda)["supersedes"] == primeira
    # A primeira continua la, intacta.
    assert hipotese_registro.por_id(conn, primeira)["efeito_minimo"] == 5_000


def test_uma_hipotese_nao_substitui_a_si_mesma(
    conn: sqlite3.Connection, evento
) -> None:
    run_id, event_id = evento
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO hypothesis (run_id, agent_event_id, enunciado,"
            " agente_origem, timestamp_registro, metrica_primaria,"
            " efeito_minimo, n_minimo, sharpe_esperado_milesimos,"
            " criterio_parada, condicoes_validade_json,"
            " condicoes_falseamento_json, testavel, horizonte_barras,"
            " content_hash, supersedes, id)"
            " VALUES (?,?,'x','a','t','idas_e_voltas',0,1,3000,"
            "'fim_da_janela','{}','[{}]',1,10,'h',7,7)",
            (run_id, event_id),
        )


# ===========================================================================
# CRITERIO 3 - o contrato de saida produz o pre-registro completo
# ===========================================================================


def test_falseamento_precisa_tocar_a_metrica_primaria() -> None:
    """Falsear so o que nao se mediu deixa a hipotese irrefutavel na pratica."""
    with pytest.raises(ValidationError, match="metrica primaria"):
        _pre(condicoes_falseamento=[
            {"metrica": "idas_e_voltas", "comparador": "maior_que", "valor": 9}
        ])


def test_falseamento_precisa_contradizer_o_efeito_minimo() -> None:
    """O caso concreto que motivou a regra.

    Declarar efeito minimo de 5.000 e falsear em "abaixo de -50.000" e
    declarar uma condicao que so um colapso total dispararia: formalmente ha
    falseamento, e na faixa que interessa a hipotese e irrefutavel.
    """
    with pytest.raises(ValidationError, match="efeito_minimo"):
        _pre(condicoes_falseamento=[
            {
                "metrica": "excesso_sobre_b1_p50_cents",
                "comparador": "menor_que",
                "valor": -50_000,
            }
        ])


def test_sharpe_fora_da_faixa_e_recusado() -> None:
    """O teto existe porque a conta de poder DIVIDE por Sharpe ao quadrado.

    Sharpe 50 pediria quatorze barras de amostra e aprovaria qualquer coisa.
    """
    with pytest.raises(ValidationError):
        _pre(sharpe_esperado_milesimos=50_000)
    with pytest.raises(ValidationError):
        _pre(sharpe_esperado_milesimos=0)


def test_o_schema_enviado_ao_provedor_cobre_os_seis_campos_do_modelo() -> None:
    """Camada 1 e dica, camada 2 e portao - mas a dica tem de estar completa.

    Um campo obrigatorio no pydantic e ausente no schema enviado produziria
    recusa a cada chamada, e o custo dessa descoberta e uma chamada paga.
    """
    from app.hipotese.schema import SCHEMA_PRE_REGISTRO

    exigidos = set(SCHEMA_PRE_REGISTRO["required"])
    do_modelo = set(PreRegistroBruto.model_fields)
    assert exigidos == do_modelo
    assert set(SCHEMA_PRE_REGISTRO["properties"]) == do_modelo


def test_o_contrato_da_proposta_carrega_o_pre_registro() -> None:
    """A frase da 0A virou `enunciado`, e tudo que a lia continua lendo."""
    from app.cerebro.contrato import SCHEMA_PROPOSTA, PropostaBruta

    assert "pre_registro" in PropostaBruta.model_fields
    assert "expectativa" not in PropostaBruta.model_fields
    assert "pre_registro" in SCHEMA_PROPOSTA["required"]

    bruta = PropostaBruta.model_validate(
        {
            "familia": "cruzamento_medias",
            "rapida": 20,
            "lenta": 50,
            "periodo": None,
            "desvios_milesimos": None,
            "position_fraction_bps": 3_000,
            "stop_loss_bps": None,
            "pre_registro": BRUTO_OK,
            "confianca_ppm": 300_000,
        }
    )
    # A propriedade devolve o enunciado: os cinco pontos que liam
    # `bruta.expectativa` na 0A continuam funcionando sem alteracao.
    assert bruta.expectativa == BRUTO_OK["enunciado"]


# ===========================================================================
# Hash de conteudo - o que torna o reteste reconhecivel (secao 8.6.1)
# ===========================================================================


def test_hash_e_do_conteudo_e_ignora_a_ordem_das_clausulas() -> None:
    """Duas gravacoes da mesma afirmacao SAO a mesma afirmacao."""
    a = _pre(
        condicoes_falseamento=[
            {
                "metrica": "excesso_sobre_b1_p50_cents",
                "comparador": "menor_que",
                "valor": 5_000,
            },
            {"metrica": "idas_e_voltas", "comparador": "maior_que", "valor": 900},
        ]
    )
    b = _pre(
        condicoes_falseamento=[
            {"metrica": "idas_e_voltas", "comparador": "maior_que", "valor": 900},
            {
                "metrica": "excesso_sobre_b1_p50_cents",
                "comparador": "menor_que",
                "valor": 5_000,
            },
        ]
    )
    assert hash_do_conteudo(a, CONDICOES, "transacao@0b") == hash_do_conteudo(
        b, CONDICOES, "transacao@0b"
    )


def test_mudar_um_parametro_muda_o_hash() -> None:
    """Reteste com parametro alterado custa 3 creditos, nao 1 (secao 8.6.1)."""
    base = hash_do_conteudo(_pre(), CONDICOES, "transacao@0b")
    outro = hash_do_conteudo(
        _pre(sharpe_esperado_milesimos=2_500), CONDICOES, "transacao@0b"
    )
    assert base != outro


def test_contador_de_tentativas_reconhece_a_mesma_hipotese(
    conn: sqlite3.Connection, evento
) -> None:
    """Secao 8.6: toda tentativa e registrada, e o contador nunca zera."""
    run_id, event_id = evento
    for _ in range(3):
        hipotese_registro.registrar(
            conn, run_id=run_id, agent_event_id=event_id, bruto=_pre(),
            condicoes_validade=CONDICOES, duracao_barra_ms=QUINZE_MIN_MS,
            horizonte_barras=56_064,
        )
    h = hipotese_registro.do_run(conn, run_id)
    assert hipotese_registro.tentativas_por_hash(conn, h["content_hash"]) == 3


# ===========================================================================
# A amostra vem das barras EXPOSTAS, nao da janela
# ===========================================================================


def test_amostra_conta_barras_com_posicao_e_nao_a_janela_inteira(
    conn: sqlite3.Connection, evento
) -> None:
    """Barra fora do mercado nao observa nada sobre a vantagem da regra.

    Contar a janela inteira afirmaria muito mais amostra do que existe, na
    direcao de aprovar - que e a direcao em que nao se erra de graca.
    """
    from app.maos_rapidas import executor

    run_id, _ = evento
    assert executor.barras_expostas(conn, run_id, QUINZE_MIN_MS) == 0


# ===========================================================================
# CRITERIO 6 - o custo do prefixo frio, MEDIDO
# ===========================================================================
#
# Gasta dinheiro de verdade, entao fica pulado por padrao - o mesmo
# interruptor dos dois testes de rede do incremento 5. Rodar a suite nao pode
# custar por descuido.
#
#   RODAR_TESTES_DE_REDE=1 python -m pytest -m rede -k prefixo


@pytest.mark.rede
@pytest.mark.skipif(
    __import__("os").getenv("RODAR_TESTES_DE_REDE", "") not in ("1", "true", "sim"),
    reason="criterio 6 gasta dinheiro de verdade; ligue com RODAR_TESTES_DE_REDE=1",
)
def test_o_custo_do_prefixo_novo_e_medido_e_nao_suposto(
    conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """D31 disse que mudar o schema esfria o cache. Aqui esse custo vira numero.

    A descoberta 5 do incremento 5 foi que **o schema de saida faz parte do
    prefixo cacheado**: sistema identico com schema diferente vem frio. O
    incremento 8 mudou o schema de `propor_regra` - trocou `expectativa` por
    `pre_registro` -, entao a primeira chamada sob o contrato novo escreve um
    prefixo novo, e escrita custa 1,25x a entrada.

    Isso era previsivel e foi previsto. O que nao era e QUANTO, e a secao 5.2
    proibe estimar: o numero tem de sair do `usage` real. Este teste o extrai
    e o imprime para ir ao `.aprendizado/`.

    Ele nao afirma um valor: afirma que o prefixo ENGAJOU o cache na primeira
    chamada e que a segunda LEU. Um numero cravado aqui envelheceria a cada
    ajuste de prompt, e seria a oitava vez que um valor para de descrever.
    """
    from pydantic import SecretStr

    from tests.test_cerebro import _chave_do_arquivo, _rodar_ciclo

    chave = _chave_do_arquivo("ANTHROPIC_API_KEY")
    if not chave:
        pytest.skip("ANTHROPIC_API_KEY ausente do .env do servico")

    reais = settings.model_copy(
        update={
            "anthropic_api_key": SecretStr(chave),
            "anthropic_workspace_id": _chave_do_arquivo("ANTHROPIC_WORKSPACE_ID"),
        }
    )
    primeiro = _rodar_ciclo(conn, cenario, reais, None)
    linhas = list(
        conn.execute(
            "SELECT node, tokens_in, tokens_cache_read, tokens_cache_write,"
            "       cost_usd_micro"
            "  FROM agent_event WHERE run_id = ? AND provider IS NOT NULL"
            " ORDER BY id",
            (primeiro.run_id,),
        )
    )
    assert linhas, f"o cerebro nao chamou: {primeiro.parou_em} / {primeiro.motivo}"

    print("\n--- custo do prefixo sob o contrato do incremento 8 ---")
    for l in linhas:
        print(
            f"  {l['node']:>18}: in={l['tokens_in']}"
            f" read={l['tokens_cache_read']} write={l['tokens_cache_write']}"
            f" custo_micro={l['cost_usd_micro']}"
        )
        engajou = (l["tokens_cache_write"] or 0) + (l["tokens_cache_read"] or 0)
        assert engajou > 0, (
            f"{l['node']}: o prefixo novo nao foi lido nem gravado. Ou ficou"
            " abaixo do minimo cacheavel do modelo, ou ha invalidador"
            " silencioso - e nos dois casos o defeito e nosso."
        )

    # Segunda passagem: o prefixo novo agora esta quente e tem de ser LIDO.
    conn.execute("DELETE FROM llm_cache")
    segundo = _rodar_ciclo(conn, cenario, reais, None)
    for l in conn.execute(
        "SELECT node, tokens_cache_read FROM agent_event"
        " WHERE run_id = ? AND provider IS NOT NULL ORDER BY id",
        (segundo.run_id,),
    ):
        assert l["tokens_cache_read"] and l["tokens_cache_read"] > 0, (
            f"{l['node']}: cache_read zero com o prefixo ja quente sob o"
            " contrato novo - ha invalidador silencioso no prefixo."
        )
