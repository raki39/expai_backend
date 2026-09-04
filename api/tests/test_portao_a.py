"""Incremento 13 — Portão A: o produto da fase (§14.4).

> "Reprovar no Portão A **não é resultado ruim; é o resultado mais informativo
> possível a esse custo**, porque significa que o mecanismo central do projeto
> ainda não existe." — §14.4

Este arquivo começa pelo bloqueio que o incremento 12 declarou e que o 13
obriga a encarar: **o controle do acaso não estava ligado ao run que ele
casa**. Sem essa ligação o critério 3 do Portão B — "acima do p95 de B1" — não
é computável, e a métrica que isola escolha de momento era inavaliável pelo
validador.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.config.schema import ExperimentConfig


# ===========================================================================
# A ligação do controle com o run que ele casa (migração 14)
# ===========================================================================


def _abrir(conn: sqlite3.Connection, **kwargs) -> int:
    from app.ledger import livro

    return livro.abrir_run(
        conn,
        config_version_id=1,
        seed_capital_usd_cents=ExperimentConfig().seed_capital_usd_cents,
        **kwargs,
    )[0]


def test_um_run_so_pode_ter_um_controle_ligado(conn: sqlite3.Connection) -> None:
    """Dois controles reivindicando o mesmo alvo não é ambiguidade: é palpite.

    Sem o índice único, `b1_do_run` faria `SELECT id FROM run WHERE
    casa_run_id = ?` e escolheria uma das linhas por ordem de id — uma escolha
    arbitrária com cara de consulta, que é exatamente a forma do defeito que a
    ligação existe para fechar.
    """
    alvo = _abrir(conn)
    _abrir(conn, agent_id="baseline-B1-agente", casa_run_id=alvo)
    with pytest.raises(sqlite3.IntegrityError):
        _abrir(conn, agent_id="baseline-B1-agente", casa_run_id=alvo)


def test_a_ligacao_e_imutavel(conn: sqlite3.Connection) -> None:
    """Trocar o alvo depois de medir é a régua trocada depois do resultado.

    E a coluna está exposta a um UPDATE que já existe e roda em todo run:
    `encerrar_run` faz `UPDATE run SET state = ...`. Sem o gatilho, bastaria
    acrescentar uma coluna àquele UPDATE.
    """
    alvo, outro = _abrir(conn), _abrir(conn)
    controle = _abrir(conn, agent_id="baseline-B1-agente", casa_run_id=alvo)
    with pytest.raises(sqlite3.IntegrityError, match="imutavel"):
        conn.execute(
            "UPDATE run SET casa_run_id = ? WHERE id = ?", (outro, controle)
        )
    # E o UPDATE que o sistema faz de verdade continua passando.
    from app.ledger import livro

    livro.encerrar_run(conn, controle, "concluido")


def test_controle_de_controle_e_recusado(conn: sqlite3.Connection) -> None:
    """Comparar um sorteio com outro sorteio não mede escolha de momento.

    Alcançável de fato: basta passar o run de um B1 como alvo do casamento.
    """
    alvo = _abrir(conn)
    controle = _abrir(conn, agent_id="baseline-B1-agente", casa_run_id=alvo)
    with pytest.raises(sqlite3.IntegrityError, match="controle de controle"):
        _abrir(conn, agent_id="baseline-B1-agente", casa_run_id=controle)


def test_run_sem_ligacao_devolve_none_e_nao_o_controle_de_outro(
    conn: sqlite3.Connection,
) -> None:
    """`None` é a resposta de todo run anterior à migração 14.

    A alternativa que existia — "o último B1 casado gravado, globalmente" —
    devolvia um controle plausível de OUTRO experimento. Em produção isso saiu
    na tela como 37 idas e voltas ao lado de um controle de 70.
    """
    from app.maos_rapidas import baselines

    sem_ligacao = _abrir(conn)
    alvo = _abrir(conn)
    _abrir(conn, agent_id="baseline-B1-agente", casa_run_id=alvo)

    assert baselines.b1_do_run(conn, sem_ligacao) is None


def test_a_guarda_nao_e_vazia(conn: sqlite3.Connection) -> None:
    """A coluna existe, e o teste acima poderia passar por ela não existir.

    Um `SELECT ... WHERE casa_run_id = ?` sobre uma coluna ausente levantaria
    `OperationalError`, e não `None` — mas um teste que só afirma `None`
    passaria igual se a função inteira fosse `return None`.
    """
    colunas = {l["name"] for l in conn.execute("PRAGMA table_info(run)")}
    assert "casa_run_id" in colunas


# ===========================================================================
# A1a — os seis controles negativos determinísticos (§14.4, R45)
# ===========================================================================


@pytest.fixture
def cenario(conn: sqlite3.Connection):
    """Dataset dividido, com os baselines rodados.

    A mesma ordem do painel — Dataset, Baselines, e só depois o braço —, e a
    mesma exigência de B4: sem B3 sob esta `config_version` a métrica dos
    controles estatísticos não tem contra o que ser medida.
    """
    from app.maos_rapidas import baselines
    from tests.test_maos_rapidas import criar_dataset, precos_passeio

    from app.b4 import braco as b4_braco

    dataset_id = criar_dataset(conn, precos_passeio(3_000))
    cfg = ExperimentConfig()
    baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        semente=cfg.default_seed,
    )
    # B4 antes, porque o controle de DUPLICAÇÃO precisa de uma hipótese real
    # para duplicar. Duplicar a si mesmo mediria outra coisa — e é o caso que
    # o próprio módulo recusa, dizendo por quê.
    b4_braco.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1
    )
    return dataset_id, cfg


@pytest.fixture
def a1a(conn: sqlite3.Connection, cenario):
    from app.a1a import braco

    dataset_id, cfg = cenario
    return braco.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1
    )


def test_as_seis_familias_sao_as_do_documento() -> None:
    """Lista fechada e citada, e não uma escolha nossa.

    Acrescentar uma sétima seria inventar critério; deixar uma de fora seria
    não testar uma família de defeito que §14.4 manda testar.
    """
    from app.a1a import catalogo

    assert catalogo.QUANTAS == 6
    esperadas = {
        "acesso explícito ao futuro",
        "duplicação disfarçada de hipótese",
        "operação que só lucra quando custos são ignorados",
        "violação conhecida do embargo",
        "preço impossível no nível de fidelidade declarado",
        "adulteração proposital do ledger",
    }
    assert {f.familia_de_defeito for f in catalogo.FAMILIAS} == esperadas
    # Chaves únicas: duas famílias com a mesma chave colapsariam no relatório
    # sem que a contagem mudasse.
    assert len(catalogo.POR_CHAVE) == 6


def test_nenhum_controle_deterministico_e_promovido(a1a) -> None:
    """§14.4: tolerância zero. **Este é o portão.**

    E ele é sobre PROMOÇÃO — o estado da hipótese na máquina de §8.1 —, e não
    sobre o veredito em texto: promover é mover a hipótese adiante, e é a
    transição que fica gravada.
    """
    promovidos = [c.chave for c in a1a.controles if c.promovido]
    assert promovidos == [], (
        "controle determinístico promovido: existe um defeito no pipeline"
        f" ({promovidos})"
    )


def test_os_seis_passam_pelo_mesmo_caminho_das_reais(
    conn: sqlite3.Connection, a1a
) -> None:
    """Injetado *pelo mesmo caminho das reais* é a exigência inteira.

    Um controle num lote separado não enfrentaria a multiplicidade do lote
    real — e o defeito que só se manifesta sob ela é justamente o que a
    tolerância zero existe para pegar.
    """
    from app.hipotese import registro as hipotese_registro
    from app.validador import estados

    assert len(a1a.controles) == 6
    for c in a1a.controles:
        # Run próprio, hipótese registrada e ADMITIDA na máquina de estados.
        assert c.run_id > 0
        assert c.hypothesis_id is not None
        assert estados.atual(conn, c.hypothesis_id) is not None
        linha = hipotese_registro.por_id(conn, c.hypothesis_id)
        assert linha["agente_origem"] == hipotese_registro.AGENTE_ORIGEM_A1A
        # E o enunciado declara a procedência em maiúsculas, como o de B4:
        # duas afirmações indistinguíveis no registro — uma pensada e uma
        # construída para revelar defeito — arruinariam a leitura do lote.
        assert "CONTROLE NEGATIVO DETERMINISTICO" in linha["enunciado"]


def test_as_injecoes_estruturais_sao_todas_barradas(a1a) -> None:
    """`barrado: true` é prova POSITIVA de que a guarda existe e disparou.

    Sem esta asserção, "nenhum promovido" poderia estar passando porque nada
    chegou a acontecer — que é a forma de teste vazio deste projeto.
    """
    from app.a1a import catalogo

    estruturais = [c for c in a1a.controles if c.tipo == catalogo.ESTRUTURAL]
    assert len(estruturais) == 4
    for c in estruturais:
        assert c.tentativas, f"{c.chave} nao injetou nada"
        for t in c.tentativas:
            assert t["barrada"], f"{c.chave}: {t['o_que']} NAO foi barrada"
            assert t["mecanismo"], f"{c.chave}: barrada sem dizer por quem"


def test_o_controle_do_futuro_barra_as_duas_portas(a1a) -> None:
    """Ler o que é do validador, e executar na barra em que se decidiu.

    São dois vazamentos diferentes e as guardas são de camadas diferentes: uma
    é fronteira de Python, a outra é CHECK do banco. Um controle que testasse
    só uma delas deixaria a outra sem cobertura sob um nome que sugere as duas.
    """
    c = next(x for x in a1a.controles if x.chave == "acesso_ao_futuro")
    mecanismos = " | ".join(t["mecanismo"] or "" for t in c.tentativas)
    assert "FinalidadeProibida" in mecanismos
    assert "IntegrityError" in mecanismos


def test_o_controle_do_embargo_barra_purga_zero(a1a) -> None:
    """A porta que só o controle revelou: `purga_barras >= 0` aceitava zero.

    Com purga e embargo zerados, `conferir_sem_vazamento` comparava
    `removidas (0) < 0` e não tinha o que acusar — a conferência parecia
    satisfeita por não ter o que comparar.
    """
    c = next(x for x in a1a.controles if x.chave == "violacao_do_embargo")
    zero = next(t for t in c.tentativas if "ZERO" in t["o_que"])
    assert zero["barrada"]
    assert "purga zero" in (zero["mecanismo"] or "")


def test_o_controle_de_preco_barra_o_preenchimento_generoso(a1a) -> None:
    """Fidelidade 1 não pode afirmar preenchimento melhor que o limite adverso.

    É o defeito mais silencioso da lista: melhora o resultado sem que nenhuma
    linha diga que houve otimismo.
    """
    c = next(x for x in a1a.controles if x.chave == "preco_impossivel")
    assert len(c.tentativas) == 2
    assert all(t["barrada"] for t in c.tentativas)


def test_o_controle_do_ledger_barra_alterar_apagar_e_desequilibrar(a1a) -> None:
    """Três portas, e a terceira é a que a regra 6 chama de partidas dobradas."""
    c = next(x for x in a1a.controles if x.chave == "ledger_adulterado")
    assert len(c.tentativas) == 3
    assert all(t["barrada"] for t in c.tentativas)


def test_o_controle_de_custos_mede_bruto_contra_liquido(a1a) -> None:
    """O ledger é a autoridade sobre dinheiro, e ele é líquido por construção.

    A diferença entre bruto e líquido sai da DECOMPOSIÇÃO do próprio ledger —
    taxa, spread, slippage e penalidade são contas próprias desde o incremento
    3 —, e não de uma segunda simulação que poderia divergir.
    """
    c = next(x for x in a1a.controles if x.chave == "lucro_so_sem_custos")
    eco = c.observado["economia"]
    assert eco["bruto_cents"] == eco["liquido_cents"] + eco["custo_de_execucao_cents"]
    assert eco["custo_de_execucao_cents"] > 0, "giro alto sem custo nenhum"
    # E a métrica sem custo foi recusada pelo enum fechado.
    assert c.tentativas[0]["barrada"]


def test_a_duplicata_disfarcada_ocupa_lugar_na_familia(
    conn: sqlite3.Connection, a1a
) -> None:
    """O que protege não é o hash: é a multiplicidade.

    O disfarce DERROTA o `content_hash`, porque `enunciado` entra nele — então
    a duplicata é cobrada como hipótese nova, 1 crédito em vez dos 3 de
    §8.6.1. A consequência é de PREÇO, e não de multiplicidade: a linha ocupa
    lugar na família de 48 e entra no contador global do DSR do mesmo jeito, e
    é a multiplicidade que BY corrige.

    Registrado com o número ao lado porque mudar o hash agora mudaria o custo
    das 16 hipóteses de B4 que já rodaram em produção — o denominador da
    comparação de §14.3 — depois de ver o resultado delas.
    """
    from app.validador import contador

    c = next(x for x in a1a.controles if x.chave == "duplicacao_disfarcada")
    dup = c.observado["duplicata"]
    assert dup["content_hash_original"] != dup["content_hash_da_duplicata"]
    assert dup["reconhecida_como_reteste"] is False
    # A linha existe no contador global, que §8.6 diz que nunca é zerado.
    assert contador.total(conn) >= 6


# ===========================================================================
# A1b — as nulas estocásticas em execuções repetidas (§14.4, R46, D29)
# ===========================================================================


def _base_sintetica(n: int = 3_000) -> list[int]:
    """Retornos com cauda gorda, na escala de uma barra de 15 min.

    Mistura de normais: 80% do tempo desvio 30 bps, 20% desvio 90. Não é
    modelo de nada — é uma série com curtose acima de 3, que é o que faz o
    DSR ter o que descontar.
    """
    import random as _r

    rng = _r.Random(7)
    return [
        round(rng.gauss(0, 30) + (rng.gauss(0, 90) if rng.random() < 0.2 else 0))
        for _ in range(n)
    ]


def test_as_duas_magnitudes_de_sinal_sao_derivadas_e_diferentes() -> None:
    """O achado que só aparece com as duas ao lado.

    O planejamento de amostra de §8.3 é calibrado em `t = 2`; o limiar de BY na
    primeira posição, com m = 48, exige `t = 3,31`. São réguas diferentes na
    mesma decisão — e uma hipótese que alcance exatamente o `n_minimo` que ela
    declarou tem p-valor ~0,023, quase cinquenta vezes o limiar de 467 ppm.

    Implantar só o piso mediria poder zero e pareceria surdez; implantar só o
    detectável esconderia que o piso não passa.
    """
    from app.a1b import calibre

    cfg = ExperimentConfig()
    m = calibre.magnitudes(
        config=cfg, duracao_barra_ms=900_000, n_barras=21_024
    )
    assert m.piso_milesimos == 2_583
    assert m.detectavel_milesimos == 4_275
    assert m.limiar_by_ppm == 467
    assert m.detectavel_milesimos > m.piso_milesimos

    # E no horizonte que uma hipótese REAL observa — o run 30 esteve com
    # posição aberta em 11.163 barras — o Sharpe detectável passa do teto que
    # o schema aceita declarar (5,00). Ou seja: naquela amostra, nenhuma
    # hipótese declarável seria promovível por BY.
    curto = calibre.magnitudes(
        config=cfg, duracao_barra_ms=900_000, n_barras=11_163
    )
    from app.hipotese.schema import SHARPE_MAX_MILESIMOS

    assert curto.detectavel_milesimos > SHARPE_MAX_MILESIMOS


def test_uma_execucao_e_reproduzivel_pelo_indice() -> None:
    """Mesma semente, mesmo desenho, mesmo índice: mesma linha.

    É o que permite gravar as 400 em pedaços sem que o conjunto deixe de ser
    um experimento só (R12).
    """
    from app.a1b import calibre

    cfg = ExperimentConfig()
    base = _base_sintetica()
    mags = calibre.magnitudes(
        config=cfg, duracao_barra_ms=900_000, n_barras=1_500
    )
    comum = dict(
        desenho=calibre.COM_SINAL, base_bps=base, config=cfg,
        duracao_barra_ms=900_000, n_barras=1_500, tentativas_globais=48,
        semente=42, mags=mags,
    )
    a = calibre.uma(indice=3, **comum)
    b = calibre.uma(indice=3, **comum)
    c = calibre.uma(indice=4, **comum)
    assert a.como_dict() == b.como_dict()
    # E índices diferentes são execuções diferentes: se não fossem, as 200
    # seriam uma repetida 200 vezes e o intervalo descreveria nada.
    assert (a.r_lote, a.v_lote) != (c.r_lote, c.v_lote) or a.indice != c.indice


def test_o_desenho_2_conta_v_como_subconjunto_de_r() -> None:
    """`V / max(R,1)` só faz sentido com V ⊆ R, e o banco impõe isso.

    Uma linha com V > R daria FDR acima de 1, o que não é um número alto: é um
    número impossível, e impossível é o que precisa ser recusado na escrita.
    """
    from app.a1b import calibre

    cfg = ExperimentConfig()
    base = _base_sintetica()
    mags = calibre.magnitudes(
        config=cfg, duracao_barra_ms=900_000, n_barras=1_500
    )
    for i in range(4):
        e = calibre.uma(
            indice=i, desenho=calibre.COM_SINAL, base_bps=base, config=cfg,
            duracao_barra_ms=900_000, n_barras=1_500, tentativas_globais=48,
            semente=42, mags=mags,
        )
        assert e.v_lote <= e.r_lote
        assert e.r_com_portao <= e.r_lote
        assert e.promovidos_piso <= e.sinais_piso
        assert e.promovidos_detectavel <= e.sinais_detectavel


def test_o_criterio_do_desenho_1_e_o_limite_superior_e_nao_conter_o_alvo(
) -> None:
    """D37 (ADR 0024): a redação da D29 era aritmeticamente inalcançável.

    Sob a nula global, BY rejeita com probabilidade no máximo `alfa / H(m)` —
    2,24% com m = 48. Um IC de 200 execuções em torno disso jamais contém 10%,
    então "o IC contém o alvo" reprovaria um BY **correto** por ele ser
    conservador. §14.4 diz "compatível com o nível", e sob BY compatível só
    pode significar "não excede".

    Este teste fixa as duas coisas: que o critério mudou, e que a leitura
    antiga continua **calculada e visível** — apagá-la esconderia que houve
    correção.
    """
    from app.a1b import calibre

    cfg = ExperimentConfig()
    # 200 execuções sem nenhuma promoção: o caso que a D29 reprovaria.
    zeradas = [
        calibre.Uma(
            desenho=calibre.NULA_GLOBAL, indice=i, r_lote=0, v_lote=0,
            r_com_portao=0, v_com_portao=0, sinais_piso=0, promovidos_piso=0,
            sinais_detectavel=0, promovidos_detectavel=0,
        )
        for i in range(cfg.a1b_execucoes)
    ]
    bloco = calibre.agregar(
        zeradas, desenho=calibre.NULA_GLOBAL, config=cfg
    )["promocao_do_lote"]
    assert bloco["ic_contem_o_alvo"] is False
    assert bloco["limite_superior_ate_o_alvo"] is True
    assert "D37" in bloco["por_que_o_criterio_e_o_limite_superior"]
    # E o intervalo de 0/200 é o que a aritmética manda: [0 ; ~1,9%].
    assert bloco["intervalo"]["alto_ppm"] < 20_000


def test_agregar_sem_execucao_nao_inventa_proporcao() -> None:
    """Zero execuções não é proporção zero.

    Um intervalo sobre nada afirmaria que se mediu — e este relatório é
    justamente o que decide se a fase passa.
    """
    from app.a1b import calibre

    saida = calibre.agregar(
        [], desenho=calibre.NULA_GLOBAL, config=ExperimentConfig()
    )
    assert saida["execucoes"] == 0
    assert saida["completo"] is False
    assert "por_que_sem_numero" in saida
    assert "promocao_do_lote" not in saida


def test_wilson_nao_degenera_em_zero() -> None:
    """`0/200` não é certeza absoluta, e a normal diria que é.

    O critério do desenho 1 é sobre onde o intervalo cai; um intervalo que
    colapsa a um ponto responde qualquer pergunta com "sim".
    """
    from app.estatistica import intervalo

    ic = intervalo.wilson(sucessos=0, n=200)
    assert ic.ponto_ppm == 0
    assert ic.baixo_ppm == 0
    assert 0 < ic.alto_ppm < 30_000  # ~1,9%
    # E o outro extremo, pelo mesmo motivo.
    cheio = intervalo.wilson(sucessos=200, n=200)
    assert cheio.alto_ppm == 1_000_000
    assert cheio.baixo_ppm < 1_000_000


def test_confianca_fora_de_95_e_recusada() -> None:
    """Aproximar `z` aqui produziria um intervalo que PARECE ser o pedido.

    A D29 fixou 95%; outra confiança é decisão nova, e não um parâmetro que já
    esteja implementado.
    """
    from app.estatistica import intervalo

    with pytest.raises(ValueError, match="95"):
        intervalo.wilson(sucessos=1, n=10, confianca_bps=9_900)


def test_o_calibre_usa_a_MESMA_decisao_do_lote_real() -> None:
    """Uma cópia do procedimento mediria o calibre de outro procedimento.

    A guarda é de importação: `a1b/calibre.py` chama `lote.decidir`, e não
    `fdr.aplicar` direto. Se alguém reescrevesse a decisão aqui, o calibre
    passaria a descrever um caminho que não promove nada em produção.
    """
    import pathlib

    fonte = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app" / "a1b" / "calibre.py"
    ).read_text(encoding="utf-8")
    assert "lote_mod.decidir(" in fonte
    assert "fdr_mod.aplicar(" not in fonte


# ===========================================================================
# O relatório do Portão A — o produto do incremento
# ===========================================================================


def _portao(conn: sqlite3.Connection, dataset_id: int, cfg: ExperimentConfig):
    from app.relatorio import portao_a

    return portao_a.montar(
        conn, config_version_id=1, config=cfg, dataset_id=dataset_id
    )


def test_cada_condicao_do_portao_sai_de_uma_consulta(
    conn: sqlite3.Connection, cenario, a1a
) -> None:
    """Nenhuma condição é digitada, e todas são booleano ou `None`.

    Um relatório de portão escrito à mão diria "o protocolo funciona" com a
    mesma confiança tivesse ele funcionado ou não.
    """
    dataset_id, cfg = cenario
    r = _portao(conn, dataset_id, cfg)
    assert r["portao"] == "A"
    assert r["pergunta"] == "o protocolo rejeita defeito?"
    for nome, valor in r["condicoes"].items():
        assert valor is None or isinstance(valor, bool), (nome, valor)
    # Onze condições, cobrindo A1a, A1b (dois desenhos e a data do IC), A2,
    # A3 e A4.
    assert len(r["condicoes"]) == 11


def test_o_portao_tem_TRES_resultados_e_pendente_nao_e_passa(
    conn: sqlite3.Connection, cenario, a1a
) -> None:
    """`None` continua diferente de `False` — e deixa de ser igual a aprovado.

    O relatório da 0A tratava `None` como neutro, e ali estava certo: com o
    teto em zero não há custo de decisão a registrar (D23). Aqui não pode: o
    Portão A é "obrigatório, eliminatório", e um critério que ninguém mediu
    não é um critério satisfeito.
    """
    dataset_id, cfg = cenario
    r = _portao(conn, dataset_id, cfg)
    # A1b não rodou neste cenário: os dois desenhos estão pendentes.
    assert "a1b_nula_global_no_alvo" in r["pendentes"]
    assert r["passa"] is False
    assert r["pendente"] is True
    assert r["reprova"] is False
    # E os três são mutuamente exclusivos onde precisam ser.
    assert not (r["passa"] and r["pendente"])
    assert not (r["passa"] and r["reprova"])


@pytest.fixture
def cenario_testavel(conn: sqlite3.Connection):
    """Janela em que os controles estatísticos CHEGAM a ser avaliados.

    Com 3.000 barras o in-sample tem 900, o Sharpe mínimo testável passa do
    teto do schema, e toda hipótese nasce arquivada como não testável — então
    nenhuma delas pode sequer ser promovida, e um teste de "promoção reprova o
    portão" passaria por não conseguir plantar o defeito.

    20.000 barras dão in-sample de 6.000, e aí o caminho inteiro roda.
    """
    from app.maos_rapidas import baselines
    from tests.test_maos_rapidas import criar_dataset, precos_passeio

    cfg = ExperimentConfig()
    dataset_id = criar_dataset(conn, precos_passeio(20_000))
    baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        semente=cfg.default_seed,
    )
    return dataset_id, cfg


@pytest.fixture
def a1a_testavel(conn: sqlite3.Connection, cenario_testavel):
    from app.a1a import braco

    dataset_id, cfg = cenario_testavel
    return braco.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1
    )


def test_os_controles_estatisticos_sao_avaliados_e_nao_promovidos(
    conn: sqlite3.Connection, a1a_testavel
) -> None:
    """Numa janela testável eles chegam ao validador, pagam crédito e caem.

    Sem esta asserção, "nenhum promovido" nos controles estatísticos poderia
    estar passando por eles nunca terem sido julgados — que é a forma de
    aprovação por vacuidade que o Portão A não pode ter.
    """
    from app.a1a import catalogo

    estatisticos = [
        c for c in a1a_testavel.controles if c.tipo == catalogo.ESTATISTICO
    ]
    assert len(estatisticos) == 2
    avaliados = [c for c in estatisticos if c.veredito is not None]
    assert avaliados, "nenhum controle estatistico chegou a receber veredito"
    for c in avaliados:
        assert c.creditos_cobrados and c.creditos_cobrados > 0
        assert c.veredito != "sustentada"
        assert c.promovido is False


def test_um_controle_promovido_REPROVA_o_portao_e_diz_qual(
    conn: sqlite3.Connection, cenario_testavel, a1a_testavel
) -> None:
    """**Um portão que nunca reprova não é portão.**

    Este é o critério 2 do plano, e ele é a metade que falta: `passa` sozinho
    poderia estar sempre verdadeiro por não olhar nada. Aqui um defeito é
    plantado de propósito — um controle determinístico é movido para
    `candidata` — e o portão precisa reprovar E dizer qual controle passou.

    A promoção é escrita pelo caminho normal do validador, e não por `UPDATE`
    solto: se fosse por fora, o teste provaria que dá para burlar a máquina de
    estados, e não que o portão vê a promoção.
    """
    from app.validador import estados

    dataset_id, cfg = cenario_testavel
    antes = _portao(conn, dataset_id, cfg)
    assert antes["reprova"] is False

    alvo = next(
        c for c in a1a_testavel.controles
        if estados.atual(conn, c.hypothesis_id).estado == estados.ENTRADA
    )
    estados.transitar(
        conn,
        alvo.hypothesis_id,
        para="candidata",
        evidencia={"etapa": "defeito_plantado", "creditos": 0},
    )

    depois = _portao(conn, dataset_id, cfg)
    assert depois["reprova"] is True
    assert depois["passa"] is False
    assert "a1a_nenhum_controle_promovido" in depois["reprovando"]
    # E o relatório diz QUAL: o nome do controle é o ponteiro para onde
    # procurar o defeito, e uma contagem não aponta para lugar nenhum.
    assert alvo.chave in depois["a1a"]["promovidos"] or (
        alvo.hypothesis_id in depois["a1a"]["promovidos"]
    )


def test_a2_recusa_afirmar_proporcionalidade_com_um_ponto_so(
    conn: sqlite3.Connection, cenario
) -> None:
    """Um ponto não tem inclinação.

    Na 0A o B1 negativo era sanidade observada; §14.4 o torna portão. E o
    portão precisa distinguir "medi e é proporcional" de "só tenho um giro" —
    devolver `True` com uma medida só afirmaria a segunda medida.
    """
    dataset_id, cfg = cenario
    r = _portao(conn, dataset_id, cfg)
    a2 = r["a2"]
    assert a2["negativo"] is True, "operar ao acaso deu lucro no simulador"
    assert a2["proporcional_ao_giro"] is None
    assert "um ponto nao tem inclinacao" in a2["por_que_sem_proporcional"]


def test_a2_mede_proporcionalidade_com_dois_giros(
    conn: sqlite3.Connection, cenario
) -> None:
    """Com dois giros a inclinação existe, e mais giro perde mais.

    É a forma executável de "B1 produz resultado negativo, **proporcional ao
    número de operações**" (§14.4, §8.4.1.3).
    """
    from app.ledger import livro
    from app.maos_rapidas import baselines, executor

    dataset_id, cfg = cenario
    barras = executor.carregar_janela(conn, dataset_id)
    alvo = livro.abrir_run(
        conn, config_version_id=1,
        seed_capital_usd_cents=cfg.seed_capital_usd_cents,
    )[0]
    baselines.b1_casado_com(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        operacoes_alvo=8, fracao_bps=10_000, semente=42, barras=barras,
        casa_run_id=alvo,
    )

    a2 = _portao(conn, dataset_id, cfg)["a2"]
    assert a2["proporcional_ao_giro"] is True
    giros = sorted(c["operacoes_alvo"] for c in a2["corridas"])
    assert len(giros) >= 2 and giros[0] != giros[-1]


def test_a3_pergunta_ao_BANCO_e_nao_a_suite(
    conn: sqlite3.Connection, cenario, a1a
) -> None:
    """Um teste verde numa máquina não diz nada sobre as linhas que existem lá.

    §14.4 exige que A3 seja "verificado por teste automatizado, não por
    inspeção". A suíte tem os testes; este bloco faz as mesmas perguntas ao
    banco de produção, sobre o que de fato ficou gravado.
    """
    dataset_id, cfg = cenario
    a3 = _portao(conn, dataset_id, cfg)["a3"]
    assert a3["execucoes_na_barra_da_decisao"] == 0
    assert a3["execucoes_em_conjunto_do_validador"] == 0
    assert a3["acessos_ao_holdout_por_outro"] == 0
    assert a3["sem_vazamento"] in (True, None)


def test_a4_confere_o_registro_nos_dois_sentidos(
    conn: sqlite3.Connection, cenario, a1a
) -> None:
    """"Nenhuma tentativa testada some do registro" é a condição mais fácil de
    esquecer, e a conferência é nos dois sentidos.

    Só o primeiro deixaria o registro acumular linhas que nenhuma tentativa
    produziu — o erro simétrico, e igualmente invisível.
    """
    dataset_id, cfg = cenario
    a4 = _portao(conn, dataset_id, cfg)["a4"]
    assert a4["testadas_sem_estado"] == []
    assert a4["hipoteses_na_tabela"] == a4["contador_global"]
    assert a4["conferencias"]["nenhuma_tentativa_some"] is True
    assert a4["conferencias"]["ledger_reconcilia"] is True


def test_o_ic_definido_antes_do_teste_e_conferido_no_historico(
    conn: sqlite3.Connection, cenario
) -> None:
    """Critério 6 do incremento 13, e a forma dele é o ponto.

    §14.4 pede "IC definido **antes** do teste", e o plano é explícito:
    *"verificável no histórico da config, não na nossa palavra"*. Então isto é
    uma comparação de datas entre duas tabelas append-only.

    Sem execução gravada a resposta é `None` — não há ordem a conferir entre um
    evento que aconteceu e outro que não aconteceu.
    """
    from app.a1b import braco as a1b_braco

    dataset_id, cfg = cenario
    r = _portao(conn, dataset_id, cfg)
    assert r["ic_antes_do_teste"]["antes"] is None
    assert r["condicoes"]["a1b_ic_definido_antes_do_teste"] is None

    # Uma execução gravada, e a ordem passa a ser conferível.
    a1b_braco.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        quantas=1,
    )
    depois = _portao(conn, dataset_id, cfg)
    bloco = depois["ic_antes_do_teste"]
    assert bloco["config_criada_em"] is not None
    assert bloco["primeira_execucao_em"] is not None
    assert bloco["antes"] is True, (
        "a config que fixou o IC nao e anterior a primeira execucao de A1b:"
        " um IC escolhido depois de ver a proporcao e a regua trocada depois"
        " do resultado"
    )


def test_a1b_grava_em_pedacos_sem_contar_a_mesma_execucao_duas_vezes(
    conn: sqlite3.Connection, cenario
) -> None:
    """Rodar de novo não regrava: o UNIQUE recusa.

    Contar a mesma execução duas vezes é o defeito mais fácil de produzir num
    registro que cresce em pedaços — e ele inflaria ou desinflaria a proporção
    sem que nada acusasse.
    """
    from app.a1b import braco as a1b_braco, registro

    dataset_id, cfg = cenario
    primeiro = a1b_braco.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        quantas=2,
    )
    assert primeiro["gravadas_agora"] == 2
    antes = len(registro.ler(conn, 1))

    # O próximo pedaço pega índices NOVOS, e não repete os dois primeiros.
    segundo = a1b_braco.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        quantas=2,
    )
    assert segundo["gravadas_agora"] == 2
    assert len(registro.ler(conn, 1)) == antes + 2

    # E a mesma execução, pedida de novo pelo índice, é recusada.
    from app.a1b import calibre

    mags = calibre.magnitudes(
        config=cfg, duracao_barra_ms=900_000, n_barras=900
    )
    repetida = calibre.uma(
        indice=0, desenho=calibre.NULA_GLOBAL,
        base_bps=_base_sintetica(), config=cfg, duracao_barra_ms=900_000,
        n_barras=200, tentativas_globais=1, semente=42, mags=mags,
    )
    assert registro.gravar(
        conn, [repetida], config_version_id=1, semente=42,
        lote=cfg.a1b_lote, n_barras=200, tentativas_globais=1,
    ) == 0


def test_o_portao_b_nao_e_avaliado_enquanto_o_a_nao_passa(
    conn: sqlite3.Connection, cenario
) -> None:
    """R49, literal: "Portão B só avaliado se o Portão A passar integralmente".

    Calculá-lo antes seria produzir o número que a fase existe para não
    produzir cedo demais.
    """
    dataset_id, cfg = cenario
    r = _portao(conn, dataset_id, cfg)
    assert r["portao_b"]["avaliado"] is False
    assert "R49" in r["portao_b"]["por_que"]
