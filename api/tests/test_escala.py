"""O domínio de cada métrica, e a cláusula que não consegue ser as duas coisas.

> *"Cada cláusula precisa ter métrica válida, unidade compatível e
> possibilidade real de ser satisfeita e não satisfeita dentro do domínio
> permitido."* — o usuário, 2026-09-09

O caso que produziu este arquivo é real e está em produção: a **hipótese 41**
declarou `patrimonio_final_cents < 950000` sobre uma semente de **100.000**
centavos. Nove vezes e meia a semente — a cláusula dispara em todo run
possível e nunca poderia deixar de disparar, e entrou no veredito com
`disparou: true`, indistinguível de uma refutação de verdade.

Duas coisas deixaram passar, e as duas estão corrigidas aqui:

1. `_falseamento_serve_para_algo` só conferia a métrica **primária**;
2. o prompt dizia *"ele começa com um capital semente"* — a palavra, e não o
   número. **O agente não errou: ele não tinha como acertar.**
"""

from __future__ import annotations

import pytest

from app.hipotese import escala
from app.hipotese.schema import ClausulaFalseamento, PreRegistroBruto

SEMENTE = 100_000
HORIZONTE = 21_024


def _pre(*clausulas, primaria="excesso_sobre_b3_cents"):
    return PreRegistroBruto(
        enunciado="hipotese de teste",
        metrica_primaria=primaria,
        efeito_minimo=5_000,
        sharpe_esperado_milesimos=1_000,
        criterio_parada="fim_da_janela",
        condicoes_falseamento=list(clausulas),
    )


def _obrigatoria():
    """A cláusula que o schema exige sobre a primária."""
    return ClausulaFalseamento.da_semente(
        "excesso_sobre_b3_cents", "menor_que", bps=500, semente_cents=SEMENTE
    )


# ---------------------------------------------------------------------------
# O domínio, derivado
# ---------------------------------------------------------------------------


def test_o_dominio_ESTRUTURAL_nao_depende_da_semente():
    """Nenhum limite imposto pelo simulador é função do capital semente.

    Foi assumir que era que produziu o defeito: a primeira versão publicava
    `[0, 3 × semente]` como *domínio observável*. Não é — se o ativo triplicar,
    o patrimônio passa de 3× sem alavancagem nenhuma. A assinatura de
    `dominio_estrutural` **não recebe a semente**, e é isso que torna o erro
    impossível de repetir.
    """
    import inspect

    assinatura = inspect.signature(escala.dominio_estrutural)
    assert "semente_cents" not in assinatura.parameters, (
        "nenhum limite ESTRUTURAL depende da semente; receber a semente aqui"
        " reabre a porta para confundir politica com impossibilidade"
    )


def test_o_teto_do_patrimonio_NAO_EXISTE_e_isso_e_declarado():
    """`None` significa *não há limite a afirmar*, e não *zero* nem *infinito*.

    Uma posição long pode ultrapassar qualquer múltiplo da semente se o ativo
    valorizar, e ganhos sucessivos compõem. Qualquer teto derivado da janela
    observada seria dado do in-sample entrando numa conferência de
    pré-registro, e inválido para qualquer outra janela.
    """
    piso, teto = escala.dominio_estrutural(
        "patrimonio_final_cents", horizonte_barras=HORIZONTE
    )
    assert piso == 0, "o piso E estrutural: long/flat sem alavancagem"
    assert teto is None, "o teto NAO e estrutural: depende do caminho de precos"


def test_o_excesso_nao_tem_limite_estrutural_de_lado_nenhum():
    """Diferença de dois patrimônios sem teto — e perder do baseline é normal."""
    assert escala.dominio_estrutural(
        "excesso_sobre_b3_cents", horizonte_barras=HORIZONTE
    ) == (None, None)


# ---------------------------------------------------------------------------
# A PROVA do único teto estrutural que existe
# ---------------------------------------------------------------------------


def test_idas_e_voltas_e_no_maximo_CEIL_das_barras_por_dois():
    """`ceil(B / 2)`, e a prova tem duas metades verificadas no executor.

    1. **No máximo uma execução por barra de decisão** — o laço de
       `executor.rodar` é `for i in range(ultima_decidivel + 1)`, a venda por
       stop faz `continue`, e o resto é `if ENTRAR ... elif SAIR`, ramos
       exclusivos;
    2. **compras e vendas alternam estritamente** — `comprar` exige
       `not aberta` e põe `aberta = True`; só `vender` devolve `False`.

    Logo a sequência mais densa é `C V C V C …`, que começa e pode terminar em
    compra: `ceil(B / 2)`.
    """
    for barras, esperado in ((0, 0), (1, 1), (2, 1), (5, 3), (6, 3), (21_024, 10_512)):
        piso, teto = escala.dominio_estrutural(
            "idas_e_voltas", horizonte_barras=barras
        )
        assert (piso, teto) == (0, esperado), barras


def test_CEIL_e_nao_FLOOR_e_a_primeira_versao_errava_o_impar():
    """Em 5 barras cabem 3 compras: `C V C V C`.

    A primeira versão usava `B // 2` = 2 e **recusaria uma cláusula legítima**
    em horizonte ímpar — um domínio apertado demais mente na direção de dar
    trabalho, como as guardas de regex estreito já mentiram três vezes.
    """
    _, teto = escala.dominio_estrutural("idas_e_voltas", horizonte_barras=5)
    assert teto == 3
    assert teto != 5 // 2


def test_as_DUAS_metades_da_prova_estao_no_executor():
    """A prova é sobre código real, então ela quebra se o código mudar.

    Um docstring afirmando a propriedade sobreviveria a qualquer regressão sem
    mudar uma letra — é a forma do `BLOCOS` e do comentário de `braco.py`.
    """
    import inspect

    from app.maos_rapidas import executor

    fonte = inspect.getsource(executor.rodar)
    # (1) uma execucao por barra: o `continue` depois da venda por stop.
    assert "continue" in fonte
    # (2) alternancia: `comprar` sob `not aberta`, e o ramo de saida e `elif`.
    assert "not aberta" in fonte
    assert "elif sinal == Sinal.SAIR and aberta" in fonte
    # E `idas_e_voltas` conta COMPRAS, e nao metade das execucoes.
    assert "side = 'compra'" in inspect.getsource(executor.idas_e_voltas)


# ---------------------------------------------------------------------------
# O CASO DA HIPOTESE 41
# ---------------------------------------------------------------------------


def test_o_caso_REAL_da_hipotese_41_e_FORA_DA_ESCALA_e_nao_impossivel():
    """`patrimonio_final_cents < 950000` sobre semente de 100.000.

    O número exato que está gravado em produção. Recusado — e recusado pela
    **política**, não por impossibilidade: `ForaDaEscalaEconomica`, e não
    `ClausulaNaoInformativa`. Se este teste voltar a esperar *"dispara
    sempre"*, o erro de 2026-09-09 foi refeito.
    """
    bruto = _pre(
        _obrigatoria(),
        ClausulaFalseamento.da_semente(
            "patrimonio_final_cents", "menor_que",
            bps=95_000, semente_cents=SEMENTE,
        ),
    )
    assert bruto.condicoes_falseamento[1].valor == 950_000

    with pytest.raises(escala.ForaDaEscalaEconomica) as erro:
        escala.conferir(
            bruto, semente_cents=SEMENTE, horizonte_barras=HORIZONTE
        )
    texto = str(erro.value)
    assert "faixa economica admissivel" in texto
    assert "POLITICA pre-declarada" in texto
    # E ela NAO pode dizer nenhuma das duas coisas que exigiriam prova.
    assert "dispara SEMPRE" not in texto
    assert "impossivel" not in texto.lower() or "nada e impossivel" in texto


def test_a_conferencia_alcanca_a_clausula_SECUNDARIA():
    """Era só a primária que era conferida, e o defeito estava na secundária.

    Aqui a primária é impecável e a secundária é tautológica: se a conferência
    voltasse a olhar só a primária, este teste passaria em silêncio.
    """
    bruto = _pre(
        _obrigatoria(),  # primaria impecavel
        ClausulaFalseamento(
            metrica="idas_e_voltas", comparador="maior_que", valor=999_999
        ),
    )
    with pytest.raises(escala.ClausulaNaoInformativa) as erro:
        escala.conferir(
            bruto, semente_cents=SEMENTE, horizonte_barras=HORIZONTE
        )
    # ESTA pode dizer "nunca dispara": o teto de `idas_e_voltas` e provado.
    assert "idas_e_voltas" in str(erro.value)
    assert "NUNCA dispara" in str(erro.value)
    assert "ESTRUTURAL" in str(erro.value)


def test_a_clausula_EM_ESCALA_passa():
    """A conferência não é um veto geral: 95% da semente é declarável."""
    bruto = _pre(
        _obrigatoria(),
        ClausulaFalseamento.da_semente(
            "patrimonio_final_cents", "menor_que",
            bps=9_500, semente_cents=SEMENTE,
        ),
        ClausulaFalseamento(
            metrica="idas_e_voltas", comparador="maior_que", valor=5_000
        ),
    )
    assert bruto.condicoes_falseamento[1].valor == 95_000
    escala.conferir(bruto, semente_cents=SEMENTE, horizonte_barras=HORIZONTE)


# ---------------------------------------------------------------------------
# A unidade, conferida sem tocar o banco
# ---------------------------------------------------------------------------


def test_metrica_monetaria_EXIGE_declaracao_relativa():
    """Um número em centavos sozinho não diz se é um décimo ou dez vezes.

    A recusa é no REGISTRO, e não no modelo — ver o teste de
    não-retroatividade logo abaixo.
    """
    bruto = _pre(
        ClausulaFalseamento(
            metrica="excesso_sobre_b3_cents",
            comparador="menor_que",
            valor=5_000,
        )
    )
    with pytest.raises(escala.ClausulaNaoInformativa) as erro:
        escala.conferir(
            bruto, semente_cents=SEMENTE, horizonte_barras=HORIZONTE
        )
    assert "sem referencia de escala" in str(erro.value)


def test_metrica_de_CONTAGEM_recusa_declaracao_relativa():
    """`idas_e_voltas` não tem semente a que se referir: bps ali é erro."""
    bruto = _pre(
        _obrigatoria(),
        ClausulaFalseamento(
            metrica="idas_e_voltas",
            comparador="maior_que",
            valor=80,
            valor_bps_da_semente=800,
        ),
    )
    with pytest.raises(escala.ClausulaNaoInformativa) as erro:
        escala.conferir(
            bruto, semente_cents=SEMENTE, horizonte_barras=HORIZONTE
        )
    assert "CONTAGEM" in str(erro.value)


def test_a_LINHA_JA_GRAVADA_continua_relegivel():
    """A regra nova não alcança o passado, e esta é a regressão que a prova.

    A conferência de unidade morou no `model_validator` por vinte minutos e
    **derrubou três rotas em produção com HTTP 500**: `_pre_registro`
    reconstrói um `PreRegistroBruto` a partir do JSON gravado, e o
    pré-registro é imutável (§8.2) — as cláusulas das 41 hipóteses existentes
    nunca terão `valor_bps_da_semente`. Exigi-lo no modelo tornou todo
    veredito antigo impossível de reler.

    Foi uma regra nova aplicada ao passado, que é o que a migração 29 evitou
    na D48 ao gravar `regua_dimensionamento` linha a linha.
    """
    # A forma EXATA em que a hipótese 41 está gravada: sem `bps` em campo nenhum.
    gravado = {
        "enunciado": "hipotese de 2026-09-04",
        "metrica_primaria": "excesso_sobre_b3_cents",
        "efeito_minimo": 50_000,
        "sharpe_esperado_milesimos": 2_700,
        "criterio_parada": "fim_da_janela",
        "condicoes_falseamento": [
            {
                "metrica": "excesso_sobre_b3_cents",
                "comparador": "menor_que",
                "valor": 50_000,
            },
            {
                "metrica": "idas_e_voltas",
                "comparador": "maior_que",
                "valor": 80,
            },
            {
                "metrica": "patrimonio_final_cents",
                "comparador": "menor_que",
                "valor": 950_000,
            },
        ],
    }
    pre = PreRegistroBruto(**gravado)
    assert [c.como_texto() for c in pre.condicoes_falseamento] == [
        "excesso_sobre_b3_cents < 50000",
        "idas_e_voltas > 80",
        "patrimonio_final_cents < 950000",
    ]
    assert all(
        c.valor_bps_da_semente is None for c in pre.condicoes_falseamento
    )
    # E o diagnóstico ainda nomeia a que não informa, sem impedir a leitura.
    fora = escala.diagnosticar(
        pre.condicoes_falseamento,
        semente_cents=SEMENTE,
        horizonte_barras=HORIZONTE,
    )
    assert [d["metrica"] for d in fora] == ["patrimonio_final_cents"]


def test_a_conversao_de_bps_e_UMA_travessia():
    """`bps x semente / 10.000`, numa função só.

    Duas conversões divergiriam, e a divergência aqui muda um veredito — a
    mesma lição da marcação a mercado, que custou seis ordens de grandeza.
    """
    c = ClausulaFalseamento.da_semente(
        "patrimonio_final_cents", "menor_que", bps=9_500, semente_cents=SEMENTE
    )
    assert c.valor == SEMENTE * 9_500 // 10_000 == 95_000
    assert c.valor_bps_da_semente == 9_500


# ---------------------------------------------------------------------------
# O diagnóstico das linhas JA GRAVADAS, que são imutáveis
# ---------------------------------------------------------------------------


def test_o_diagnostico_publica_sem_corrigir():
    """O pré-registro é imutável (§8.2): o que se pode é dizer que não informa."""
    ruim = ClausulaFalseamento.da_semente(
        "patrimonio_final_cents", "menor_que",
        bps=95_000, semente_cents=SEMENTE,
    )
    boa = _obrigatoria()
    fora = escala.diagnosticar(
        [boa, ruim], semente_cents=SEMENTE, horizonte_barras=HORIZONTE
    )
    assert len(fora) == 1
    d = fora[0]
    assert d["metrica"] == "patrimonio_final_cents"
    assert d["classificacao"] == "fora_da_escala_economica"
    assert d["dominio_estrutural"] == [0, None]
    assert d["faixa_economica_admissivel"] == [0, 300_000]
    assert d["nao_fortalece_veredito"] is True
    # E o campo que impede a leitura errada vai JUNTO.
    assert "NAO afirma que o valor e impossivel" in d["o_que_isso_NAO_afirma"]


def test_o_diagnostico_separa_as_DUAS_classes():
    """A mesma função, dois vereditos diferentes, e cada um diz o que é."""
    estrutural = ClausulaFalseamento(
        metrica="idas_e_voltas", comparador="maior_que", valor=20_000
    )
    politica = ClausulaFalseamento(
        metrica="patrimonio_final_cents",
        comparador="menor_que",
        valor=950_000,
        valor_bps_da_semente=95_000,
    )
    por_classe = {
        d["classificacao"]: d
        for d in escala.diagnosticar(
            [estrutural, politica],
            semente_cents=SEMENTE,
            horizonte_barras=HORIZONTE,
        )
    }
    assert set(por_classe) == {"nao_informativa", "fora_da_escala_economica"}
    # A estrutural PODE afirmar impossibilidade; a de politica nao.
    assert "NUNCA dispara" in por_classe["nao_informativa"]["por_que"]
    assert por_classe["nao_informativa"]["o_que_isso_NAO_afirma"] is None
    assert por_classe["fora_da_escala_economica"]["o_que_isso_NAO_afirma"]


def test_o_dominio_do_run_e_o_da_SEMENTE_DELE():
    """Trocar a semente não pode reclassificar uma cláusula antiga.

    Com semente de 1.000.000 o limiar de 950.000 passa a ser 95% dela, e a
    mesma cláusula vira informativa. É por isso que `promocao` lê a semente da
    config que abriu o RUN, e não a vigente.
    """
    ruim = ClausulaFalseamento(
        metrica="patrimonio_final_cents",
        comparador="menor_que",
        valor=950_000,
        valor_bps_da_semente=95_000,
    )
    assert escala.diagnosticar(
        [ruim], semente_cents=100_000, horizonte_barras=HORIZONTE
    )
    # Com semente de 1.000.000 o limiar passa a ser 95% dela, e a FAIXA o
    # admite. O dominio estrutural nao mudou - ele nunca dependeu da semente.
    assert not escala.diagnosticar(
        [ruim], semente_cents=1_000_000, horizonte_barras=HORIZONTE
    )


# ---------------------------------------------------------------------------
# O CONTRATO do agente entra na identidade reproduzível
# ---------------------------------------------------------------------------


def test_o_hash_do_contrato_esta_no_payload_da_config():
    """Dois deploys com prompts diferentes não podem ter a mesma identidade.

    Era o vão que o incremento 18 fechou para o perfil de calibração e que
    seguia aberto para o prompt: `identidade_executavel` era
    `config_hash + perfil`, e o prompt não aparecia nela.
    """
    from app.cerebro.contrato import hash_do_contrato
    from app.config.schema import ExperimentConfig

    cfg = ExperimentConfig()
    assert cfg.contrato_do_agente_hash == hash_do_contrato()
    # MATERIAL: mudar o prompt muda o caminho de decisao.
    assert "contrato_do_agente_hash" in cfg.payload_material()


def test_mudar_o_texto_do_prompt_muda_o_hash_e_portanto_a_identidade():
    """O hash é derivado do CÓDIGO, então não há número para esquecer.

    Digitar uma versão ao lado do texto é a forma do defeito que este projeto
    conta: alguém muda o texto e o número fica descrevendo o anterior.
    """
    import app.cerebro.prompts as prompts
    from app.cerebro.contrato import hash_do_contrato

    antes = hash_do_contrato()
    original = prompts.SISTEMA
    try:
        prompts.SISTEMA = original + "\numa linha nova no contrato"
        assert hash_do_contrato() != antes
    finally:
        prompts.SISTEMA = original
    assert hash_do_contrato() == antes


def test_o_bloco_de_escala_carrega_OS_QUATRO_itens_exigidos():
    """capital semente, moeda, unidade e limites — em números, não em palavra."""
    from app.cerebro import prompts

    texto = prompts.bloco_de_escala(
        capital_semente_cents=SEMENTE,
        moeda="USD",
        fracao_maxima_bps=10_000,
        horizonte_barras=HORIZONTE,
    )
    assert "100000 centavos" in texto
    assert "1000.00 USD" in texto
    assert "moeda: USD" in texto
    assert "CENTAVOS INTEIROS" in texto
    assert "10000 bps do caixa" in texto
    assert "sem alavancagem" in texto
    # E os dois dominios, para que o limiar nasca dentro deles.
    # O teto ESTRUTURAL de idas e voltas, que e ceil(B / 2).
    assert f"de 0 a {(HORIZONTE + 1) // 2}" in texto
    # E a faixa de patrimonio, dita como FAIXA e nao como teto do simulador.
    assert "300000" in texto
    assert "ADMISSIVEL" in texto
    assert "Nao e um teto do simulador" in texto
    assert "valor_bps_da_semente" in texto


def test_o_limite_de_posicao_e_lido_do_FIELD_e_nao_digitado():
    """Um segundo literal divergiria do que o validador de fato recusa."""
    from app.regra.schema import FRACAO_MAXIMA_BPS, Regra

    campo = Regra.model_fields["position_fraction_bps"]
    le = next(m.le for m in campo.metadata if getattr(m, "le", None))
    assert FRACAO_MAXIMA_BPS == le == 10_000


# ---------------------------------------------------------------------------
# A cláusula não informativa NÃO fortalece veredito
# ---------------------------------------------------------------------------


def test_a_clausula_nao_informativa_nao_refuta():
    """Ela dispara, é publicada, e não decide nada.

    A hipótese 41 permanece imutável, e a cláusula dela continua com
    `disparou: true` — o que ela perde é o direito de fortalecer o veredito.
    Uma linha que não podia dar outra resposta não observou nada.
    """
    from app.hipotese import veredito as veredito_mod

    class _Realizado:
        def de(self, m):
            return {
                "idas_e_voltas": 999_999,
                "excesso_sobre_b3_cents": 9_000,
            }.get(m)

        def por_que_falta(self, m):
            return "ausente"

    # Uma factual que dispara SEMPRE (999.999 idas e voltas passam de 1), e a
    # obrigatória que não dispara.
    tautologica = ClausulaFalseamento(
        metrica="idas_e_voltas", comparador="maior_que", valor=1
    )
    pre = _pre(_obrigatoria(), tautologica)

    # Sem a marca, ela refutaria por FATO — e fato não depende de amostra.
    sem_marca = veredito_mod.emitir(
        pre, _Realizado(), n_efetivo=1, n_minimo=1
    )
    assert sem_marca.veredito == "refutada"

    # Com a marca, o mesmo disparo deixa de decidir.
    com_marca = veredito_mod.emitir(
        pre,
        _Realizado(),
        n_efetivo=1,
        n_minimo=1,
        clausulas_sem_voto={tautologica.como_texto(): "nao_informativa"},
    )
    assert com_marca.veredito != "refutada"

    # E ela continua VISÍVEL, com o disparo e a marca ao lado.
    linha = [
        c for c in com_marca.como_dict()["condicoes_falseamento"]
        if c["metrica"] == "idas_e_voltas"
    ][0]
    assert linha["disparou"] is True
    assert linha["nao_fortalece_veredito"] is True
    assert linha["classificacao"] == "nao_informativa"


def test_o_A1a_barra_a_clausula_tautologica():
    """A terceira tentativa do controle de custos, e o número é o da 41.

    Mora naquela família porque a guarda é a mesma — o schema do pré-registro
    recusando uma DECLARAÇÃO —, e porque a lista de §14.4 é fechada e citada:
    uma sétima família não caberia no orçamento 16+16+6+10 da D25.
    """
    from app.a1a import catalogo, injecoes

    tentativas = injecoes.metrica_sem_custo()
    assert len(tentativas) == 2
    tauto = [t for t in tentativas if "tautologica" in t.o_que][0]
    assert tauto.barrada is True
    # Pela POLITICA, e nao por impossibilidade - o teto do patrimonio nao e
    # estrutural, e o controle existe para provar que a faixa recusa.
    assert "ForaDaEscalaEconomica" in tauto.mecanismo
    assert "faixa economica admissivel" in tauto.mecanismo
    assert "dispara SEMPRE" not in tauto.mecanismo

    # E ela NÃO abriu família nova: a lista continua com as seis do documento.
    assert catalogo.QUANTAS == 6
    # A família que a hospeda declara as três tentativas e as duas guardas.
    fam = catalogo.POR_CHAVE["lucro_so_sem_custos"]
    assert "três tentativas" in fam.o_que_injeta
    assert "TAUTOLÓGICA" in fam.o_que_injeta
    assert "escala.py" in fam.guarda_esperada


# ---------------------------------------------------------------------------
# A deriva do contrato, que `config_hash_confere` NAO ve
# ---------------------------------------------------------------------------


def test_a_deriva_do_contrato_e_ACUSADA_por_campo_proprio(conn):
    """`config_hash_confere` respondeu `True` sobre um contrato defasado.

    **Medido em produção em 2026-09-09**: a `config_version` 8 guardava
    `contrato_do_agente_hash = 24e76a14…` e o código no ar calculava
    `131898fe…`. `conferir_hash` não estava errado — ele compara o payload
    GRAVADO com o hash GRAVADO, e os dois seguiam coerentes entre si. O que
    ninguém conferia era o payload contra o **código**.

    Mesma forma do defeito que `integridade.py` cometeu com a migração 28:
    `integras: true` sobre uma estrutura que tinha parado de descrever, no
    campo cuja única função é acusar.
    """
    import app.cerebro.prompts as prompts
    from app.relatorio import integridade

    bloco = integridade.contrato_do_agente(conn)
    assert bloco["confere"] is True
    assert bloco["se_nao_confere"] is None
    assert bloco["gravado_na_config_vigente"] == (
        bloco["calculado_do_codigo_no_ar"]
    )

    # Muda o prompt, e o campo tem de virar sozinho.
    original = prompts.SISTEMA
    try:
        prompts.SISTEMA = original + "\numa linha nova"
        depois = integridade.contrato_do_agente(conn)
        assert depois["confere"] is False
        assert depois["gravado_na_config_vigente"] != (
            depois["calculado_do_codigo_no_ar"]
        )
        assert "Reancore" in depois["se_nao_confere"]
    finally:
        prompts.SISTEMA = original

    # E ele explica por que o outro campo nao pega, junto do proprio campo.
    assert "GRAVADO" in bloco["por_que_config_hash_confere_nao_pega"]


def test_o_relatorio_de_integridade_PUBLICA_o_contrato(conn):
    """No relatório, e não só na função — a lição do `poder` que não aparecia."""
    from app.relatorio import integridade

    r = integridade.montar(conn)
    assert "contrato_do_agente" in r["identidades"]
    assert r["identidades"]["contrato_do_agente"]["confere"] is True
