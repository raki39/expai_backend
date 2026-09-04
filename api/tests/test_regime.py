"""Testes do detector de regimes (ADR 0026, incremento 17).

O ADR foi decidido sobre medicao feita ANTES de existir forward, e a minha
proposta original foi derrubada por ela. Vários testes aqui existem para
impedir que a proposta derrubada volte por descuido.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

from app.regime import deteccao

APP = Path(__file__).resolve().parents[1] / "app"
PASSO = 900_000  # 15 min em ms
DIA = 96         # barras de 15 min por dia


def serie(vals: list[int | None], *, inicio: int = 0) -> list[tuple[int, int | None]]:
    """Serie de retornos com carimbos ADJACENTES (sem lacuna)."""
    return [(inicio + i * PASSO, v) for i, v in enumerate(vals)]


# ------------------------------------------------- os numeros sao CONGELADOS

def test_os_cortes_sao_os_do_adr_0026():
    """19,3 e 25,3 bps por barra, em mili-bps.

    Se alguem mexer nestes numeros, toda comparacao que atravesse a mudanca
    fica invalida (secao 10.2.3) - e o ADR diz "cortes congelados antes do
    forward".
    """
    assert deteccao.CORTE_INFERIOR_MILI_BPS == 19_300
    assert deteccao.CORTE_SUPERIOR_MILI_BPS == 25_300
    assert deteccao.JANELA_BARRAS == 672           # 7 dias
    assert deteccao.PERMANENCIA_BARRAS == 672      # 7 dias CONSECUTIVOS
    assert deteccao.REGIMES_MINIMOS == 2


def test_a_janela_e_a_permanencia_sao_SETE_DIAS():
    assert deteccao.JANELA_BARRAS == 7 * DIA
    assert deteccao.PERMANENCIA_BARRAS == 7 * DIA


def test_o_detector_NAO_recalcula_os_cortes():
    """`derivar_cortes` existe para provar procedencia, nao para ser chamada.

    Recalcular em producao adaptaria a regua ao dado que fosse chegando, e
    "dois regimes distintos" viraria automatico - a definicao frouxa que
    secao 19.2 alerta.
    """
    fonte = (APP / "regime" / "deteccao.py").read_text(encoding="utf-8")
    arvore = ast.parse(fonte)
    for no in ast.walk(arvore):
        if not isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if no.name in ("derivar_cortes",):
            continue
        chamadas = [
            c.func.id for c in ast.walk(no)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        ]
        assert "derivar_cortes" not in chamadas, (
            f"{no.name} chama derivar_cortes - o detector usa os CONGELADOS"
        )


def test_a_direcao_nao_esta_no_detector():
    """A medicao derrubou o eixo de direcao, e a ausencia dele e deliberada.

    A celula conjunta herda a persistencia da dimensao MENOS persistente: so
    volatilidade da mediana de 10,1 dias, com direcao junto cai para 2,4.

    A direcao continua registrada em `condicoes_validade`, mas este modulo nao
    a produz - para que ninguem a use por engano na CONTAGEM de cobertura.
    """
    fonte = (APP / "regime" / "deteccao.py").read_text(encoding="utf-8")
    arvore = ast.parse(fonte)
    nomes = {
        n.name for n in ast.walk(arvore)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    # "deriva" NAO serve como palavra proibida: colide com `derivar_cortes`,
    # que e legitima. Foi o primeiro modo de falha desta guarda.
    assert "derivar_cortes" in nomes
    assert not any("direcao" in n or "drift" in n for n in nomes), nomes
    # E os regimes possiveis sao tres, todos de volatilidade.
    assert deteccao._classificar_vol(0) == "vol_baixa"
    assert deteccao._classificar_vol(22_000) == "vol_media"
    assert deteccao._classificar_vol(99_000) == "vol_alta"


# ---------------------------------------------------- classificacao por vol

def test_classifica_nos_tres_regimes():
    assert deteccao._classificar_vol(19_299) == "vol_baixa"
    assert deteccao._classificar_vol(19_300) == "vol_media"   # fronteira: nao e baixa
    assert deteccao._classificar_vol(25_300) == "vol_media"   # fronteira: nao e alta
    assert deteccao._classificar_vol(25_301) == "vol_alta"


def test_a_janela_e_CAUSAL_e_nao_inclui_a_barra_do_instante():
    """O regime de `t` usa so retornos ANTERIORES a `t`.

    Uma janela que incluisse `t` olharia para uma barra que, no instante da
    decisao, ainda nao fechou - e e a proibicao de secao 11.4 aplicada ao
    proprio detector.
    """
    janela = 4
    # Retornos pequenos, e um ENORME exatamente no instante classificado.
    vals: list[int | None] = [1_000] * janela + [900_000] + [1_000] * janela
    cls = deteccao.classificar_serie(serie(vals), janela=janela)

    # O instante em que o retorno gigante TERMINA (indice `janela`) e
    # classificado com a janela anterior a ele, que so tem retornos pequenos.
    no_instante = cls[janela]
    assert no_instante.regime == "vol_baixa", (
        "o retorno do proprio instante entrou na janela - vazamento"
    )
    # E o instante SEGUINTE ja sente o gigante.
    assert cls[janela + 1].regime == "vol_alta"


def test_janela_incompleta_sai_indefinida_com_motivo():
    cls = deteccao.classificar_serie(serie([1_000] * 10), janela=4)
    assert [c.regime for c in cls[:4]] == [None] * 4
    assert all(c.motivo == "janela_incompleta" for c in cls[:4])
    assert cls[4].regime is not None


# ------------------------------------------ subsecao 3b: lacunas nos retornos

def test_retorno_que_atravessa_lacuna_nao_entra_na_conta():
    """`None` na serie nao vira zero, e nao vira valor imputado.

    O vies de trata-lo como retorno normal e SEMPRE positivo - o retorno que
    salta `d` barras tem variancia `(d+1)*sigma^2` e entra como `sigma^2`. E
    volatilidade inflada FABRICA transicao de regime, tornando a cobertura
    mais facil: a direcao de aprovar.
    """
    janela = 100
    limpa = deteccao.classificar_serie(serie([20_000] * (janela + 1)), janela=janela)
    vol_limpa = limpa[janela].vol_mili_bps

    # Uma barra ausente dentro da tolerancia (1 de 100 = 1%).
    com_falta: list[int | None] = [20_000] * (janela + 1)
    com_falta[50] = None
    cf = deteccao.classificar_serie(serie(com_falta), janela=janela)
    c = cf[janela]

    assert c.regime is not None, "1% ausente esta dentro da tolerancia"
    assert c.retornos_ausentes == 1
    assert c.retornos_usados == janela - 1
    # O ausente foi EXCLUIDO, e nao contado como zero: se fosse zero, a
    # variancia de uma serie constante deixaria de ser zero.
    assert c.vol_mili_bps == vol_limpa == 0


def test_lacuna_acima_da_tolerancia_invalida_a_janela():
    janela = 100
    vals: list[int | None] = [20_000] * (janela + 1)
    for i in (10, 20, 30):          # 3 de 100 = 3% > 1%
        vals[i] = None
    c = deteccao.classificar_serie(serie(vals), janela=janela)[janela]
    assert c.regime is None
    assert c.motivo == "lacuna_acima_da_tolerancia"
    assert c.vol_mili_bps is None, "janela invalida nao publica volatilidade"


def test_buraco_continuo_longo_invalida_mesmo_dentro_da_fracao():
    """5 barras seguidas ausentes em 1000 sao 0,5% - mas o buraco passa de 1 h.

    As duas condicoes existem porque 6 ausentes espalhados e 6 seguidos sao
    coisas diferentes: o segundo e uma interrupcao real.
    """
    janela = 1_000
    vals: list[int | None] = [20_000] * (janela + 1)
    for i in range(100, 105):       # 5 seguidas > BURACO_MAXIMO_BARRAS (4)
        vals[i] = None
    c = deteccao.classificar_serie(serie(vals), janela=janela)[janela]
    assert c.regime is None
    assert c.motivo == "buraco_continuo_longo"


# ----------------------------------------------------------------- episodios

def test_episodio_exige_permanencia_CONSECUTIVA():
    """Corrida menor que a permanencia e descartada.

    E a "variacao cosmetica" que secao 19.2 manda nao contar como regime novo.
    """
    p = 10
    vals: list[int | None] = [0, 2_000] * 100   # vol baixa, longa (ALTERNA)
    cls = deteccao.classificar_serie(serie(vals), janela=5)
    eps = deteccao.episodios(cls, permanencia=p)
    assert len(eps) == 1 and eps[0].regime == "vol_baixa"
    assert eps[0].barras >= p

    # Agora uma corrida CURTA de outro regime no meio: nao vira episodio.
    curta = [0, 2_000] * 50 + [0, 900_000, 0] + [0, 2_000] * 50
    cls2 = deteccao.classificar_serie(serie(curta), janela=5)
    eps2 = deteccao.episodios(cls2, permanencia=p)
    assert all(e.regime == "vol_baixa" for e in eps2), (
        f"a oscilacao de 3 barras virou episodio: {[e.regime for e in eps2]}"
    )


def test_indefinido_QUEBRA_o_episodio_e_nao_e_ponte():
    """Se nao se observou, nao se afirma continuidade.

    Sem isto, uma interrupcao no meio de um episodio de 7 dias o costuraria
    como se o mercado tivesse sido observado o tempo todo.
    """
    p = 20
    # 30 iguais, um trecho indefinido, 30 iguais. Nenhum lado alcanca 20+20.
    cls = (
        [deteccao.Classificacao(i * PASSO, "vol_media", 22_000, 10, 0, None)
         for i in range(30)]
        + [deteccao.Classificacao((30 + i) * PASSO, None, None, 0, 99,
                                  "lacuna_acima_da_tolerancia") for i in range(5)]
        + [deteccao.Classificacao((35 + i) * PASSO, "vol_media", 22_000, 10, 0, None)
           for i in range(30)]
    )
    eps = deteccao.episodios(cls, permanencia=p)
    assert len(eps) == 2, "o indefinido virou ponte e uniu os dois trechos"
    assert all(e.barras == 30 for e in eps)

    # E com permanencia maior que cada lado, nenhum episodio sobrevive.
    assert deteccao.episodios(cls, permanencia=40) == []


# ----------------------------------------------------------------- cobertura

def test_cobertura_exige_DOIS_regimes_distintos():
    p = 10
    so_um = deteccao.classificar_serie(serie([0, 2_000] * 50), janela=5)
    cv = deteccao.cobertura(so_um, permanencia=p)
    assert cv["quantidade"] == 1
    assert cv["cumprida"] is False, "um regime so nao cumpre a cobertura"

    # ATENCAO: serie CONSTANTE tem desvio-padrao ZERO, seja o valor 1.000 ou
    # 900.000. A primeira versao deste teste usava `[900_000] * 100` esperando
    # vol alta e recebia vol BAIXA - passando/falhando por motivo diferente do
    # que afirmava. Volatilidade exige VARIACAO, e por isso os blocos alternam.
    dois = deteccao.classificar_serie(
        serie([0, 2_000] * 50 + [0, 90_000] * 50), janela=5
    )
    cv2 = deteccao.cobertura(dois, permanencia=p)
    assert cv2["quantidade"] == 2
    assert cv2["cumprida"] is True
    assert set(cv2["regimes_cobertos"]) == {"vol_baixa", "vol_alta"}


def test_cobertura_e_travamento_INDEPENDENTE_de_amostra():
    """Amostra enorme com um regime so continua nao cumprindo.

    E o ponto de secao 8.5: sem isto, "uma estrategia de altissima frequencia
    declararia n_minimo atingido em duas horas de um unico regime de mercado e
    sairia da quarentena tendo aprendido nada sobre robustez".
    """
    enorme = deteccao.classificar_serie(serie([0, 2_000] * 10_000), janela=5)
    cv = deteccao.cobertura(enorme, permanencia=deteccao.PERMANENCIA_BARRAS)
    assert cv["episodios"] >= 1
    assert cv["quantidade"] == 1
    assert cv["cumprida"] is False


def test_cobertura_nao_promove_nada():
    """Este modulo diz o FATO; a consequencia e de quem tem autoridade.

    A guarda varre CODIGO, nao prosa. A primeira versao varria texto cru e
    acusou o proprio comentario que explica a fronteira - o defeito que o
    incremento 11 ja registrou.
    """
    codigo = _sem_prosa((APP / "regime" / "deteccao.py").read_text(encoding="utf-8"))
    for proibido in ("promov", "hypothesis_state", "transicao", "parecer"):
        assert proibido not in codigo.lower(), (
            f"o detector CHAMA algo com {proibido!r} - promocao e do validador"
        )


# ---------------------------------------------------------------- derivacao

def test_a_derivacao_reproduz_tercis_conhecidos():
    """Serie construida para ter tercis exatos."""
    janela = 10
    # Tres blocos de volatilidade crescente, longos o bastante para encher
    # janelas de cada nivel.
    vals: list[int | None] = (
        [0, 10_000] * 200        # alterna: vol ~5.000
        + [0, 40_000] * 200      # vol ~20.000
        + [0, 80_000] * 200      # vol ~40.000
    )
    lo, hi = deteccao.derivar_cortes(serie(vals), janela=janela)
    assert 0 < lo < hi
    # Os cortes caem entre os niveis construidos.
    assert 5_000 < lo < 40_000
    assert 20_000 < hi < 80_000


def test_derivacao_recusa_serie_curta_em_vez_de_inventar():
    with pytest.raises(deteccao.DatasetSemGrade):
        deteccao.derivar_cortes(serie([1_000] * 5), janela=4)


# ------------------------------------------------- fronteira do incremento 9

def test_o_detector_nao_le_barra_direto():
    """A fronteira do incremento 9 nao tem excecao.

    O validador ja tentou fura-la duas vezes - uma com `SELECT ... FROM bar`
    dentro de si sob um comentario afirmando que nao era excecao, e outra
    lendo quantas barras o holdout tem. Quem le barra e o `loader`.
    """
    fonte = (APP / "regime" / "deteccao.py").read_text(encoding="utf-8")
    sem_prosa = _sem_prosa(fonte)
    assert "FROM bar" not in sem_prosa
    assert "SELECT" not in sem_prosa.upper()


def _sem_prosa(fonte: str) -> str:
    """Remove COMENTARIO e docstring, por `tokenize`.

    Nao e zelo. A primeira versao desta guarda buscava texto cru e acusou o
    proprio COMENTARIO que explica o que ela proibe - "nenhum caminho promove
    com um regime so" contem "promove".

    E o defeito exato que o incremento 11 registrou em `app/estatistica`: uma
    guarda que proibe a explicacao de por que algo e proibido empurra para
    apagar o comentario, e o comentario e justamente a parte que se quer
    manter. A correcao la foi `codigo_sem_prosa`, e e a mesma daqui.
    """
    import io
    import tokenize

    saida: list[str] = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(fonte).readline))
    except tokenize.TokenError:
        return fonte
    inicio_de_linha = True
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and inicio_de_linha:
            continue                       # docstring: sozinha na linha
        if tok.type in (tokenize.NEWLINE, tokenize.NL):
            inicio_de_linha = True
        elif tok.type not in (tokenize.INDENT, tokenize.DEDENT):
            inicio_de_linha = False
        saida.append(tok.string)
    return " ".join(saida)


def test_a_guarda_de_fronteira_nao_e_vazia():
    """Ela precisa remover docstring E ainda encontrar codigo."""
    fonte = (APP / "regime" / "deteccao.py").read_text(encoding="utf-8")
    sem = _sem_prosa(fonte)
    assert "def classificar_serie" in sem
    assert len(sem) > 1_000
    # E a prosa do modulo, que EXPLICA a fronteira, foi de fato removida.
    assert "SELECT" not in sem.upper()


# ------------------------------------------------------ retornos causais (3b)

def test_retorno_que_salta_no_tempo_vira_None():
    """A regra da subsecao 3b, no nivel de quem conhece a grade de barras."""
    from app.dataset import loader

    assert loader.ESCALA_MILI_BPS == 1_000
    fonte = (APP / "dataset" / "loader.py").read_text(encoding="utf-8")
    i = fonte.index("def retornos_causais")
    corpo = fonte[i:i + 3_000]
    assert "!= passo" in corpo, "sem a checagem de adjacencia temporal nao ha 3b"


def test_desvio_padrao_de_serie_constante_e_zero():
    assert deteccao._desvio_padrao_mili_bps([5, 5, 5, 5]) == 0


def test_desvio_padrao_conhecido():
    """[0, 20.000] alterna: media 10.000, desvio 10.000."""
    assert deteccao._desvio_padrao_mili_bps([0, 20_000] * 50) == 10_000


def test_maior_buraco():
    assert deteccao._maior_buraco([True] * 10) == 0
    assert deteccao._maior_buraco([True, False, True, False, False, True]) == 2
    assert deteccao._maior_buraco([False] * 7) == 7
