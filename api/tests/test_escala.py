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


def test_o_dominio_sai_da_semente_e_do_horizonte_e_nao_de_constante():
    """Nenhum dos três limites é digitado: todos saem de insumo declarado."""
    teto = SEMENTE * escala.TETO_MULTIPLO_DA_SEMENTE_BPS // 10_000
    assert escala.dominio(
        "patrimonio_final_cents",
        semente_cents=SEMENTE,
        horizonte_barras=HORIZONTE,
    ) == (0, teto)
    # Excesso é diferença entre dois patrimônios: simétrico.
    assert escala.dominio(
        "excesso_sobre_b3_cents",
        semente_cents=SEMENTE,
        horizonte_barras=HORIZONTE,
    ) == (-teto, teto)
    # Uma ida e uma volta gastam duas barras — o máximo é aritmética.
    assert escala.dominio(
        "idas_e_voltas", semente_cents=SEMENTE, horizonte_barras=HORIZONTE
    ) == (0, HORIZONTE // 2)


def test_o_dominio_acompanha_a_semente_em_vez_de_fixar_um_numero():
    """Dobrar a semente dobra o teto — se não acompanhasse, seria constante."""
    _, teto_1 = escala.dominio(
        "patrimonio_final_cents",
        semente_cents=SEMENTE,
        horizonte_barras=HORIZONTE,
    )
    _, teto_2 = escala.dominio(
        "patrimonio_final_cents",
        semente_cents=SEMENTE * 2,
        horizonte_barras=HORIZONTE,
    )
    assert teto_2 == teto_1 * 2


# ---------------------------------------------------------------------------
# O CASO DA HIPOTESE 41
# ---------------------------------------------------------------------------


def test_o_caso_REAL_da_hipotese_41_e_recusado():
    """`patrimonio_final_cents < 950000` sobre semente de 100.000.

    O número exato que está gravado em produção. Se este teste passar a
    aceitá-lo, a correção foi desfeita.
    """
    bruto = _pre(
        _obrigatoria(),
        ClausulaFalseamento.da_semente(
            "patrimonio_final_cents", "menor_que",
            bps=95_000, semente_cents=SEMENTE,
        ),
    )
    # A cláusula gravada vale exatamente 950.000 centavos.
    assert bruto.condicoes_falseamento[1].valor == 950_000

    with pytest.raises(escala.ClausulaNaoInformativa) as erro:
        escala.conferir(
            bruto, semente_cents=SEMENTE, horizonte_barras=HORIZONTE
        )
    assert "dispara SEMPRE" in str(erro.value)
    assert "300000" in str(erro.value)  # o maximo observavel


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
    assert "idas_e_voltas" in str(erro.value)
    assert "nunca dispara" in str(erro.value)


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
    """Um número em centavos sozinho não diz se é um décimo ou dez vezes."""
    with pytest.raises(ValueError) as erro:
        _pre(
            ClausulaFalseamento(
                metrica="excesso_sobre_b3_cents",
                comparador="menor_que",
                valor=5_000,
            )
        )
    assert "sem referencia de escala" in str(erro.value)


def test_metrica_de_CONTAGEM_recusa_declaracao_relativa():
    """`idas_e_voltas` não tem semente a que se referir: bps ali é erro."""
    with pytest.raises(ValueError) as erro:
        _pre(
            _obrigatoria(),
            ClausulaFalseamento(
                metrica="idas_e_voltas",
                comparador="maior_que",
                valor=80,
                valor_bps_da_semente=800,
            ),
        )
    assert "CONTAGEM" in str(erro.value)


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
    assert fora[0]["metrica"] == "patrimonio_final_cents"
    assert fora[0]["dominio"] == [0, 300_000]
    assert fora[0]["nao_fortalece_veredito"] is True


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
    assert f"de 0 a {HORIZONTE // 2}" in texto
    assert "300000 centavos" in texto
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
        clausulas_nao_informativas=frozenset({tautologica.como_texto()}),
    )
    assert com_marca.veredito != "refutada"

    # E ela continua VISÍVEL, com o disparo e a marca ao lado.
    linha = [
        c for c in com_marca.como_dict()["condicoes_falseamento"]
        if c["metrica"] == "idas_e_voltas"
    ][0]
    assert linha["disparou"] is True
    assert linha["nao_informativa"] is True


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
    assert "ClausulaNaoInformativa" in tauto.mecanismo
    assert "dispara SEMPRE" in tauto.mecanismo

    # E ela NÃO abriu família nova: a lista continua com as seis do documento.
    assert catalogo.QUANTAS == 6
    # A família que a hospeda declara as três tentativas e as duas guardas.
    fam = catalogo.POR_CHAVE["lucro_so_sem_custos"]
    assert "três tentativas" in fam.o_que_injeta
    assert "TAUTOLÓGICA" in fam.o_que_injeta
    assert "escala.py" in fam.guarda_esperada
