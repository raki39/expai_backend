"""Extracao alinhada a grade e manifesto do bruto (ADR 0032, incremento 18)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from coletor import arquivo, entrega, envio, extracao
from coletor.arquivo import Diario

GRADE = 900_000
T0 = 1_756_000_000_000 // GRADE * GRADE

# O offset MEDIDO numa maquina real (ADR 0028): -2.450 ms. Ele passa da
# tolerancia inteira de 2 s, e e por isso que a correcao nao e refinamento.
OFFSET_MS = -2_450.0


def linha_relogio(offset_ms: float = OFFSET_MS, ns: int = 0) -> dict:
    return {
        "tipo": "relogio", "medido_em_ns": ns, "rtt_ms": 12.0,
        "offset_ms": offset_ms, "incerteza_bruta_ms": abs(offset_ms) + 6.0,
        "incerteza_residual_ms": 6.0, "server_time_ms": 0,
    }


def linha_amostra(
    received_ms: int, *, ask: str = "60001.50", bid: str = "60000.50",
    u: int = 1, disponivel: bool = True, motivo: str | None = None,
) -> dict:
    ns = received_ms * 1_000_000
    return {
        "sampled_at_ns": ns + 1_000_000, "received_at_ns": ns,
        "idade_ms": 1, "u": u,
        "bid": bid if disponivel else None,
        "bid_qty": "5.0" if disponivel else None,
        "ask": ask if disponivel else None,
        "ask_qty": "7.0" if disponivel else None,
        "disponivel": disponivel, "motivo": motivo,
        "u_duplicado": False, "u_regrediu": False, "delta_u": 1,
    }


def escrever(tmp_path: Path, linhas: list[dict], dia: str | None = None) -> list[Path]:
    """Grava as linhas num arquivo do coletor, no formato real.

    O nome do dia sai de `arquivo.dia_utc`, e nao de uma data que eu
    escreva a mao: escrevi "2026-08-24" para um T0 que cai em 2025, e os
    dois testes que dependiam do nome falharam por isso.
    """
    if dia is None:
        dia = arquivo.dia_utc(T0 * 1_000_000)
    caminho = tmp_path / f"bookticker-btcusdt-{dia}.jsonl.gz"
    import gzip
    with gzip.GzipFile(filename=str(caminho), mode="wb", compresslevel=6, mtime=0) as f:
        for l in linhas:
            f.write((json.dumps(l, separators=(",", ":")) + "\n").encode())
    return [caminho]


# ===========================================================================
# A regra: a ULTIMA amostra com corrigido <= t_grid
# ===========================================================================


def test_escolhe_a_ULTIMA_amostra_antes_do_instante(tmp_path):
    """A mais proxima por baixo - nao a primeira, e nao uma media.

    Media de duas cotacoes e uma cotacao que nunca existiu.
    """
    # Com offset -2450, `corrigido = recebido - 2450`. Para o corrigido cair
    # 500 ms antes de T0, o recebido tem de ser T0 - 500 + 2450.
    linhas = [
        linha_relogio(),
        linha_amostra(T0 - 5_000 + 2_450, ask="60000.00"),
        linha_amostra(T0 - 500 + 2_450, ask="60009.00"),   # <- esta
        linha_amostra(T0 + 3_000 + 2_450, ask="60099.00"),  # posterior
    ]
    obs = extracao.extrair(escrever(tmp_path, linhas),
                           de_ms=T0, ate_ms_exclusive=T0 + GRADE)
    assert len(obs) == 1
    assert obs[0].disponivel
    assert obs[0].ask == 6000900000000, "pegou a amostra errada"
    assert obs[0].defasagem_ms == 500


def test_NUNCA_usa_amostra_posterior_ao_instante(tmp_path):
    """Seria ler o futuro do instante."""
    linhas = [
        linha_relogio(),
        linha_amostra(T0 + 1 + 2_450, ask="60099.00"),
    ]
    obs = extracao.extrair(escrever(tmp_path, linhas),
                           de_ms=T0, ate_ms_exclusive=T0 + GRADE)
    assert obs[0].disponivel is False
    assert obs[0].motivo == "sem_amostra_na_janela"
    assert obs[0].ask is None


def test_alem_da_tolerancia_sai_DEFASADA_e_sem_preco(tmp_path):
    linhas = [
        linha_relogio(),
        linha_amostra(T0 - 2_001 + 2_450),
    ]
    obs = extracao.extrair(escrever(tmp_path, linhas),
                           de_ms=T0, ate_ms_exclusive=T0 + GRADE)
    assert obs[0].disponivel is False
    assert obs[0].motivo == "defasada"
    assert (obs[0].bid, obs[0].ask) == (None, None)


def test_O_RELOGIO_CORRIGIDO_MUDA_O_VEREDITO(tmp_path):
    """O teste que justifica o requisito 2 inteiro.

    Uma amostra recebida 1.000 ms antes do instante PARECE valida pelo relogio
    local. Com o offset real de -2.450 ms, o horario dela na escala da
    exchange e 3.450 ms antes - **alem da tolerancia**.

    Sem a correcao, esta observacao entraria na calibracao como boa, medindo o
    `ask` de um instante que nao e o da execucao. Com ela, sai indisponivel.
    """
    linhas = [linha_relogio(), linha_amostra(T0 - 1_000)]
    obs = extracao.extrair(escrever(tmp_path, linhas),
                           de_ms=T0, ate_ms_exclusive=T0 + GRADE)
    assert obs[0].disponivel is False, (
        "sem corrigir o relogio, esta amostra passaria por valida"
    )
    assert obs[0].motivo == "defasada"


def test_SEM_medida_de_relogio_anterior_a_observacao_nao_sai(tmp_path):
    """Corrigir por zero seria assumir deriva zero.

    E o usuario recusou isso explicitamente ao fechar o ADR 0028 - o offset
    medido em maquina real passa da tolerancia inteira.
    """
    linhas = [linha_amostra(T0 - 500)]  # amostra ANTES de qualquer relogio
    obs = extracao.extrair(escrever(tmp_path, linhas),
                           de_ms=T0, ate_ms_exclusive=T0 + GRADE)
    assert obs[0].disponivel is False
    assert obs[0].ask is None


def test_amostra_indisponivel_do_coletor_propaga_o_MOTIVO(tmp_path):
    linhas = [
        linha_relogio(),
        linha_amostra(T0 - 500 + 2_450, disponivel=False, motivo="desconectado"),
    ]
    obs = extracao.extrair(escrever(tmp_path, linhas),
                           de_ms=T0, ate_ms_exclusive=T0 + GRADE)
    assert obs[0].disponivel is False
    assert obs[0].motivo == "desconectado"


def test_TODO_instante_produz_observacao(tmp_path):
    """Pular o instante ruim faria a cobertura parecer perfeita onde nao e."""
    linhas = [linha_relogio(), linha_amostra(T0 - 500 + 2_450)]
    obs = extracao.extrair(escrever(tmp_path, linhas),
                           de_ms=T0, ate_ms_exclusive=T0 + 5 * GRADE)
    assert [o.t_grid_ms for o in obs] == [T0 + i * GRADE for i in range(5)]
    assert sum(o.disponivel for o in obs) == 1
    assert all(o.motivo for o in obs if not o.disponivel)


def test_preco_vira_inteiro_por_DECIMAL(tmp_path):
    """Regra 5. `float("0.1") * 10**8` nao da 10000000 exato em toda plataforma.

    A `api` compara estes inteiros por IGUALDADE contra o que ja gravou, entao
    duas extracoes discordantes apareceriam como corrupcao de dado.
    """
    linhas = [linha_relogio(), linha_amostra(T0 - 500 + 2_450, ask="0.1", bid="0.1")]
    obs = extracao.extrair(escrever(tmp_path, linhas),
                           de_ms=T0, ate_ms_exclusive=T0 + GRADE)
    assert obs[0].ask == 10_000_000
    assert isinstance(obs[0].ask, int)


def test_a_extracao_e_DETERMINISTICA(tmp_path):
    """E o que torna honesta a recusa de conteudo divergente na mesma chave."""
    linhas = [linha_relogio()] + [
        linha_amostra(T0 - 500 + 2_450 + i * GRADE) for i in range(4)
    ]
    arquivos = escrever(tmp_path, linhas)
    a = extracao.extrair(arquivos, de_ms=T0, ate_ms_exclusive=T0 + 4 * GRADE)
    b = extracao.extrair(arquivos, de_ms=T0, ate_ms_exclusive=T0 + 4 * GRADE)
    assert a == b


def test_a_escala_e_a_MESMA_da_ingestao_historica_e_do_rele():
    assert extracao.PRICE_SCALE_EXP == 8
    assert extracao.VOLUME_SCALE_EXP == 8


def test_o_contrato_do_coletor_bate_com_o_da_api():
    """Duas copias em repositorios diferentes, e este teste e a costura."""
    assert extracao.CONTRATO == "bbo@1"
    assert extracao.GRADE_MS == 900_000
    assert extracao.TOLERANCIA_MS == 2_000


def test_a_regra_do_horario_corrigido_e_a_mesma_dos_dois_lados():
    """A formula existe em dois repositorios. Este teste fixa o valor.

    `//` do Python arredonda para BAIXO, e e a semantica que vale. Ha teste
    identico do lado da `api`, e os dois quebram se um dos lados mudar.
    """
    base = 1_000_000_000_000
    assert extracao.corrigir(base, -2_450_000) == base - 2_450
    assert extracao.corrigir(base, -2_450_500) == base - 2_451
    assert extracao.corrigir(base, 2_450_500) == base + 2_450


# ===========================================================================
# Requisito 6: manifesto do arquivo bruto
# ===========================================================================


def test_o_manifesto_carrega_hash_contagem_e_fronteiras(tmp_path):
    linhas = [linha_relogio(ns=1_000)] + [
        linha_amostra(T0 + i) for i in range(3)
    ]
    caminho = escrever(tmp_path, linhas)[0]
    m = arquivo.manifesto(caminho)
    assert len(m["sha256"]) == 64
    assert m["linhas"] == 4
    assert m["truncado"] is False
    assert m["primeiro_ns"] == 1_000
    assert m["ultimo_ns"] == (T0 + 2) * 1_000_000 + 1_000_000


def test_o_manifesto_NAO_e_sobrescrito(tmp_path):
    """Um manifesto que muda nao e manifesto.

    Se o arquivo divergir do hash selado, isso tem de APARECER na conferencia
    - e nao ser apagado por um manifesto novo.
    """
    caminho = escrever(tmp_path, [linha_amostra(T0)])[0]
    arquivo.escrever_manifesto(caminho)
    primeiro = arquivo.caminho_do_manifesto(caminho).read_text(encoding="utf-8")

    with caminho.open("ab") as f:
        f.write(b"lixo")
    arquivo.escrever_manifesto(caminho)
    assert arquivo.caminho_do_manifesto(caminho).read_text(encoding="utf-8") == primeiro
    assert arquivo.conferir_manifesto(caminho) is False, (
        "a alteracao do bruto tem de aparecer, e nao ser apagada"
    )


def test_conferir_manifesto_sem_manifesto_e_None(tmp_path):
    caminho = escrever(tmp_path, [linha_amostra(T0)])[0]
    assert arquivo.conferir_manifesto(caminho) is None


def test_selar_nao_toca_o_dia_CORRENTE(tmp_path):
    """Um arquivo que ainda recebe linhas nao tem hash estavel."""
    ontem = arquivo.dia_utc((T0 - 86_400_000) * 1_000_000)
    hoje = arquivo.dia_utc(T0 * 1_000_000)
    escrever(tmp_path, [linha_amostra(T0)], dia=ontem)
    escrever(tmp_path, [linha_amostra(T0)], dia=hoje)
    selados = arquivo.selar_dias_fechados(tmp_path, "bookticker-btcusdt", hoje=hoje)
    assert [p.name for p in selados] == [f"bookticker-btcusdt-{ontem}.jsonl.gz"]
    assert arquivo.caminho_do_manifesto(
        tmp_path / f"bookticker-btcusdt-{hoje}.jsonl.gz").exists() is False


def test_o_Diario_sela_o_dia_ao_VIRAR(tmp_path):
    """A rotacao e o unico instante em que o arquivo do dia fica estavel."""
    d = Diario(tmp_path, "bookticker-btcusdt")
    d.escrever({"x": 1}, ns=1_756_000_000_000_000_000)          # 2026-08-24
    d.escrever({"x": 2}, ns=1_756_000_000_000_000_000 + 86_400_000_000_000)
    d.fechar()
    manifestos = sorted(p.name for p in tmp_path.glob("*.manifesto.json"))
    assert len(manifestos) == 1, f"selou {manifestos}"


# ===========================================================================
# A entrega: postura diferente da do rele, e ela e deliberada
# ===========================================================================


def test_sem_credencial_a_COLETA_continua(monkeypatch):
    """O rele falha FECHADO; o coletor nao pode.

    A entrega e derivada e refazivel do arquivo bruto. A COLETA nao e - um
    segundo de BBO nao gravado esta perdido para sempre. Recusar a subir
    trocaria uma perda irrecuperavel por uma recuperavel.
    """
    assert entrega.destino_do_ambiente({}) is None
    assert entrega.destino_do_ambiente({"COLETOR_API_URL": "http://x"}) is None
    d = entrega.destino_do_ambiente({
        "COLETOR_API_URL": "http://x/", "API_SERVICE_TOKEN": "t",
        "COLETOR_HMAC_SECRET": "s",
    })
    assert d == envio.Destino(base_url="http://x", token="t", segredo="s")


def test_a_fronteira_da_extracao_e_a_barra_CORRENTE(tmp_path):
    """Para um instante estar resolvido, tem de existir amostra depois dele.

    Extrair ate `agora` concluiria cedo demais: a proxima amostra a chegar
    poderia ser a candidata certa do ultimo instante.
    """
    linhas = [linha_relogio()] + [
        linha_amostra(T0 - 500 + 2_450 + i * GRADE) for i in range(4)
    ]
    escrever(tmp_path, linhas)

    chamado = {}

    def falso_ponto(*_a, **_k):
        return {"retomar_de_ms": T0, "grade_ms": GRADE, "tolerancia_ms": 2_000}

    def falso_enviar(_d, **kw):
        chamado["obs"] = kw["observacoes"]
        return {"aceitas": len(kw["observacoes"]), "repetidas": 0}

    import coletor.entrega as m
    original_ponto, original_enviar = m.envio.ponto_de_retomada, m.envio.enviar
    m.envio.ponto_de_retomada, m.envio.enviar = falso_ponto, falso_enviar
    try:
        # `agora` no meio do instante 3: fechados sao 0, 1, 2.
        agora = T0 + 3 * GRADE + 60_000
        r = m.uma_volta(
            envio.Destino("http://x", "t", "s"), tmp_path,
            "bookticker-btcusdt", venue="binance", symbol="BTCUSDT",
            agora_ms=agora,
        )
    finally:
        m.envio.ponto_de_retomada, m.envio.enviar = original_ponto, original_enviar

    assert [o.t_grid_ms for o in chamado["obs"]] == [
        T0, T0 + GRADE, T0 + 2 * GRADE
    ], "o instante em formacao entrou"
    assert r["enviadas"] == 3


def test_so_le_os_arquivos_dos_dias_que_o_periodo_toca(tmp_path):
    """Ler o diretorio inteiro ficaria mais lento a cada mes de coleta."""
    de = T0 // 86_400_000 * 86_400_000          # meia-noite UTC do dia de T0
    dias = [arquivo.dia_utc((de + i * 86_400_000) * 1_000_000) for i in range(3)]
    for dia in dias:
        escrever(tmp_path, [linha_amostra(T0)], dia=dia)
    ate = de + 86_400_000                       # +1 dia
    achados = entrega.arquivos_do_periodo(tmp_path, "bookticker-btcusdt", de, ate)
    assert [p.name for p in achados] == [
        f"bookticker-btcusdt-{dias[0]}.jsonl.gz",
        f"bookticker-btcusdt-{dias[1]}.jsonl.gz",
    ], "o terceiro dia nao e tocado pelo periodo"


# ===========================================================================
# O defeito que a producao pegou, em 2026-09-07
# ===========================================================================


def linha_relogio_FALHADA(ns: int = 1) -> dict:
    """A linha EXATA que `fluxo.sondar_relogio` grava quando a sonda falha.

    Copiada do codigo, e nao imaginada: `{tipo, medido_em_ns, falha}`. Ela nao
    tem `offset_ms`, e isso e deliberado - "nao inventa valor, e um relogio nao
    medido e um relogio nao medido".
    """
    return {"tipo": "relogio", "medido_em_ns": ns, "falha": "451 bloqueio"}


def test_sonda_FALHADA_nao_quebra_a_extracao(tmp_path):
    """`KeyError: 'offset_ms'` na PRIMEIRA volta em producao.

    Eu supus que toda linha `tipo: relogio` trouxesse medicao. A linha de
    falha existe de proposito e nao traz - e o defeito so apareceu quando a
    entrega foi ligada, porque ate entao ninguem lia essas linhas.
    """
    linhas = [
        linha_relogio(),
        linha_relogio_FALHADA(),
        linha_amostra(T0 - 500 + 2_450),
    ]
    obs = extracao.extrair(escrever(tmp_path, linhas),
                           de_ms=T0, ate_ms_exclusive=T0 + GRADE)
    assert obs[0].disponivel is True


def test_sonda_falhada_MANTEM_a_ultima_medida_boa(tmp_path):
    """E o certo, e nao so o conveniente.

    A sonda falhar informa sobre a REDE ate a Binance, e nao sobre o relogio
    local. O offset varia devagar; a falha nao o torna desconhecido, so o
    deixa mais VELHO - e a idade vai gravada, para que ninguem precise supor.
    """
    linhas = [
        linha_relogio(offset_ms=-2_450.0, ns=1_000_000),
        linha_relogio_FALHADA(ns=2_000_000),
        linha_amostra(T0 - 500 + 2_450),
    ]
    obs = extracao.extrair(escrever(tmp_path, linhas),
                           de_ms=T0, ate_ms_exclusive=T0 + GRADE)
    assert obs[0].offset_us == -2_450_000, "perdeu a medida boa"
    assert obs[0].relogio_medido_em_ms == 1, (
        "a idade tem de ser a da medida BOA, e nao a da falha"
    )


def test_SO_falhas_deixa_o_instante_sem_cotacao(tmp_path):
    """Sem nenhuma medida boa, corrigir por zero seria assumir deriva zero."""
    linhas = [linha_relogio_FALHADA(), linha_amostra(T0 - 500)]
    obs = extracao.extrair(escrever(tmp_path, linhas),
                           de_ms=T0, ate_ms_exclusive=T0 + GRADE)
    assert obs[0].disponivel is False
    assert obs[0].ask is None


def test_a_IDADE_do_relogio_atravessa(tmp_path):
    """Sem ela, seis horas e trinta segundos ficam indistinguiveis."""
    linhas = [linha_relogio(ns=(T0 - 60_000) * 1_000_000),
              linha_amostra(T0 - 500 + 2_450)]
    obs = extracao.extrair(escrever(tmp_path, linhas),
                           de_ms=T0, ate_ms_exclusive=T0 + GRADE)
    assert obs[0].relogio_medido_em_ms == T0 - 60_000
    assert "relogio_medido_em_ms" in obs[0].como_corpo()


def test_a_primeira_extracao_nao_varre_ANTES_de_o_coletor_existir(tmp_path):
    """Medido em producao: 301 dos 367 indisponiveis eram desse periodo.

    A janela inicial de 7 dias alcancou 2026-08-31 e a coleta liga em
    2026-09-04 - e "nao havia coletor" foi gravado com o mesmo motivo de "nao
    havia cotacao". Ausencia de observacao e observacao de ausencia sao coisas
    diferentes, e contar as duas juntas faz a cobertura descrever a nossa data
    de deploy em vez do mercado.
    """
    dia = arquivo.dia_utc(T0 * 1_000_000)
    escrever(tmp_path, [linha_relogio(), linha_amostra(T0 - 500 + 2_450)], dia=dia)

    pedidos = {}

    def falso_ponto(*_a, **_k):
        return {"retomar_de_ms": None, "grade_ms": GRADE, "tolerancia_ms": 2_000}

    def falso_enviar(_d, **kw):
        pedidos["obs"] = kw["observacoes"]
        return {"aceitas": len(kw["observacoes"]), "repetidas": 0}

    import coletor.entrega as m
    orig = m.envio.ponto_de_retomada, m.envio.enviar
    m.envio.ponto_de_retomada, m.envio.enviar = falso_ponto, falso_enviar
    try:
        m.uma_volta(
            envio.Destino("http://x", "t", "s"), tmp_path, "bookticker-btcusdt",
            venue="binance", symbol="BTCUSDT", agora_ms=T0 + 4 * GRADE,
        )
    finally:
        m.envio.ponto_de_retomada, m.envio.enviar = orig

    meia_noite = T0 // 86_400_000 * 86_400_000
    assert pedidos["obs"][0].t_grid_ms >= meia_noite, (
        "varreu para tras do primeiro dia com arquivo"
    )


def test_sem_arquivo_nenhum_a_volta_nao_inventa_periodo(tmp_path):
    def falso_ponto(*_a, **_k):
        return {"retomar_de_ms": None, "grade_ms": GRADE, "tolerancia_ms": 2_000}

    import coletor.entrega as m
    orig = m.envio.ponto_de_retomada
    m.envio.ponto_de_retomada = falso_ponto
    try:
        r = m.uma_volta(
            envio.Destino("http://x", "t", "s"), tmp_path, "bookticker-btcusdt",
            venue="binance", symbol="BTCUSDT", agora_ms=T0 + 4 * GRADE,
        )
    finally:
        m.envio.ponto_de_retomada = orig
    assert r["enviadas"] == 0
    # `estado`, e nao `motivo`: o campo virou uma lista fechada quando a volta
    # ociosa passou a falar, porque o log precisa distinguir "nada novo" de
    # "nenhum arquivo" - e texto livre nao distingue nada.
    assert r["estado"] == "sem_arquivo"


# ===========================================================================
# Toda volta fala: "ocioso" e "quebrado" nao podem ter a mesma cara
# ===========================================================================


def _volta(tmp_path, *, retomar, agora_ms, enviar=None):
    import coletor.entrega as m

    def falso_ponto(*_a, **_k):
        return {"retomar_de_ms": retomar, "grade_ms": GRADE,
                "tolerancia_ms": 2_000}

    orig = m.envio.ponto_de_retomada, m.envio.enviar
    m.envio.ponto_de_retomada = falso_ponto
    m.envio.enviar = enviar or (lambda _d, **kw: {
        "aceitas": len(kw["observacoes"]), "repetidas": 0})
    try:
        return m.uma_volta(
            envio.Destino("http://x", "t", "s"), tmp_path, "bookticker-btcusdt",
            venue="binance", symbol="BTCUSDT", agora_ms=agora_ms,
        )
    finally:
        m.envio.ponto_de_retomada, m.envio.enviar = orig


def test_volta_sem_nada_novo_diz_EM_DIA_e_nao_fica_calada(tmp_path):
    """Com grade de 15 min e laco de 5, DUAS EM TRES voltas nao enviam nada.

    Enquanto so a volta produtiva falava, "funcionando e ocioso" ficava
    indistinguivel de "quebrado e calado" - e foi exatamente essa a duvida que
    a primeira operacao real produziu.
    """
    dia = arquivo.dia_utc(T0 * 1_000_000)
    escrever(tmp_path, [linha_relogio(), linha_amostra(T0 - 500 + 2_450)], dia=dia)
    r = _volta(tmp_path, retomar=T0 + GRADE, agora_ms=T0 + GRADE + 60_000)
    assert r["estado"] == "em_dia"
    assert r["atraso_instantes"] == 0


def test_sem_arquivo_e_ALERTA_e_nao_silencio(tmp_path):
    """O coletor esta gravando agora. Se nenhum arquivo cobre o periodo, ou o
    prefixo mudou, ou o volume sumiu - e nada disso pode sair pelo mesmo
    silencio de uma volta ociosa.
    """
    r = _volta(tmp_path, retomar=T0, agora_ms=T0 + 4 * GRADE)
    assert r["estado"] == "sem_arquivo"
    assert r["atraso_instantes"] == 4, "o atraso e visivel mesmo sem arquivo"


def test_o_ATRASO_vai_em_toda_volta(tmp_path):
    """E o numero que separa atraso de lacuna, que e a distincao do ADR 0029."""
    dia = arquivo.dia_utc(T0 * 1_000_000)
    linhas = [linha_relogio()] + [
        linha_amostra(T0 - 500 + 2_450 + i * GRADE) for i in range(4)
    ]
    escrever(tmp_path, linhas, dia=dia)
    r = _volta(tmp_path, retomar=T0, agora_ms=T0 + 3 * GRADE + 60_000)
    assert r["estado"] == "entregue"
    assert r["enviadas"] == 3
    assert r["atraso_instantes"] == 0, "entregou tudo o que fechou"
    assert r["ultimo_entregue_ms"] == T0 + 2 * GRADE


# ===========================================================================
# O BLOCO DEFLATE DANIFICADO NO MEIO — o crash de 2026-09-08
#
# `zlib.error: Error -3 while decompressing data: invalid block type`, no boot,
# em ciclo. A causa e uma so e ela e de TIPO: `gzip.BadGzipFile` e um
# `OSError`; `zlib.error` NAO E - herda de `Exception` direto. O `try` que
# protegia a conferencia listava os tres primeiros e nao o quarto.
#
# E a ironia: `ler` foi escrita para exatamente esse caso e trata `zlib.error`
# sob um comentario dizendo "lixo no meio do fluxo". O leitor sabia; a
# conferencia ao lado dele nao.
# ===========================================================================


def _com_bloco_danificado(caminho: Path) -> None:
    """Vira um bit no MEIO do fluxo deflate.

    Diferente de truncar: truncar deixa o membro sem marcador de fim e levanta
    `EOFError`/`BadGzipFile`, que o `try` antigo pegava. Danificar um bloco
    levanta `zlib.error`, que ele nao pegava - e e o que um SIGKILL no meio de
    uma escrita produz.
    """
    b = bytearray(caminho.read_bytes())
    b[len(b) // 2] ^= 0xFF
    caminho.write_bytes(bytes(b))


def test_manifesto_de_arquivo_com_bloco_danificado_NAO_levanta(tmp_path):
    """A regressao exata da producao: isto levantava `zlib.error`."""
    caminho = escrever(tmp_path, [linha_amostra(T0 + i) for i in range(200)])[0]
    _com_bloco_danificado(caminho)

    m = arquivo.manifesto(caminho)
    assert m["truncado"] is True
    assert len(m["sha256"]) == 64
    assert m["bytes_descomprimidos"] >= 0


def test_integridade_de_arquivo_com_bloco_danificado_NAO_levanta(tmp_path):
    """A mesma conferencia estava escrita duas vezes, com o mesmo buraco."""
    caminho = escrever(tmp_path, [linha_amostra(T0 + i) for i in range(200)])[0]
    _com_bloco_danificado(caminho)
    assert arquivo.integridade(caminho)["truncado"] is True


def test_zlib_error_nao_e_OSError():
    """A premissa do defeito, afirmada em teste em vez de em comentario.

    Se um dia `zlib.error` passar a herdar de `OSError`, este teste quebra e
    alguem decide - em vez de a guarda ficar redundante em silencio.
    """
    import zlib

    assert not issubclass(zlib.error, OSError)
    assert issubclass(gzip.BadGzipFile, OSError)


def test_um_arquivo_ruim_NAO_impede_o_selo_dos_outros(tmp_path):
    """A coleta nao pode ser refem do selo de um dia que ja passou."""
    ontem = arquivo.dia_utc((T0 - 86_400_000) * 1_000_000)
    anteontem = arquivo.dia_utc((T0 - 2 * 86_400_000) * 1_000_000)
    hoje = arquivo.dia_utc(T0 * 1_000_000)

    ruim = escrever(tmp_path, [linha_amostra(T0 + i) for i in range(200)],
                    dia=anteontem)[0]
    _com_bloco_danificado(ruim)
    escrever(tmp_path, [linha_amostra(T0)], dia=ontem)

    selados = arquivo.selar_dias_fechados(tmp_path, "bookticker-btcusdt", hoje)
    assert [p.name for p in selados] == [
        f"bookticker-btcusdt-{anteontem}.jsonl.gz",
        f"bookticker-btcusdt-{ontem}.jsonl.gz",
    ], "os dois selam: o danificado sela DECLARANDO o dano"


def test_selo_que_falha_de_verdade_nao_derruba_a_varredura(tmp_path, monkeypatch):
    """E se `escrever_manifesto` levantar por outro motivo (disco cheio)?

    O dia seguinte continua sendo selado, e nao ha selo silencioso: sem
    manifesto, `conferir_manifesto` devolve `None` - "nao ha manifesto".
    """
    ontem = arquivo.dia_utc((T0 - 86_400_000) * 1_000_000)
    anteontem = arquivo.dia_utc((T0 - 2 * 86_400_000) * 1_000_000)
    hoje = arquivo.dia_utc(T0 * 1_000_000)
    primeiro = escrever(tmp_path, [linha_amostra(T0)], dia=anteontem)[0]
    escrever(tmp_path, [linha_amostra(T0)], dia=ontem)

    real = arquivo.escrever_manifesto

    def falha_no_primeiro(caminho: Path):
        if caminho.name == primeiro.name:
            raise OSError("No space left on device")
        return real(caminho)

    monkeypatch.setattr(arquivo, "escrever_manifesto", falha_no_primeiro)
    selados = arquivo.selar_dias_fechados(tmp_path, "bookticker-btcusdt", hoje)

    assert [p.name for p in selados] == [f"bookticker-btcusdt-{ontem}.jsonl.gz"]
    assert arquivo.conferir_manifesto(primeiro) is None


def test_o_leitor_e_a_conferencia_nao_discordam(tmp_path):
    """Uma travessia so: `ler` e `varrer` usam o mesmo `_descomprimir`.

    Antes eram duas ideias de "acabou" - `zlib.decompressobj` no leitor e
    `gzip.open` na conferencia -, e elas podiam discordar sobre o mesmo
    arquivo. Aqui o arquivo danificado tem de dar linhas legiveis E `truncado`
    ao mesmo tempo, sem contradicao.
    """
    caminho = escrever(tmp_path, [linha_amostra(T0 + i) for i in range(200)])[0]
    intacto = arquivo.varrer(caminho)
    assert intacto.limpo is True
    assert intacto.bytes_descomprimidos > 0

    _com_bloco_danificado(caminho)
    danificado = arquivo.varrer(caminho)
    assert danificado.limpo is False
    assert danificado.bytes_descomprimidos <= intacto.bytes_descomprimidos
    # O leitor entrega exatamente o que a travessia disse que da para ler.
    assert sum(1 for _ in arquivo.ler(caminho)) == arquivo.integridade(caminho)["linhas"]


def test_varrer_de_arquivo_inexistente_e_fato_e_nao_excecao(tmp_path):
    p = arquivo.varrer(tmp_path / "nao-existe.jsonl.gz")
    assert p.limpo is False
    assert p.bytes_descomprimidos == 0
