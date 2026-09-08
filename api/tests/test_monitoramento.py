"""Incremento 20: o monitoramento continuo de §8.8. ADR 0035.

Os seis criterios do plano, mais as seis garantias que o usuario exigiu ao
aprovar a D46, mais a demonstracao dimensional que ele pediu ANTES de `d_t`
ser congelado.

O teste que mais importa nao e nenhum dos que verificam recusa: e o
**controle sintetico**, que exige que o monitor DISPARE quando a degradacao
existe. Um monitor que nunca alarma tem falso alarme perfeito e e inutil - a
mesma distincao entre calibrado e surdo que o "1 promocao em 200 lotes" do
Portao A mediu.
"""

from __future__ import annotations

import random
import sqlite3

import pytest

from app.hipotese import poder
from app.monitoramento import cusum, limiar, monitor
from app.monitoramento import veredito as v
from app.regime.deteccao import BURACO_MAXIMO_BARRAS, FRACAO_MINIMA_PRESENTE
from app.relatorio import monitoramento as relatorio
from app.store import migrar
from app.validador import estados

BARRA_MS = 15 * 60 * 1_000


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    migrar(c)
    return c


def _serie(n: int, media: int, desvio: int, semente: int = 7) -> list[int]:
    rng = random.Random(semente)
    return [int(rng.gauss(media, desvio)) for _ in range(n)]


def _congelar(
    conn: sqlite3.Connection, *, assunto: str = "teste",
    efeito: int = 50_000, horizonte: int = 21_024,
    serie: list[int] | None = None, reserva: int = 500,
) -> monitor.LimiarCongelado:
    return monitor.congelar(
        conn, assunto=assunto, efeito_minimo_cents=efeito,
        horizonte_barras=horizonte,
        serie_milicents=serie if serie is not None else _serie(1_500, 4_000, 3_000),
        run_digest="digest-do-congelado", horizonte_da_reserva=reserva,
        repeticoes=120,
    )


# ===========================================================================
# A DEMONSTRACAO DIMENSIONAL
#
# O usuario pediu, textualmente, "antes de congelar d_t, demonstre
# dimensionalmente que observado_t, efeito_minimo, n_minimo e a frequencia por
# barra estao na mesma unidade". Nao estavam - e estes testes sao a
# demonstracao, em codigo executavel em vez de em prosa.
# ===========================================================================


def test_n_minimo_esta_em_observacoes_efetivas_e_nao_em_barras():
    """A premissa inteira do ajuste 3, lida do proprio codigo.

    Se esta docstring mudar em `poder.py`, este teste quebra - e e assim que a
    demonstracao dimensional deixa de ser uma afirmacao minha.
    """
    assert "EFETIVAS" in poder.n_minimo.__doc__


def test_o_alvo_por_barra_usa_horizonte_e_nao_n_minimo():
    """A conta condenada, ao lado da correta, com os numeros da hipotese 41."""
    efeito, n_min, horizonte = 50_000, 19_240, 21_024

    certo = cusum.alvo_por_barra(
        efeito_minimo_cents=efeito, horizonte_barras=horizonte
    )
    errado = cusum.alvo_por_barra(
        efeito_minimo_cents=efeito, horizonte_barras=n_min
    )

    assert certo == 2_379          # milicents por BARRA
    assert errado == 2_599         # milicents por observacao EFETIVA
    # +9,27%: o alvo errado e mais alto, entao `d_t` fica mais positivo e o
    # CUSUM sobe sem degradacao nenhuma.
    assert errado > certo
    assert round(100 * (errado / certo - 1), 2) == 9.25 or errado - certo == 220

    # A deriva fantasma sobre a reserva, em cents.
    deriva_cents = (errado - certo) * relatorio.RESERVA_BARRAS // 1_000
    assert deriva_cents == 1_774   # US$ 17,74 de deficit inventado


def test_n_minimo_nao_aparece_no_pacote_de_monitoramento():
    """Guarda de codigo: `n_minimo` nao volta para a conta da taxa.

    Verificada nao-vacuidade: a guarda le arquivos de verdade, e ha assercao
    de que ela leu algo. Guarda vazia e pior que guarda ausente.
    """
    import pathlib

    from tests._prosa import codigo_sem_prosa

    pasta = pathlib.Path(monitor.__file__).parent
    arquivos = sorted(pasta.glob("*.py"))
    assert len(arquivos) >= 4, "a guarda precisa ler o pacote inteiro"

    acusados = [a.name for a in arquivos if "n_minimo" in codigo_sem_prosa(a)]
    assert acusados == [], (
        f"`n_minimo` esta em observacoes EFETIVAS e o CUSUM atualiza por"
        f" BARRA: {acusados} misturam unidades (ADR 0035, ajuste 3)"
    )


def test_a_folga_e_metade_do_alvo_e_nao_um_numero_escolhido():
    for alvo in (2_379, 1, 1_000, -2_000):
        assert cusum.folga(alvo) == abs(alvo) // 2


def test_o_arredondamento_do_alvo_vai_para_cima():
    # 1 cent em 3 barras = 333,33 milicents/barra -> 334, na direcao de mais
    # sensibilidade.
    assert cusum.alvo_por_barra(efeito_minimo_cents=1, horizonte_barras=3) == 334


def test_horizonte_zero_nao_tem_taxa_a_derivar():
    with pytest.raises(cusum.AlvoNaoDerivavel):
        cusum.alvo_por_barra(efeito_minimo_cents=1, horizonte_barras=0)


# ===========================================================================
# CRITERIO 1: CUSUM sobre o desvio entre esperado e observado
# ===========================================================================


def test_a_recorrencia_tem_uma_definicao_so():
    """`acumular` e a calibracao usam a MESMA `avancar`, e o teste amarra."""
    xs = _serie(200, 2_000, 1_500, semente=3)
    alvo, k = 2_379, 1_189

    pela_serie = cusum.acumular(
        [(i, x) for i, x in enumerate(xs)],
        alvo_milicents_por_barra=alvo, folga_milicents_por_barra=k,
    ).maximo_milicents
    pela_replica = cusum.maximo_de_uma_replica(xs, alvo=alvo, k=k)

    assert pela_serie == pela_replica


def test_o_piso_em_zero_e_parte_da_estatistica():
    # Observado muito acima do alvo: `d_t` negativo, e `S` nao fica negativo.
    s = cusum.acumular(
        [(i, 10_000) for i in range(50)],
        alvo_milicents_por_barra=2_379, folga_milicents_por_barra=1_189,
    )
    assert all(p.s_milicents == 0 for p in s.passos)
    assert s.maximo_milicents == 0


def test_s_inicial_negativo_e_recusado():
    with pytest.raises(ValueError):
        cusum.acumular([], alvo_milicents_por_barra=1,
                       folga_milicents_por_barra=0, s_inicial_milicents=-1)


# ===========================================================================
# CRITERIO 2: o esperado vem do PRE-REGISTRO, nunca do proprio observado
# ===========================================================================


def test_o_alvo_nao_e_media_movel_do_observado(conn: sqlite3.Connection):
    """R79. O alvo congelado nao muda quando o observado muda."""
    lim = _congelar(conn)
    antes = lim.alvo_milicents_por_barra

    monitor.registrar(
        conn, assunto="teste",
        observados=[(i * BARRA_MS, -50_000) for i in range(30)],
    )
    depois = monitor.exigir(conn, "teste").alvo_milicents_por_barra
    assert depois == antes


def test_sem_pre_registro_nao_ha_alvo():
    """O B3 nao vira sujeito: `AlvoNaoDerivavel`, e nao um alvo padrao."""
    assert cusum.AlvoNaoDerivavel.__doc__ is not None
    assert "R79" in cusum.AlvoNaoDerivavel.__doc__


# ===========================================================================
# CRITERIO 3 e o AJUSTE 2: dois limiares, aninhados
# ===========================================================================


def test_os_dois_limiares_saem_da_mesma_distribuicao_e_sao_aninhados():
    xs = _serie(1_000, 3_000, 4_000)
    cal = limiar.calibrar(
        xs, alvo_milicents_por_barra=2_379, folga_milicents_por_barra=1_189,
        horizonte_barras=500, repeticoes=200,
    )
    assert cal.alerta_milicents <= cal.critico_milicents
    assert cal.prob_alerta_ppm == 100_000    # 10%
    assert cal.prob_critico_ppm == 50_000    # 5%


def test_o_orcamento_e_honrado_sobre_o_HORIZONTE_e_nao_em_media():
    """A garantia do ajuste 1, medida: <= 10% das replicas cruzam o alerta."""
    xs = _serie(4_000, 3_000, 4_000, semente=11)
    alvo, k = 2_379, 1_189
    cal = limiar.calibrar(
        xs, alvo_milicents_por_barra=alvo, folga_milicents_por_barra=k,
        horizonte_barras=800, repeticoes=400,
    )

    # Replicas NOVAS, com outra semente: se o limiar so servisse para as que o
    # calibraram, isto acusaria.
    from app.calibracao.bootstrap import reamostrar_por_blocos
    rng = random.Random(99)
    cruzou_alerta = cruzou_critico = 0
    for _ in range(400):
        r = reamostrar_por_blocos(xs, 800, cal.bloco, rng)
        m = cusum.maximo_de_uma_replica(r, alvo=alvo, k=k)
        cruzou_alerta += m >= cal.alerta_milicents
        cruzou_critico += m >= cal.critico_milicents

    # Folga de amostragem: 400 replicas dao erro-padrao ~1,5 pontos em 10%.
    assert cruzou_alerta / 400 <= 0.16
    assert cruzou_critico / 400 <= 0.10
    assert cruzou_critico <= cruzou_alerta


def test_o_banco_recusa_limiares_invertidos(conn: sqlite3.Connection):
    _congelar(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO monitor_limiar (assunto, efeito_minimo_cents,"
            " horizonte_barras, alvo_milicents_por_barra,"
            " folga_milicents_por_barra, alerta_milicents, critico_milicents,"
            " prob_alerta_ppm, prob_critico_ppm, semente, algoritmo, bloco,"
            " repeticoes, serie_hash, serie_n, run_digest, congelado_em)"
            " VALUES ('invertido',1,1,1,0,900,100,100000,50000,42,'x',1,1,"
            " 'h',10,'d','agora')"
        )


def test_cruzar_o_critico_implica_ter_cruzado_o_alerta(conn: sqlite3.Connection):
    """Aninhamento imposto pelo BANCO, e nao pela ordem em que eu gravo."""
    _congelar(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO monitor_alarme (assunto, nivel, t_ms, s_milicents,"
            " limiar_milicents, barras_puladas, maior_lacuna, motivo,"
            " registrado_em) VALUES"
            " ('teste','critico',1,999999,1,0,0,'sem alerta antes','agora')"
        )


def test_um_salto_grande_grava_os_DOIS_alarmes_na_mesma_barra(
    conn: sqlite3.Connection,
):
    lim = _congelar(conn)
    # Um deficit enorme numa barra so: cruza alerta e critico juntos.
    salto = -(lim.critico_milicents * 3)
    monitor.registrar(conn, assunto="teste", observados=[(BARRA_MS, salto)])

    niveis = [a["nivel"] for a in monitor.alarmes(conn, "teste")]
    assert niveis == ["alerta", "critico"]
    assert len({a["t_ms"] for a in monitor.alarmes(conn, "teste")}) == 1


# ===========================================================================
# CRITERIO 4: os dois estados entram na maquina de §8.1
# ===========================================================================


def _hipotese(conn: sqlite3.Connection) -> int:
    """Uma hipotese na maquina, pelo helper que a suite ja tem.

    Reusar em vez de escrever a quarta versao de um `INSERT INTO hypothesis` -
    a licao que o incremento 19 pagou tres vezes.
    """
    from tests.test_creditos import _config_version, _hipotese as criar, _run

    cv = _config_version(conn)
    return criar(conn, _run(conn, cv))


def test_os_dois_estados_existem_na_maquina_desde_a_migracao_11(
    conn: sqlite3.Connection,
):
    legais = {
        (r["de"], r["para"])
        for r in conn.execute("SELECT de, para FROM transicao_legal")
    }
    assert ("conhecimento_validado", "em_suspeita") in legais
    assert ("em_suspeita", "invalidado") in legais
    assert ("em_suspeita", "revalidado") in legais


def test_nenhum_estado_pode_ser_pulado_pelo_monitor(conn: sqlite3.Connection):
    """Uma hipotese recem-registrada nao vai direto para `em_suspeita`."""
    h = _hipotese(conn)
    estados.registrar_entrada(conn, h, evidencia={"t": 1})
    with pytest.raises(estados.TransicaoRecusada):
        monitor.transitar_por_alarme(
            conn, hypothesis_id=h, nivel="alerta", motivo="x", alarme_id=1,
        )


def test_o_caminho_completo_ate_invalidado_funciona(conn: sqlite3.Connection):
    h = _hipotese(conn)
    estados.registrar_entrada(conn, h, evidencia={"t": 1})
    for para in ("candidata", "em_quarentena", "conhecimento_validado"):
        estados.transitar(conn, h, para=para, evidencia={"t": 1})

    monitor.transitar_por_alarme(
        conn, hypothesis_id=h, nivel="alerta", motivo="cruzou", alarme_id=1,
    )
    assert estados.atual(conn, h).estado == "em_suspeita"

    monitor.transitar_por_alarme(
        conn, hypothesis_id=h, nivel="critico", motivo="cruzou", alarme_id=2,
    )
    assert estados.atual(conn, h).estado == "invalidado"


# ===========================================================================
# CRITERIO 5 e R81: o motivo fica, e o registro PERMANECE
# ===========================================================================


def test_o_alarme_grava_o_motivo_e_nao_se_apaga(conn: sqlite3.Connection):
    lim = _congelar(conn)
    monitor.registrar(
        conn, assunto="teste",
        observados=[(BARRA_MS, -(lim.alerta_milicents * 2))],
    )
    (a,) = [x for x in monitor.alarmes(conn, "teste") if x["nivel"] == "alerta"]
    assert lim.serie_hash[:12] in a["motivo"]
    assert str(lim.prob_alerta_ppm) in a["motivo"]

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM monitor_alarme WHERE id = ?", (a["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE monitor_alarme SET motivo = 'outro' WHERE id = ?",
            (a["id"],),
        )


def test_o_alarme_e_LATCHED_e_o_primeiro_cruzamento_vence(
    conn: sqlite3.Connection,
):
    lim = _congelar(conn)
    monitor.registrar(
        conn, assunto="teste",
        observados=[(BARRA_MS, -(lim.alerta_milicents * 2))],
    )
    primeiro = monitor.alarmes(conn, "teste")[0]

    # Mais barras, mais deficit: o alarme nao e reescrito.
    monitor.registrar(
        conn, assunto="teste",
        observados=[(2 * BARRA_MS, -(lim.alerta_milicents * 5))],
    )
    depois = [a for a in monitor.alarmes(conn, "teste") if a["nivel"] == "alerta"]
    assert len(depois) == 1
    assert depois[0]["t_ms"] == primeiro["t_ms"]
    assert depois[0]["s_milicents"] == primeiro["s_milicents"]


def test_o_alarme_sobrevive_a_reinicio_do_servico(tmp_path):
    """Ele mora no banco, e nao em memoria. Conexao nova, alarme la."""
    caminho = tmp_path / "monitor.db"
    c1 = sqlite3.connect(caminho, isolation_level=None)
    c1.row_factory = sqlite3.Row
    migrar(c1)
    lim = _congelar(c1)
    monitor.registrar(
        c1, assunto="teste",
        observados=[(BARRA_MS, -(lim.alerta_milicents * 2))],
    )
    antes = monitor.alarmes(c1, "teste")
    assert "alerta" in [a["nivel"] for a in antes]
    c1.close()

    c2 = sqlite3.connect(caminho, isolation_level=None)
    c2.row_factory = sqlite3.Row
    assert monitor.alarmes(c2, "teste") == antes


def test_o_passo_e_imutavel(conn: sqlite3.Connection):
    _congelar(conn)
    monitor.registrar(conn, assunto="teste", observados=[(BARRA_MS, 3_000)])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE monitor_passo SET s_milicents = 0")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM monitor_passo")


# ===========================================================================
# O LIMIAR E CONGELADO ANTES DO PRIMEIRO TICK
# ===========================================================================


def test_nao_se_recalibra(conn: sqlite3.Connection):
    _congelar(conn)
    with pytest.raises(monitor.JaCongelado):
        _congelar(conn)


def test_o_limiar_e_imutavel_por_gatilho(conn: sqlite3.Connection):
    _congelar(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE monitor_limiar SET alerta_milicents = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM monitor_limiar")


def test_o_monitor_nao_roda_com_limiar_improvisado(conn: sqlite3.Connection):
    with pytest.raises(monitor.LimiarNaoCongelado):
        monitor.registrar(conn, assunto="nao-existe", observados=[])


def test_a_procedencia_do_limiar_fica_gravada(conn: sqlite3.Connection):
    lim = _congelar(conn)
    assert lim.semente == limiar.SEMENTE
    assert lim.algoritmo == limiar.ALGORITMO
    assert lim.bloco >= 1
    assert lim.repeticoes >= 1
    assert len(lim.serie_hash) == 64
    assert lim.run_digest == "digest-do-congelado"


def test_o_hash_da_serie_muda_se_a_serie_mudar():
    a = limiar.hash_da_serie([1, 2, 3])
    b = limiar.hash_da_serie([1, 2, 4])
    assert a != b
    assert a == limiar.hash_da_serie([1, 2, 3])


# ===========================================================================
# AJUSTE 4: barra ausente
# ===========================================================================


def test_barra_ausente_nao_atualiza_o_cusum():
    s = cusum.acumular(
        [(1, None), (2, None), (3, None)],
        alvo_milicents_por_barra=2_379, folga_milicents_por_barra=1_189,
    )
    assert [p.s_milicents for p in s.passos] == [0, 0, 0]
    assert s.barras_puladas == 3
    assert s.maior_lacuna == 3
    assert all(p.d_milicents is None for p in s.passos)


def test_barra_ausente_tratada_como_zero_SUBIRIA_o_cusum():
    """A demonstracao de por que a minha resposta 4 estava errada."""
    como_zero = cusum.acumular(
        [(i, 0) for i in range(10)],
        alvo_milicents_por_barra=2_379, folga_milicents_por_barra=1_189,
    )
    como_ausente = cusum.acumular(
        [(i, None) for i in range(10)],
        alvo_milicents_por_barra=2_379, folga_milicents_por_barra=1_189,
    )
    assert como_zero.maximo_milicents > 0     # empurraria para o alarme
    assert como_ausente.maximo_milicents == 0


def test_a_maior_lacuna_e_a_maior_corrida_e_nao_o_total():
    s = cusum.acumular(
        [(0, 1), (1, None), (2, 1), (3, None), (4, None), (5, None), (6, 1)],
        alvo_milicents_por_barra=1, folga_milicents_por_barra=0,
    )
    assert s.barras_puladas == 4
    assert s.maior_lacuna == 3


def test_a_tolerancia_e_a_da_D40_e_nao_uma_copia():
    assert v.COBERTURA_MINIMA_PPM == int(round(FRACAO_MINIMA_PRESENTE * 1e6))
    # O 4 de `deteccao.py` e "1 hora a 15 minutos". Se o timeframe mudar, esta
    # igualdade quebra e alguem decide - em vez de o 4 passar a descrever
    # outra coisa em silencio.
    assert v.lacuna_maxima_em_barras(BARRA_MS) == BURACO_MAXIMO_BARRAS


def test_cobertura_abaixo_da_tolerancia_deixa_o_monitor_indisponivel():
    passos = [(i, None) for i in range(5)] + [(i, 1_000) for i in range(5, 100)]
    s = cusum.acumular(
        passos, alvo_milicents_por_barra=2_379, folga_milicents_por_barra=1_189,
    )
    ver = v.avaliar(
        s, v.Limiares(1, 2, 100_000, 50_000), duracao_barra_ms=BARRA_MS,
    )
    assert ver.estado == v.INDISPONIVEL
    assert ver.mediu is False
    assert "D40" in ver.motivo


def test_lacuna_longa_deixa_o_monitor_indisponivel_mesmo_com_cobertura_alta():
    # 5 ausentes seguidas (> 4 barras = 1 hora) em 2.000: cobertura 99,75%.
    passos = (
        [(i, 1_000) for i in range(1_000)]
        + [(1_000 + i, None) for i in range(5)]
        + [(1_005 + i, 1_000) for i in range(995)]
    )
    s = cusum.acumular(
        passos, alvo_milicents_por_barra=2_379, folga_milicents_por_barra=1_189,
    )
    assert s.cobertura_ppm > v.COBERTURA_MINIMA_PPM
    ver = v.avaliar(
        s, v.Limiares(1, 2, 100_000, 50_000), duracao_barra_ms=BARRA_MS,
    )
    assert ver.estado == v.INDISPONIVEL
    assert "lacuna" in ver.motivo


def test_indisponivel_vem_ANTES_do_alarme():
    """Com cobertura ruim, nem o alarme nem a ausencia dele sao lidos."""
    passos = [(i, None) for i in range(50)] + [(50, -10_000_000)]
    s = cusum.acumular(
        passos, alvo_milicents_por_barra=2_379, folga_milicents_por_barra=1_189,
    )
    ver = v.avaliar(
        s, v.Limiares(1, 2, 100_000, 50_000), duracao_barra_ms=BARRA_MS,
    )
    assert ver.cruzou_critico is True     # cruzou de fato
    assert ver.estado == v.INDISPONIVEL   # e ainda assim nao e lido
    assert ver.mediu is False


# ===========================================================================
# O CONTROLE SINTETICO — a prova de que o monitor NAO E SURDO
#
# Precedente do ADR 0034, aprovado pelo usuario: o caminho que produz veredito
# positivo roda na SUITE, sobre a funcao pura, com dado artificial. Ele nao
# produz evidencia e nao aparece como candidata real.
# ===========================================================================


def test_controle_sintetico_alarma_na_degradacao_e_nao_alarma_sem_ela():
    """A prova de que o monitor NAO E SURDO, e ela e por FRACAO.

    Uma realizacao so nao prova nada dos dois lados: com orcamento de 10%, uma
    serie saudavel cruza o alerta uma vez em dez **por construcao**. Medir uma
    so e chamar de "nao alarma" seria a leitura ingenua que o "1 promocao em
    200 lotes" do Portao A existe para evitar - la o numero e o que separa
    calibrado de surdo, e aqui tambem.

    A serie que calibra tem o tamanho do in-sample real (21.024 barras) porque
    o limiar depende da CAUDA dela, e cauda de amostra curta e truncada. O
    teste abaixo mede exatamente esse efeito.
    """
    alvo, k = 2_379, 1_189
    calibradora = _serie(21_024, 5_000, 2_000, semente=5)
    cal = limiar.calibrar(
        calibradora, alvo_milicents_por_barra=alvo, folga_milicents_por_barra=k,
        horizonte_barras=1_000, repeticoes=600,
    )
    lim = v.Limiares(
        cal.alerta_milicents, cal.critico_milicents,
        cal.prob_alerta_ppm, cal.prob_critico_ppm,
    )

    def estado(serie: list[int]) -> str:
        s = cusum.acumular(
            [(i, x) for i, x in enumerate(serie)],
            alvo_milicents_por_barra=alvo, folga_milicents_por_barra=k,
        )
        return v.avaliar(s, lim, duracao_barra_ms=BARRA_MS).estado

    saudaveis = [
        estado(_serie(1_000, 5_000, 2_000, semente=100 + i)) for i in range(20)
    ]
    assert saudaveis.count(v.SEM_ALARME) >= 15, saudaveis

    # Com degradacao implantada: a taxa cai abaixo de metade do alvo, que e
    # exatamente o desvio que `k = alvo/2` foi derivado para detectar.
    degradadas = [
        estado(_serie(1_000, -3_000, 2_000, semente=200 + i)) for i in range(20)
    ]
    assert all(e == v.INVALIDADO for e in degradadas), degradadas


def test_o_limite_do_bootstrap_esta_medido_e_declarado():
    """O bootstrap condiciona no in-sample OBSERVADO, e isso tem preco.

    H0 aqui e *"o forward se comporta como o in-sample congelado"*, e nao *"o
    forward vem da mesma lei desconhecida"*. Contra realizacoes NOVAS da mesma
    lei, o limiar cruza um pouco mais do que o orcamento nominal, porque a
    cauda de uma amostra finita e truncada.

    **Medido, e nao estimado**, com alvo 2.379, folga 1.189 e horizonte 1.000:

    | serie que calibra | cruzam sob H0 verdadeira |
    |---|---|
    | 2.000 barras | 61,3% |
    | 8.000 barras | 13,3% |
    | **21.024 (o in-sample real)** | **13,3%** |
    | 60.000 barras | 10,8% |

    A amostra curta e o caso ruim, e ele nao e o nosso. Este teste fixa o
    numero do caso real, para que ele nao piore em silencio.
    """
    alvo, k = 2_379, 1_189
    calibradora = _serie(21_024, 5_000, 2_000, semente=5)
    cal = limiar.calibrar(
        calibradora, alvo_milicents_por_barra=alvo, folga_milicents_por_barra=k,
        horizonte_barras=1_000, repeticoes=600,
    )
    frescas = [
        cusum.maximo_de_uma_replica(
            _serie(1_000, 5_000, 2_000, semente=9_000 + i), alvo=alvo, k=k
        )
        for i in range(300)
    ]
    cruzam = sum(1 for m in frescas if m >= cal.alerta_milicents) / 300
    assert cruzam <= relatorio.CRUZAMENTO_MEDIDO_SOB_H0_VERDADEIRA
    assert cruzam > cal.prob_alerta_ppm / 1_000_000 * 0.5


def test_o_controle_sintetico_nao_passa_pelo_caminho_de_producao(
    conn: sqlite3.Connection,
):
    """Ele alimenta `avaliar`, e `monitorar` continua sem sujeito."""
    assert monitor.monitorar(conn, duracao_barra_ms=BARRA_MS).estado == (
        v.SEM_CONHECIMENTO
    )


# ===========================================================================
# A GARANTIA: ausencia de alarme nao comprova nada
# ===========================================================================


def test_sem_alarme_carrega_o_proprio_desmentido():
    s = cusum.acumular(
        [(i, 10_000) for i in range(200)],
        alvo_milicents_por_barra=2_379, folga_milicents_por_barra=1_189,
    )
    ver = v.avaliar(
        s, v.Limiares(100, 200, 100_000, 50_000), duracao_barra_ms=BARRA_MS,
    )
    assert ver.estado == v.SEM_ALARME
    assert ver.comprova_edge is False
    assert ver.promove_candidata is False
    assert "NAO comprova edge" in ver.motivo


def test_nenhum_estado_do_monitor_comprova_edge():
    for fabricar in (
        lambda: v.sem_conhecimento_em_uso("x"),
        lambda: v.avaliar(
            cusum.acumular([(0, 0)], alvo_milicents_por_barra=1,
                           folga_milicents_por_barra=0),
            v.Limiares(1, 2, 100_000, 50_000), duracao_barra_ms=BARRA_MS,
        ),
    ):
        ver = fabricar()
        assert ver.comprova_edge is False
        assert ver.promove_candidata is False


def test_sem_alarme_nao_entra_nos_requisitos_da_quarentena():
    """Guarda: o monitor nao vira sexto-e-meio requisito de saida.

    Verificada nao-vacuidade - a lista de requisitos e lida de verdade.
    """
    from app.quarentena.veredito import REQUISITOS

    assert len(REQUISITOS) == 6
    texto = " ".join(REQUISITOS).lower()
    for proibido in ("alarme", "cusum", "monitor", "suspeita"):
        assert proibido not in texto


def test_o_modulo_da_quarentena_nao_importa_o_monitoramento():
    import pathlib

    from app.quarentena import veredito as qv

    from tests._prosa import codigo_sem_prosa

    fonte = codigo_sem_prosa(pathlib.Path(qv.__file__))
    assert "import" in fonte, "guarda vazia"
    assert "monitoramento" not in fonte


# ===========================================================================
# PUREZA: `cusum` e `veredito` nao alcançam o banco
# ===========================================================================


def test_os_modulos_puros_nao_importam_sqlite():
    import pathlib

    from tests._prosa import codigo_sem_prosa

    for modulo in (cusum, v):
        fonte = codigo_sem_prosa(pathlib.Path(modulo.__file__))
        assert "import" in fonte, "guarda vazia"
        assert "sqlite3" not in fonte, f"{modulo.__name__} alcanca o banco"


def test_avaliar_nao_recebe_conexao():
    import inspect

    nomes = set(inspect.signature(v.avaliar).parameters)
    assert "conn" not in nomes
    assert nomes == {"serie", "lim", "duracao_barra_ms"}


def test_o_monitor_nao_importa_app_fase():
    """A recusa da 0C e uma CONSULTA, e nao um `if` sobre a fase."""
    import pathlib

    from tests._prosa import codigo_sem_prosa

    fonte = codigo_sem_prosa(pathlib.Path(monitor.__file__))
    assert "import" in fonte, "guarda vazia"
    assert "app.fase" not in fonte and "from ..fase" not in fonte


# ===========================================================================
# O RETESTE: posterior e disjunto, e nao apaga nada
# ===========================================================================


def test_o_reteste_precisa_ser_posterior_ao_alarme(conn: sqlite3.Connection):
    lim = _congelar(conn)
    monitor.registrar(
        conn, assunto="teste",
        observados=[(10 * BARRA_MS, -(lim.alerta_milicents * 2))],
    )
    (a,) = [x for x in monitor.alarmes(conn, "teste") if x["nivel"] == "alerta"]

    with pytest.raises(sqlite3.IntegrityError):
        monitor.agendar_reteste(
            conn, alarme_id=a["id"], de_ms=a["t_ms"],
            ate_ms_exclusive=a["t_ms"] + BARRA_MS,
        )
    monitor.agendar_reteste(
        conn, alarme_id=a["id"], de_ms=a["t_ms"] + BARRA_MS,
        ate_ms_exclusive=a["t_ms"] + 100 * BARRA_MS,
    )
    assert len(monitor.retestes(conn, "teste")) == 1


def test_o_reteste_nao_apaga_historico_nem_reinicia_o_cusum(
    conn: sqlite3.Connection,
):
    lim = _congelar(conn)
    monitor.registrar(
        conn, assunto="teste",
        observados=[(BARRA_MS, -(lim.alerta_milicents * 2))],
    )
    s_antes = monitor.serie(conn, "teste").passos[-1].s_milicents
    (a,) = [x for x in monitor.alarmes(conn, "teste") if x["nivel"] == "alerta"]
    monitor.agendar_reteste(
        conn, alarme_id=a["id"], de_ms=2 * BARRA_MS,
        ate_ms_exclusive=50 * BARRA_MS,
    )

    # Barras novas, depois do reteste: o acumulado CONTINUA de onde estava.
    monitor.registrar(
        conn, assunto="teste", observados=[(3 * BARRA_MS, 4_000)],
    )
    serie = monitor.serie(conn, "teste")
    assert len(serie.passos) == 2
    assert serie.passos[0].s_milicents == s_antes
    assert monitor.alarmes(conn, "teste")     # o alarme continua la


def test_a_janela_do_reteste_e_selada(conn: sqlite3.Connection):
    lim = _congelar(conn)
    monitor.registrar(
        conn, assunto="teste",
        observados=[(BARRA_MS, -(lim.alerta_milicents * 2))],
    )
    (a,) = [x for x in monitor.alarmes(conn, "teste") if x["nivel"] == "alerta"]
    monitor.agendar_reteste(
        conn, alarme_id=a["id"], de_ms=2 * BARRA_MS,
        ate_ms_exclusive=50 * BARRA_MS,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE monitor_reteste SET de_ms = 0")


# ===========================================================================
# CONTINUIDADE: reprocessar a mesma barra nao a conta duas vezes
# ===========================================================================


def test_barra_ja_registrada_e_ignorada(conn: sqlite3.Connection):
    _congelar(conn)
    obs = [(i * BARRA_MS, 1_000) for i in range(10)]
    primeiro = monitor.registrar(conn, assunto="teste", observados=obs)
    segundo = monitor.registrar(conn, assunto="teste", observados=obs)

    assert primeiro["barras_novas"] == 10
    assert segundo["barras_novas"] == 0
    assert len(monitor.serie(conn, "teste").passos) == 10


def test_o_acumulado_continua_entre_voltas(conn: sqlite3.Connection):
    _congelar(conn)
    de_uma_vez = cusum.acumular(
        [(i * BARRA_MS, 1_000) for i in range(20)],
        alvo_milicents_por_barra=monitor.exigir(conn, "teste").alvo_milicents_por_barra,
        folga_milicents_por_barra=monitor.exigir(conn, "teste").folga_milicents_por_barra,
    )
    monitor.registrar(
        conn, assunto="teste",
        observados=[(i * BARRA_MS, 1_000) for i in range(10)],
    )
    monitor.registrar(
        conn, assunto="teste",
        observados=[(i * BARRA_MS, 1_000) for i in range(10, 20)],
    )
    aos_pedacos = monitor.serie(conn, "teste")
    assert aos_pedacos.passos[-1].s_milicents == de_uma_vez.passos[-1].s_milicents


# ===========================================================================
# O ESTADO DA 0C, e ele e DERIVADO
# ===========================================================================


def test_a_0c_nao_tem_sujeito_de_monitoramento(conn: sqlite3.Connection):
    ver = monitor.monitorar(conn, duracao_barra_ms=BARRA_MS)
    assert ver.estado == v.SEM_CONHECIMENTO
    assert ver.mediu is False
    assert "ADR 0034" in ver.motivo
    assert "R79" in ver.motivo


def test_o_estado_muda_sozinho_quando_houver_conhecimento_em_uso(
    conn: sqlite3.Connection,
):
    """A recusa e uma consulta: com uma hipotese validada, ele passa a medir."""
    h = _hipotese(conn)
    estados.registrar_entrada(conn, h, evidencia={"t": 1})
    for para in ("candidata", "em_quarentena", "conhecimento_validado"):
        estados.transitar(conn, h, para=para, evidencia={"t": 1})

    ver = monitor.monitorar(conn, duracao_barra_ms=BARRA_MS)
    assert ver.estado != v.SEM_CONHECIMENTO
    # Sem limiar congelado para ela, o monitor fica indisponivel - e nao
    # improvisa um.
    assert ver.estado == v.INDISPONIVEL
    assert "limiar improvisado" in ver.motivo


def test_conhecimento_em_uso_le_a_view_derivada(conn: sqlite3.Connection):
    assert monitor.conhecimento_em_uso(conn) == []
    assert monitor.ESTADOS_EM_USO == (
        "conhecimento_validado", "revalidado", "condicionado",
    )


# ===========================================================================
# O RELATORIO
# ===========================================================================


def test_o_relatorio_declara_a_insensibilidade_com_numero(
    conn: sqlite3.Connection,
):
    r = relatorio.montar(conn, duracao_barra_ms=BARRA_MS)
    assert r["estado"] == v.SEM_CONHECIMENTO
    assert r["ausencia_de_alarme_comprova_edge"] is False
    assert r["ausencia_de_alarme_promove_candidata"] is False
    assert r["conhecimento_em_uso"] == []
    assert "ADR 0034" in r["por_que_vazio"]

    tabela = r["diagnostico"]["arl0_pela_aproximacao_de_poisson"]
    cinco = [t for t in tabela if t["orcamento_ppm"] == 50_000][0]
    assert cinco["arl0_exigido_barras"] == 157_214
    assert r["diagnostico"]["reserva_barras"] == 8_064


def test_o_relatorio_declara_o_limite_de_variancia(conn: sqlite3.Connection):
    r = relatorio.montar(conn, duracao_barra_ms=BARRA_MS)
    assert "variancia" in r["limite_declarado"]
    assert r["estatistica"]["n_minimo_entra_na_taxa"] is False


def test_arl0_e_diagnostico_e_a_conta_confere():
    # -N/ln(1-p), conferido a mao para os quatro do ADR.
    assert limiar.arl0_exigido(prob=0.05, horizonte_barras=8_064) == 157_214
    assert limiar.arl0_exigido(prob=0.10, horizonte_barras=8_064) == 76_537
    assert limiar.arl0_exigido(prob=0.20, horizonte_barras=8_064) == 36_138


def test_o_relatorio_mostra_os_assuntos_congelados(conn: sqlite3.Connection):
    _congelar(conn)
    r = relatorio.montar(conn, duracao_barra_ms=BARRA_MS)
    (a,) = r["assuntos"]
    assert a["assunto"] == "teste"
    assert a["limiar"]["algoritmo"] == limiar.ALGORITMO
    assert a["lacuna_maxima_barras"] == BURACO_MAXIMO_BARRAS


# ===========================================================================
# O VINCULO ESTRUTURAL COM A ADMISSAO DA QUARENTENA
# ===========================================================================


def test_serie_que_nao_alcanca_o_alvo_deixa_o_monitor_surdo():
    """Deriva positiva sob H0 -> limiar cresce -> monitor honestamente surdo.

    E `m >= alvo` e exatamente o que a admissao da quarentena exige. A regua
    da quarentena e a sensibilidade do monitor sao a mesma condicao.
    """
    alvo, k = 2_379, 1_189
    fraca = _serie(1_000, 500, 1_000, semente=4)     # media muito abaixo
    forte = _serie(1_000, 5_000, 1_000, semente=4)   # media acima do alvo

    cal_fraca = limiar.calibrar(
        fraca, alvo_milicents_por_barra=alvo, folga_milicents_por_barra=k,
        horizonte_barras=800, repeticoes=150,
    )
    cal_forte = limiar.calibrar(
        forte, alvo_milicents_por_barra=alvo, folga_milicents_por_barra=k,
        horizonte_barras=800, repeticoes=150,
    )

    assert cal_fraca.surdo_por_deriva is True
    assert cal_forte.surdo_por_deriva is False
    assert cal_fraca.alerta_milicents > cal_forte.alerta_milicents * 10


def test_serie_curta_demais_nao_calibra():
    from app.calibracao.bootstrap import SerieCurtaDemais

    with pytest.raises(SerieCurtaDemais):
        limiar.calibrar(
            [1, 2, 3], alvo_milicents_por_barra=1, folga_milicents_por_barra=0,
            horizonte_barras=10,
        )
