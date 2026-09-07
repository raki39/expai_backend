"""BBO alinhado a grade e janela do piloto (ADR 0032, incremento 18).

Os sete requisitos que o usuário fixou na aprovação da D51 têm teste cada um, e
os nomes dizem qual.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.aovivo import bbo
from app.calibracao import piloto

GRADE = 900_000
T0 = 1_756_000_000_000 // GRADE * GRADE
CONTRATO = "bbo@1"

SERIE = bbo.Serie(
    venue="binance", symbol="BTCUSDT",
    price_scale_exp=8, volume_scale_exp=8,
)

# Um offset realista: o coletor MEDIU -2.450 ms numa maquina real, e o valor
# passa da tolerancia inteira de 2 s. Usar zero aqui esconderia justamente o
# que a correcao existe para consertar.
OFFSET_US = -2_450_000


def amostra(i: int, *, defasagem_ms: int = 500, ask: int = 60_001_00000000,
            u: int = 1) -> bbo.Amostra:
    """Uma observacao valida no instante `i` da grade."""
    t_grid = T0 + i * GRADE
    corrigido = t_grid - defasagem_ms
    received = corrigido - OFFSET_US // 1000
    return bbo.Amostra(
        t_grid_ms=t_grid, disponivel=True,
        bid=ask - 100_00000, bid_qty=5_00000000,
        ask=ask, ask_qty=7_00000000, u=u,
        received_at_ms=received,
        received_at_corrigido_ms=corrigido,
        sampled_at_ms=received + 3,
        defasagem_ms=defasagem_ms,
        offset_us=OFFSET_US, rtt_us=12_000, incerteza_residual_us=6_000,
        relogio_medido_em_ms=corrigido - 30_000,
    )


def ausente(i: int, motivo: str = "desconectado") -> bbo.Amostra:
    return bbo.Amostra(
        t_grid_ms=T0 + i * GRADE, disponivel=False, motivo=motivo
    )


# ===========================================================================
# Requisito 3: alinhamento causal, e a ausencia registrada
# ===========================================================================


def test_a_regra_do_horario_corrigido_e_INTEIRA_e_uma_so():
    """`float` faria os dois lados discordarem por arredondamento.

    E a divergencia chegaria disfarcada de corrupcao de dado, que e o modo de
    falha mais caro que esta rota tem.
    """
    base = 1_000_000_000_000
    assert bbo.corrigir(base, -2_450_000) == base - 2_450
    assert bbo.corrigir(base, 2_450_000) == base + 2_450
    assert bbo.corrigir(1_000, 0) == 1_000

    # E o caso que decide a semantica: `//` do Python arredonda para BAIXO
    # (-inf), enquanto o `/` inteiro do SQLite TRUNCA para zero. Para offset
    # negativo - que e o caso real medido - os dois dao respostas diferentes:
    #
    #     Python:  -2_450_500 // 1000  ==  -2451
    #     SQLite:  -2450500 / 1000     ==  -2450
    #
    # Um dia alguem vai querer refazer esta conta em SQL, e o resultado
    # divergiria por 1 ms so em offsets negativos - o defeito mais dificil de
    # ver que este arquivo poderia produzir. A conta vive em Python, dos DOIS
    # lados, e este teste e o que fixa qual das duas semanticas vale.
    assert bbo.corrigir(base, -2_450_500) == base - 2_451
    assert bbo.corrigir(base, 2_450_500) == base + 2_450


def test_cotacao_POSTERIOR_ao_instante_e_recusada(conn: sqlite3.Connection):
    """Defasagem negativa e ler o futuro do instante."""
    a = amostra(0, defasagem_ms=-1)
    with pytest.raises(bbo.AmostraInvalida) as e:
        bbo.receber(conn, SERIE, CONTRATO, [a])
    assert "NEGATIVA" in str(e.value)
    assert "futuro" in str(e.value)


def test_defasagem_alem_da_tolerancia_nao_pode_vir_como_disponivel(
    conn: sqlite3.Connection,
):
    with pytest.raises(bbo.AmostraInvalida) as e:
        bbo.receber(conn, SERIE, CONTRATO, [amostra(0, defasagem_ms=2_001)])
    assert "tolerancia" in str(e.value)


def test_o_horario_corrigido_e_RECALCULADO_e_nao_aceito(conn: sqlite3.Connection):
    """Confiar no remetente moveria a fronteira de confianca para fora."""
    a = amostra(0)
    mentiroso = bbo.Amostra(
        **{**a.__dict__, "received_at_corrigido_ms": a.received_at_corrigido_ms + 7}
    )
    with pytest.raises(bbo.AmostraInvalida) as e:
        bbo.receber(conn, SERIE, CONTRATO, [mentiroso])
    assert "nao bate" in str(e.value)


def test_indisponivel_NUNCA_carrega_preco(conn: sqlite3.Connection):
    """A D41 e literal sobre nao interpolar.

    A forma mais facil de violar seria repetir a cotacao anterior num campo
    que ninguem olha - entao o modulo recusa, e o banco tambem.
    """
    a = bbo.Amostra(t_grid_ms=T0, disponivel=False, motivo="defasada",
                    ask=60_000_00000000)
    with pytest.raises(bbo.AmostraInvalida) as e:
        bbo.receber(conn, SERIE, CONTRATO, [a])
    assert "interpolar" in str(e.value)


def test_o_BANCO_tambem_recusa_indisponivel_com_preco(conn: sqlite3.Connection):
    """Por SQL cru: se o modulo tiver defeito, a regra continua valendo."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO bbo_amostra (venue, symbol, t_grid_ms, contrato,"
            " grade_ms, disponivel, motivo, ask, price_scale_exp,"
            " volume_scale_exp, recebido_em)"
            " VALUES ('binance','BTCUSDT',?,?,?,0,'defasada',1,8,8,'x')",
            (T0, CONTRATO, GRADE),
        )


def test_o_BANCO_recusa_defasagem_negativa(conn: sqlite3.Connection):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO bbo_amostra (venue, symbol, t_grid_ms, contrato,"
            " grade_ms, disponivel, motivo, defasagem_ms, price_scale_exp,"
            " volume_scale_exp, recebido_em)"
            " VALUES ('binance','BTCUSDT',?,?,?,0,'defasada',-1,8,8,'x')",
            (T0, CONTRATO, GRADE),
        )


def test_instante_fora_da_grade_e_recusado(conn: sqlite3.Connection):
    a = bbo.Amostra(t_grid_ms=T0 + 1, disponivel=False, motivo="defasada")
    with pytest.raises(bbo.AmostraInvalida) as e:
        bbo.receber(conn, SERIE, CONTRATO, [a])
    assert "grade" in str(e.value)


def test_motivo_fora_da_lista_fechada_e_recusado(conn: sqlite3.Connection):
    a = bbo.Amostra(t_grid_ms=T0, disponivel=False, motivo="deu_ruim")
    with pytest.raises(bbo.AmostraInvalida):
        bbo.receber(conn, SERIE, CONTRATO, [a])


def test_livro_cruzado_e_dado_corrompido(conn: sqlite3.Connection):
    a = amostra(0)
    cruzado = bbo.Amostra(**{**a.__dict__, "bid": a.ask + 1})
    with pytest.raises(bbo.AmostraInvalida) as e:
        bbo.receber(conn, SERIE, CONTRATO, [cruzado])
    assert "cruzado" in str(e.value)


# ===========================================================================
# Requisito 4: a chave idempotente, e o contrato dentro dela
# ===========================================================================


def test_reenvio_IDENTICO_e_absorvido(conn: sqlite3.Connection):
    r1 = bbo.receber(conn, SERIE, CONTRATO, [amostra(0), amostra(1)])
    r2 = bbo.receber(conn, SERIE, CONTRATO, [amostra(0), amostra(1)])
    assert (r1.aceitas, r1.repetidas) == (2, 0)
    assert (r2.aceitas, r2.repetidas) == (0, 2)


def test_conteudo_DIFERENTE_na_mesma_chave_e_erro_grave(conn: sqlite3.Connection):
    bbo.receber(conn, SERIE, CONTRATO, [amostra(0)])
    with pytest.raises(bbo.DivergenciaDeAmostra) as e:
        bbo.receber(conn, SERIE, CONTRATO, [amostra(0, ask=99_999_00000000)])
    assert "append-only" in str(e.value)


def test_lote_com_divergencia_nao_entra_pela_METADE(conn: sqlite3.Connection):
    """A transacao e deste modulo, e nao do chamador - licao do incremento 16."""
    bbo.receber(conn, SERIE, CONTRATO, [amostra(0)])
    with pytest.raises(bbo.DivergenciaDeAmostra):
        bbo.receber(conn, SERIE, CONTRATO,
                    [amostra(5), amostra(0, ask=99_999_00000000)])
    presentes = [
        int(r["t_grid_ms"])
        for r in conn.execute("SELECT t_grid_ms FROM bbo_amostra ORDER BY 1")
    ]
    assert presentes == [T0], f"a amostra 5 sobreviveu a um lote recusado: {presentes}"


def test_o_CONTRATO_esta_na_chave_e_as_duas_versoes_COEXISTEM(
    conn: sqlite3.Connection,
):
    """Sem o contrato na chave, mudar a regra reescreveria a historia.

    A mesma (instrumento, t_grid) passaria a ter outro conteudo numa tabela
    append-only - e ou o reenvio seria recusado, ou a serie teria duas
    semanticas sob o mesmo nome.
    """
    conn.execute(
        "INSERT INTO bbo_contrato (contrato, timeframe, execution_reference,"
        " latency_bars, grade_ms, tolerancia_ms, criado_em)"
        " VALUES ('bbo@2','15m','abertura',1,900000,2000,'x')"
    )
    bbo.receber(conn, SERIE, CONTRATO, [amostra(0)])
    r = bbo.receber(conn, SERIE, "bbo@2", [amostra(0, ask=60_500_00000000)])
    assert r.aceitas == 1, "o mesmo instante sob outro contrato e outra linha"
    assert conn.execute("SELECT COUNT(*) FROM bbo_amostra").fetchone()[0] == 2


def test_a_tabela_e_apenas_por_ACRESCIMO(conn: sqlite3.Connection):
    bbo.receber(conn, SERIE, CONTRATO, [amostra(0)])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE bbo_amostra SET ask = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM bbo_amostra")


def test_apagar_indisponivel_melhoraria_a_cobertura_sem_melhorar_o_dado(
    conn: sqlite3.Connection,
):
    """E por isso que o DELETE nao existe, e nao por disciplina."""
    bbo.receber(conn, SERIE, CONTRATO, [ausente(0)])
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("DELETE FROM bbo_amostra WHERE disponivel = 0")
    assert "cobertura" in str(e.value)


# ===========================================================================
# Requisito 5: o contrato amarrado a semantica de execucao
# ===========================================================================


def test_o_contrato_declara_a_semantica_que_assume(conn: sqlite3.Connection):
    c = bbo.ler_contrato(conn, CONTRATO)
    assert (c.timeframe, c.execution_reference, c.latency_bars) == (
        "15m", "abertura", 1
    )
    assert (c.grade_ms, c.tolerancia_ms) == (900_000, 2_000)


def test_mudar_execution_reference_RECUSA_a_calibracao(conn: sqlite3.Connection):
    """O requisito que mais importa.

    Amostrar na abertura so cobre os instantes de execucao porque a D20 fixou
    a referencia ali. Sem esta conferencia, trocar o campo faria as amostras
    deixarem de cobrir **sem que nada acusasse**.
    """
    with pytest.raises(bbo.ContratoNaoDescreveMais) as e:
        bbo.conferir_contrato(
            conn, CONTRATO, timeframe="15m",
            execution_reference="limite_adverso", latency_bars=1,
        )
    assert "execution_reference" in str(e.value)
    assert "instante errado" in str(e.value)


def test_mudar_latency_bars_tambem_recusa(conn: sqlite3.Connection):
    with pytest.raises(bbo.ContratoNaoDescreveMais) as e:
        bbo.conferir_contrato(conn, CONTRATO, timeframe="15m",
                              execution_reference="abertura", latency_bars=2)
    assert "latency_bars" in str(e.value)


def test_mudar_timeframe_tambem_recusa(conn: sqlite3.Connection):
    with pytest.raises(bbo.ContratoNaoDescreveMais):
        bbo.conferir_contrato(conn, CONTRATO, timeframe="1h",
                              execution_reference="abertura", latency_bars=1)


def test_semantica_igual_passa(conn: sqlite3.Connection):
    c = bbo.conferir_contrato(conn, CONTRATO, timeframe="15m",
                              execution_reference="abertura", latency_bars=1)
    assert c.contrato == CONTRATO


def test_a_RECUSA_e_da_calibracao_e_nao_da_ingestao(conn: sqlite3.Connection):
    """As amostras continuam chegando mesmo com a config divergente.

    Elas sao observacao de mercado e nao deixam de ser verdadeiras. Recusar a
    ingestao perderia dado irrecuperavel para proteger uma conta que se pode
    simplesmente nao fazer.
    """
    with pytest.raises(bbo.ContratoNaoDescreveMais):
        bbo.conferir_contrato(conn, CONTRATO, timeframe="15m",
                              execution_reference="limite_adverso",
                              latency_bars=1)
    r = bbo.receber(conn, SERIE, CONTRATO, [amostra(0)])
    assert r.aceitas == 1


def test_o_contrato_e_imutavel(conn: sqlite3.Connection):
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("UPDATE bbo_contrato SET execution_reference = 'x'")
    assert "reinterpretaria" in str(e.value)


# ===========================================================================
# Cobertura e retomada
# ===========================================================================


def test_a_retomada_conta_a_INDISPONIVEL_tambem(conn: sqlite3.Connection):
    """Ela tambem e observacao: declara que naquele instante nao havia cotacao."""
    bbo.receber(conn, SERIE, CONTRATO, [amostra(0), ausente(1)])
    assert bbo.ultimo_t_grid(conn, SERIE, CONTRATO) == T0 + GRADE


def test_cobertura_separa_o_que_existe_do_que_serve(conn: sqlite3.Connection):
    bbo.receber(conn, SERIE, CONTRATO, [
        amostra(0), amostra(1), ausente(2, "defasada"), ausente(3, "desconectado")
    ])
    c = bbo.cobertura(conn, SERIE, CONTRATO)
    assert (c["total"], c["validas"], c["indisponiveis"]) == (4, 2, 2)
    assert c["fracao_valida_ppm"] == 500_000
    assert c["por_motivo"] == {"defasada": 1, "desconectado": 1}


# ===========================================================================
# Requisito 7: a janela do piloto, derivada e gravada UMA vez
# ===========================================================================


def encher(conn: sqlite3.Connection, n: int, *, de: int = 0) -> None:
    bbo.receber(conn, SERIE, CONTRATO, [amostra(i) for i in range(de, de + n)])


def test_o_piloto_nao_fecha_antes_das_DUAS_travas(conn: sqlite3.Connection):
    encher(conn, 100)
    with pytest.raises(piloto.PilotoNaoFechaAinda) as e:
        piloto.derivar(conn, SERIE, CONTRATO)
    assert "1000" in str(e.value).replace(".", "")


def test_as_duas_travas_sao_obrigatorias_e_vence_a_MAIS_TARDE(
    conn: sqlite3.Connection,
):
    """Literal do ADR 0027: "encerra no mais tarde entre".

    Com 1.000 observacoes seguidas a 15 min sao ~10,4 dias - menos que 14.
    Entao quem fecha e o CALENDARIO, e a trava de contagem sozinha teria dado
    uma calibracao de dez dias.
    """
    # 14 dias de grade = 1344 instantes. Enche 1400 para cobrir os dois lados.
    encher(conn, 1_400)
    j = piloto.derivar(conn, SERIE, CONTRATO)
    assert j.fechada_por == "dias", (
        "1.000 observacoes a 15 min sao ~10,4 dias, e o ADR manda esperar 14"
    )
    assert j.ate_ms_exclusive == j.de_ms + 14 * piloto.MS_POR_DIA
    assert 13.9 < j.dias_corridos < 14.1


def test_quando_ha_muita_indisponibilidade_vence_a_CONTAGEM(
    conn: sqlite3.Connection,
):
    """O outro lado da mesma regra, e e o que a torna necessaria.

    Um periodo com metade das observacoes perdidas alcanca 14 dias com 672
    validas - e calibrar com 672 seria usar uma amostra que a propria regra
    declarou insuficiente.
    """
    # Uma valida a cada duas, por 30 dias de grade.
    amostras = [
        amostra(i) if i % 2 == 0 else ausente(i)
        for i in range(2_880)
    ]
    bbo.receber(conn, SERIE, CONTRATO, amostras)
    j = piloto.derivar(conn, SERIE, CONTRATO)
    assert j.fechada_por == "observacoes"
    assert j.observacoes_validas >= piloto.OBSERVACOES_MINIMAS
    assert j.dias_corridos > 14, "passou dos 14 dias esperando amostra valida"


def test_a_janela_e_gravada_UMA_vez_e_depois_LIDA(conn: sqlite3.Connection):
    """Sem isso ela mudaria a cada amostra nova, INVISIVELMENTE."""
    encher(conn, 1_400)
    primeira = piloto.fechar(conn, SERIE, CONTRATO)

    # Chega muito mais dado depois.
    encher(conn, 1_000, de=1_400)
    segunda = piloto.fechar(conn, SERIE, CONTRATO)

    assert segunda == primeira, "a janela se moveu depois de fechada"
    assert conn.execute("SELECT COUNT(*) FROM janela_piloto").fetchone()[0] == 1


def test_regravar_a_janela_e_recusado_pelo_BANCO(conn: sqlite3.Connection):
    encher(conn, 1_400)
    piloto.fechar(conn, SERIE, CONTRATO)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO janela_piloto (contrato, venue, symbol, de_ms,"
            " ate_ms_exclusive, observacoes_validas, observacoes_totais,"
            " dias_corridos_x1000, fechada_por, criado_em)"
            " VALUES (?,?,?,1,2,1,1,1,'dias','x')",
            (CONTRATO, SERIE.venue, SERIE.symbol),
        )
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute("UPDATE janela_piloto SET de_ms = 1")
    assert "olhando o resultado" in str(e.value)


def test_o_INICIO_e_derivado_e_nao_escolhido(conn: sqlite3.Connection):
    """O primeiro `t_grid` com amostra VALIDA - indisponivel nao abre o piloto."""
    amostras = [ausente(0), ausente(1)] + [amostra(i) for i in range(2, 1_402)]
    bbo.receber(conn, SERIE, CONTRATO, amostras)
    j = piloto.derivar(conn, SERIE, CONTRATO)
    assert j.de_ms == T0 + 2 * GRADE


def test_derivar_NAO_grava(conn: sqlite3.Connection):
    """Perguntar "quanto falta" nao pode congelar a fronteira como efeito."""
    encher(conn, 1_400)
    piloto.derivar(conn, SERIE, CONTRATO)
    piloto.derivar(conn, SERIE, CONTRATO)
    assert conn.execute("SELECT COUNT(*) FROM janela_piloto").fetchone()[0] == 0
    assert piloto.ler(conn, SERIE, CONTRATO) is None


def test_disponivel_SEM_a_idade_do_relogio_e_recusada(conn: sqlite3.Connection):
    """Nasceu de um defeito em producao, e a coluna veio junto.

    A extracao quebrou com `KeyError: offset_ms` porque toda linha
    `tipo: relogio` foi suposta trazer medicao - e a sonda FALHADA grava uma
    linha sem ela, de proposito ("um relogio nao medido e um relogio nao
    medido"). Ao consertar, ficou visivel que a observacao nao dizia de QUANDO
    era o offset que a corrigiu: seis horas e trinta segundos ficavam
    indistinguiveis.

    `incerteza_residual_us` cobre a assimetria da viagem; a idade cobre o
    envelhecimento. Sao coisas diferentes, e o requisito 2 pede as duas.
    """
    a = amostra(0)
    sem_idade = bbo.Amostra(**{**a.__dict__, "relogio_medido_em_ms": None})
    with pytest.raises(bbo.AmostraInvalida) as e:
        bbo.receber(conn, SERIE, CONTRATO, [sem_idade])
    assert "relogio_medido_em_ms" in str(e.value)


def test_a_idade_do_relogio_e_GRAVADA_e_nao_so_aceita(conn: sqlite3.Connection):
    bbo.receber(conn, SERIE, CONTRATO, [amostra(0)])
    linha = conn.execute(
        "SELECT relogio_medido_em_ms FROM bbo_amostra"
    ).fetchone()
    assert linha["relogio_medido_em_ms"] == T0 - 500 - 30_000


def test_NENHUM_limiar_de_validade_do_relogio_e_escolhido(conn: sqlite3.Connection):
    """A taxa de deriva ainda nao foi medida.

    Fixar "o offset vale por N minutos" sem medir seria arbitrio com uma casa
    decimal a mais - o mesmo que a D45 recusou ao nao fixar `sigma_e`. Grava-se
    o numero; o criterio vem depois, com medicao.

    Este teste existe para que, no dia em que alguem fixar o limiar, a decisao
    seja deliberada - e nao um `if` que apareceu sem ADR.
    """
    velho = amostra(0)
    antiga = bbo.Amostra(**{
        **velho.__dict__,
        "relogio_medido_em_ms": velho.received_at_corrigido_ms - 6 * 3_600_000,
    })
    r = bbo.receber(conn, SERIE, CONTRATO, [antiga])
    assert r.aceitas == 1, (
        "um offset de seis horas ainda e ACEITO, e de proposito: a idade fica "
        "gravada e quem decide o que fazer com ela e a calibracao"
    )


def test_a_cobertura_OBSERVADA_exclui_o_que_veio_antes_do_coletor(
    conn: sqlite3.Connection,
):
    """Medido na primeira entrega real: 301 dos 367 indisponiveis eram de
    antes de existir coletor.

    Sem ponto de retomada, a primeira extracao olha 7 dias para tras, e o
    coletor liga em 2026-09-04. Aqueles instantes nao sao observacao de
    "nao havia cotacao" - sao a ausencia de observador, e contar as duas
    coisas juntas faz a cobertura descrever a nossa data de deploy em vez do
    mercado.
    """
    # 4 instantes antes de existir dado, depois 4 observados (3 validos).
    antes = [ausente(i, "sem_amostra_na_janela") for i in range(4)]
    depois = [amostra(4), amostra(5), ausente(6, "defasada"), amostra(7)]
    bbo.receber(conn, SERIE, CONTRATO, antes + depois)

    total = bbo.cobertura(conn, SERIE, CONTRATO)
    assert (total["total"], total["validas"]) == (8, 3)
    assert total["fracao_valida_ppm"] == 375_000, "37,5% - e o numero enganoso"

    primeiro_valido = T0 + 4 * GRADE
    observada = bbo.cobertura(conn, SERIE, CONTRATO, de_ms=primeiro_valido)
    assert (observada["total"], observada["validas"]) == (4, 3)
    assert observada["fracao_valida_ppm"] == 750_000, "75% - e o numero real"
    assert observada["por_motivo"] == {"defasada": 1}
