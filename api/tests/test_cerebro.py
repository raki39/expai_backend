"""Testes do incremento 5: o cerebro lento entra no laco.

Nenhum teste daqui toca a rede. Os que exigem chamada real - `cache_read`
maior que zero na segunda reflexao (criterio 6) e a viabilidade do segundo
provedor (criterio 7b) - estao marcados `rede` e sao PULADOS sem chave. Nao
sao testes que passam por acidente: sao testes que ainda nao rodaram, e o
relatorio do incremento diz isso com todas as letras.

O adaptador falso nao e um atalho: ele existe para que a fronteira testada
seja a nossa - teto, cache, custo, validacao, registro - e nao a do provedor.
Um teste que dependesse do modelo responder certo mediria o modelo, nao o
codigo.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
from decimal import Decimal

import pytest

from app.cerebro import cache, ciclo, contexto, custo, grafo, prompts, propostas
from app.cerebro import reflexao, tetos
from app.cerebro.contrato import (
    SCHEMA_PROPOSTA,
    Interpretacao,
    PropostaBruta,
    montar_regra,
)
from app.cerebro.provedores.base import (
    Credenciais,
    ErroDoProvedor,
    Pedido,
    Resposta,
)
from app.config.schema import ExperimentConfig, PrecoModelo
from app.ledger import contas
from app.ledger.livro import (
    Uso,
    abrir_run,
    carteira,
    conferir_arredondamento_do_custo,
    conferir_partidas_dobradas,
    conferir_vinculo_inferencia,
    gasto_com_reflexao,
    saldo_da_conta,
)
from app.maos_rapidas import executor
from app.settings import get_settings
from tests.test_maos_rapidas import precos_passeio
from tests.test_simulador import criar_dataset

SEMENTE_USD = 100_000


# ===========================================================================
# Adaptador falso
# ===========================================================================


INTERPRETACAO_OK = json.dumps(
    {
        "regime": "indefinido",
        "diagnostico": "amplitude tipica proxima do custo de giro",
        "familia_recomendada": "cruzamento_medias",
    }
)

# O pre-registro da secao 8.2, que na 0B acompanha toda proposta valida.
#
# Sharpe 2,0 e uma declaracao HONESTA e, nesta fixture, NAO TESTAVEL: 2.500
# barras de 15 minutos sao ~26 dias, e o Sharpe minimo testavel ai e 7,49 -
# acima do teto de 5,00 que o schema aceita. Nenhuma hipotese e testavel numa
# janela de 26 dias, e isso e a secao 8.3 funcionando, nao defeito da fixture.
# Os testes que precisam do caminho TESTAVEL montam a sua propria janela.
PRE_REGISTRO_OK = {
    "enunciado": "cerca de 300 operacoes, provavelmente abaixo da mediana",
    "metrica_primaria": "excesso_sobre_b1_p50_cents",
    "efeito_minimo": 0,
    "sharpe_esperado_milesimos": 2_000,
    "criterio_parada": "fim_da_janela",
    "condicoes_falseamento": [
        {
            "metrica": "excesso_sobre_b1_p50_cents",
            "comparador": "menor_que",
            "valor": 0,
        }
    ],
}

# O mesmo bloco em JSON cru, para as respostas invalidas montadas a mao: o que
# se testa la e o parametro da REGRA, entao o pre-registro precisa estar
# valido para nao mascarar o motivo da recusa.
PRE_JSON = json.dumps(PRE_REGISTRO_OK)


PROPOSTA_OK = json.dumps(
    {
        "familia": "cruzamento_medias",
        "rapida": 20,
        "lenta": 50,
        "periodo": None,
        "desvios_milesimos": None,
        "position_fraction_bps": 10_000,
        "stop_loss_bps": None,
        "pre_registro": PRE_REGISTRO_OK,
        "confianca_ppm": 300_000,
    }
)


class AdaptadorFalso:
    """Devolve respostas roteirizadas e guarda os pedidos que recebeu."""

    def __init__(
        self,
        respostas: list[str],
        *,
        uso: Uso | None = None,
        provider: str = "anthropic",
        erro: Exception | None = None,
    ) -> None:
        self.provider = provider
        self.respostas = list(respostas)
        self.uso = uso or Uso(
            tokens_in=1_500, tokens_out=200, tokens_cache_read=0,
            tokens_cache_write=1_200, bruto={"falso": True},
        )
        self.erro = erro
        self.pedidos: list[Pedido] = []
        self.chaves_recebidas: list[str] = []

    def chamar(self, pedido: Pedido, *, credenciais: Credenciais) -> Resposta:
        self.pedidos.append(pedido)
        self.chaves_recebidas.append(credenciais.api_key)
        if self.erro is not None:
            raise self.erro
        if not self.respostas:
            raise AssertionError("o adaptador falso recebeu mais chamadas que o roteiro")
        return Resposta(texto=self.respostas.pop(0), uso=self.uso, bruto={})


@pytest.fixture
def cenario(conn: sqlite3.Connection):
    dataset_id = criar_dataset(conn, precos_passeio(2_500))
    return dataset_id, ExperimentConfig()


@pytest.fixture
def settings(ambiente):
    return get_settings()


def _rodar_ciclo(conn, cenario, settings, adaptador, config=None):
    dataset_id, cfg = cenario
    return ciclo.rodar(
        conn,
        dataset_id=dataset_id,
        config=config or cfg,
        config_version_id=1,
        settings=settings,
        adaptador=adaptador,
    )


# ===========================================================================
# CRITERIO 5 - pre-processamento e codigo, nao prompt
# ===========================================================================


def test_o_prompt_recebe_resumo_e_nao_o_log_bruto(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """O prompt nao pode conter linha de barra individual (secao 3.6 regra 4)."""
    adaptador = AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    _rodar_ciclo(conn, cenario, settings, adaptador)

    dataset_id, cfg = cenario
    # A janela que o CEREBRO observa e `exploracao` (D27), nao a de execucao.
    # O teste precisa varrer as barras que ele de fato viu: procurar timestamp
    # de in_sample no prompt nao provaria nada, porque o cerebro nunca os teve.
    barras = executor.carregar_janela(
        conn, dataset_id, finalidade="exploracao"
    )
    assert len(barras) > 400  # ha log bruto de sobra para vazar

    for pedido in adaptador.pedidos:
        texto = pedido.sistema + "".join(t for _, t in pedido.mensagens)
        # Nenhum timestamp de barra aparece no prompt.
        for barra in barras[:50]:
            assert str(barra.open_time_ms) not in texto
        # E o prompt inteiro cabe em poucos milhares de caracteres.
        assert len(texto) < 12_000


def test_resumo_e_deterministico_e_inteiro(cenario, conn) -> None:
    dataset_id, cfg = cenario
    barras = executor.carregar_janela(conn, dataset_id)
    a = contexto.resumir(barras, cfg)
    b = contexto.resumir(barras, cfg)
    assert a == b
    for valor in a.como_dict().values():
        assert not isinstance(valor, float)


def test_custo_de_giro_vem_da_config_e_nao_de_constante() -> None:
    """Se as taxas mudarem, o numero que vai ao modelo muda junto."""
    base = ExperimentConfig()
    caro = base.model_copy(update={"taker_fee_bps": Decimal("50")})
    assert contexto.custo_ida_e_volta_bps(caro) > contexto.custo_ida_e_volta_bps(base)


# ===========================================================================
# CRITERIO 7 - nenhum id de modelo fora da configuracao
# ===========================================================================


def test_nenhum_id_de_modelo_fora_da_configuracao() -> None:
    """Secao 3.9: o agente pede um tier, nunca um modelo.

    A busca e por PADRAO de id, e nao por uma lista de modelos conhecidos:
    uma lista protege contra o que ja foi lembrado, um padrao protege contra
    o modelo que alguem colar amanha.

    E olha LITERAIS DE STRING, via AST, e nao o texto cru do arquivo: um id
    de modelo so faz mal quando pode ser enviado ao provedor, e citar o nome
    de um arquivo de notas num comentario nao envia nada a lugar nenhum.
    """
    import ast
    import re

    padrao = re.compile(r"^(claude|gpt|o[1-9])-[a-z0-9.-]+$")
    raiz = pathlib.Path(__file__).resolve().parents[1] / "app"
    permitido = {raiz / "config" / "schema.py"}

    for arquivo in raiz.rglob("*.py"):
        if arquivo in permitido:
            continue
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            if isinstance(no, ast.Constant) and isinstance(no.value, str):
                assert not padrao.match(no.value.strip()), (
                    f"{arquivo.relative_to(raiz)}:{no.lineno} {no.value!r}"
                )


def test_o_codigo_de_decisao_nao_menciona_provedor() -> None:
    """Nome de provedor so na configuracao e na camada de adaptadores.

    `app/cerebro/provedores/` e a excecao declarada: a funcao dela e conhecer
    os SDKs. O resto do cerebro - grafo, contexto, prompts, tetos, custo -
    nao pode ter uma unica mencao, senao a decisao passa a depender de quem
    atende.
    """
    import ast

    raiz = pathlib.Path(__file__).resolve().parents[1] / "app"
    modulos = (
        "grafo.py", "contexto.py", "prompts.py", "tetos.py", "custo.py",
        "contrato.py", "propostas.py", "ciclo.py", "reflexao.py", "cache.py",
    )
    for nome in modulos:
        arvore = ast.parse((raiz / "cerebro" / nome).read_text(encoding="utf-8"))
        # Docstrings e comentarios explicam o porque e sao bem-vindos; o que
        # nao pode e o codigo EXECUTAVEL depender de quem atende. Comentario
        # nem chega na AST; docstring chega e e descartada aqui.
        docstrings = {
            id(no.body[0].value)
            for no in ast.walk(arvore)
            if isinstance(no, (ast.Module, ast.FunctionDef, ast.ClassDef))
            and no.body
            and isinstance(no.body[0], ast.Expr)
            and isinstance(no.body[0].value, ast.Constant)
        }
        for no in ast.walk(arvore):
            if isinstance(no, ast.Constant) and isinstance(no.value, str):
                if id(no) in docstrings:
                    continue
                texto = no.value.lower()
            elif isinstance(no, ast.Name):
                texto = no.id.lower()
            elif isinstance(no, ast.Attribute):
                texto = no.attr.lower()
            else:
                continue
            for proibido in ("anthropic", "openai"):
                assert proibido not in texto, (
                    f"cerebro/{nome}:{getattr(no, 'lineno', '?')} menciona {proibido}"
                )


def test_tier_resolve_para_provedor_e_modelo(cenario) -> None:
    _, cfg = cenario
    provider, model = reflexao.resolver_tier(cfg, "padrao")
    assert provider and model
    with pytest.raises(reflexao.TierNaoConfigurado):
        reflexao.resolver_tier(cfg, "inexistente")
    with pytest.raises(reflexao.TierNaoConfigurado):
        # `topo_alt` existe e aponta para um modelo vazio: "nao configurado"
        # nao e o mesmo que "inexistente", e os dois precisam recusar.
        reflexao.resolver_tier(cfg, "topo_alt")

    # E o segundo provedor resolve de verdade, com preco datado (ADR 0009).
    provedor_alt, modelo_alt = reflexao.resolver_tier(cfg, "padrao_alt")
    assert provedor_alt != provider
    assert custo.preco_de(cfg, provedor_alt, modelo_alt).verified_at


# ===========================================================================
# CRITERIO 1 e 7c - custo do uso real, e indisponivel nao e zero
# ===========================================================================


def test_custo_sai_do_usage_e_bate_com_a_tabela_de_precos() -> None:
    preco = PrecoModelo(
        provider="anthropic", model="modelo-de-teste",
        input_usd_per_mtok=Decimal("2.00"),
        output_usd_per_mtok=Decimal("10.00"),
        cache_read_usd_per_mtok=Decimal("0.20"),
        cache_write_usd_per_mtok=Decimal("2.50"),
        verified_at="2026-09-01",
    )
    uso = Uso(
        tokens_in=1_000_000, tokens_out=100_000,
        tokens_cache_read=1_000_000, tokens_cache_write=1_000_000,
    )
    conta = custo.calcular(uso, preco)
    assert conta.entrada_micro == 2_000_000        # US$ 2,00
    assert conta.saida_micro == 1_000_000          # US$ 1,00
    assert conta.cache_read_micro == 200_000       # US$ 0,20
    assert conta.cache_write_micro == 2_500_000    # US$ 2,50
    assert conta.total_micro == 5_700_000
    assert conta.total_cents == 570
    assert conta.completo


def test_token_ausente_com_preco_definido_nao_vira_zero() -> None:
    """Completar com zero seria estimar, e a secao 5.2 proibe estimar."""
    preco = PrecoModelo(
        provider="anthropic", model="modelo-de-teste",
        input_usd_per_mtok=Decimal("2.00"),
        output_usd_per_mtok=Decimal("10.00"),
        cache_read_usd_per_mtok=Decimal("0.20"),
        verified_at="2026-09-01",
    )
    with pytest.raises(custo.UsoIncompleto):
        custo.calcular(Uso(tokens_in=100, tokens_out=None), preco)


def test_componente_sem_preco_se_anuncia_em_vez_de_sumir() -> None:
    """E o caso da escrita de cache num provedor que nao a precifica."""
    preco = PrecoModelo(
        provider="anthropic", model="modelo-de-teste",
        input_usd_per_mtok=Decimal("2.00"),
        output_usd_per_mtok=Decimal("10.00"),
        cache_read_usd_per_mtok=Decimal("0.20"),
        cache_write_usd_per_mtok=None,
        verified_at="2026-09-01",
    )
    conta = custo.calcular(
        Uso(tokens_in=10, tokens_out=10, tokens_cache_read=0,
            tokens_cache_write=5_000),
        preco,
    )
    assert not conta.completo
    assert "cache_write" in conta.componentes_sem_preco


def test_preco_sem_data_de_verificacao_e_recusado() -> None:
    cfg = ExperimentConfig()
    sem_data = cfg.model_copy(
        update={
            "price_table": [
                PrecoModelo(
                    provider="anthropic", model=cfg.tiers["padrao"].model,
                    input_usd_per_mtok=Decimal("2"),
                    output_usd_per_mtok=Decimal("10"),
                    verified_at=None,
                )
            ]
        }
    )
    with pytest.raises(custo.SemPreco):
        custo.preco_de(sem_data, "anthropic", cfg.tiers["padrao"].model)


def test_indisponivel_fica_nulo_no_evento(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """Criterio 7c: o provedor nao informou escrita de cache -> NULL.

    O cenario e o do segundo provedor, que nao cobra nem reporta escrita de
    cache: a tabela de precos nao tem preco para o componente e a resposta
    nao traz o token. O par coerente e o que deixa o campo indisponivel.

    O par INcoerente - ha preco e nao ha token - e erro duro, e esta coberto
    em `test_token_ausente_com_preco_definido_nao_vira_zero`: ali completar
    com zero seria estimar.
    """
    _, cfg = cenario
    modelo = cfg.tiers["padrao"].model
    sem_preco_de_escrita = cfg.model_copy(
        update={
            "price_table": [
                PrecoModelo(
                    provider="anthropic", model=modelo,
                    input_usd_per_mtok=Decimal("2.00"),
                    output_usd_per_mtok=Decimal("10.00"),
                    cache_read_usd_per_mtok=Decimal("0.20"),
                    cache_write_usd_per_mtok=None,
                    verified_at="2026-09-02",
                )
            ]
        }
    )
    adaptador = AdaptadorFalso(
        [INTERPRETACAO_OK, PROPOSTA_OK],
        uso=Uso(tokens_in=1_000, tokens_out=100, tokens_cache_read=0,
                tokens_cache_write=None, bruto={"sem_cache_write": True}),
    )
    resultado = _rodar_ciclo(
        conn, cenario, settings, adaptador, config=sem_preco_de_escrita
    )
    linha = conn.execute(
        "SELECT tokens_cache_write, tokens_cache_read, usage_bruto_json"
        " FROM agent_event WHERE run_id = ? AND provider IS NOT NULL"
        " ORDER BY id LIMIT 1",
        (resultado.run_id,),
    ).fetchone()
    assert linha["tokens_cache_write"] is None   # indisponivel
    assert linha["tokens_cache_read"] == 0       # informado, e era zero
    assert "sem_cache_write" in linha["usage_bruto_json"]


def test_o_payload_bruto_do_provedor_e_preservado(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    adaptador = AdaptadorFalso(
        [INTERPRETACAO_OK, PROPOSTA_OK],
        uso=Uso(tokens_in=10, tokens_out=10, tokens_cache_read=0,
                tokens_cache_write=0, bruto={"campo_exotico_do_provedor": 7}),
    )
    resultado = _rodar_ciclo(conn, cenario, settings, adaptador)
    brutos = [
        l["usage_bruto_json"]
        for l in conn.execute(
            "SELECT usage_bruto_json FROM agent_event WHERE run_id = ?"
            "  AND usage_bruto_json IS NOT NULL",
            (resultado.run_id,),
        )
    ]
    assert brutos and all("campo_exotico_do_provedor" in b for b in brutos)


# ===========================================================================
# CRITERIO 2 - regra valida ou nada
# ===========================================================================


@pytest.mark.parametrize(
    "resposta,porque",
    [
        ('{"familia": "media_movel_exponencial", "rapida": 5, "lenta": 20,'
         ' "periodo": null, "desvios_milesimos": null,'
         ' "position_fraction_bps": 10000, "stop_loss_bps": null,'
         ' "pre_registro": ' + PRE_JSON + ', "confianca_ppm": 1}',
         "familia fora do catalogo fechado"),
        ('{"familia": "cruzamento_medias", "rapida": 50, "lenta": 20,'
         ' "periodo": null, "desvios_milesimos": null,'
         ' "position_fraction_bps": 10000, "stop_loss_bps": null,'
         ' "pre_registro": ' + PRE_JSON + ', "confianca_ppm": 1}',
         "rapida maior que a lenta"),
        ('{"familia": "cruzamento_medias", "rapida": 20, "lenta": 5000,'
         ' "periodo": null, "desvios_milesimos": null,'
         ' "position_fraction_bps": 10000, "stop_loss_bps": null,'
         ' "pre_registro": ' + PRE_JSON + ', "confianca_ppm": 1}',
         "parametro fora da faixa do catalogo"),
        ('{"familia": "cruzamento_medias", "rapida": 20, "lenta": 50,'
         ' "periodo": 14, "desvios_milesimos": null,'
         ' "position_fraction_bps": 10000, "stop_loss_bps": null,'
         ' "pre_registro": ' + PRE_JSON + ', "confianca_ppm": 1}',
         "parametro de outra familia junto"),
        ("isto nao e json", "nem json e"),
    ],
)
def test_resposta_invalida_vira_rejeicao_registrada(
    conn: sqlite3.Connection, cenario, settings, resposta: str, porque: str
) -> None:
    adaptador = AdaptadorFalso([INTERPRETACAO_OK, resposta])
    resultado = _rodar_ciclo(conn, cenario, settings, adaptador)

    todas = propostas.do_run(conn, resultado.run_id)
    assert len(todas) == 1, porque
    assert todas[0]["status"] == "rejeitada"
    assert todas[0]["rule_id"] is None
    assert todas[0]["rejection_reason"]
    # A resposta crua fica guardada: sem ela nao ha diagnostico possivel.
    assert todas[0]["raw_response_json"] == resposta
    # E nada foi executado sob uma regra que nao existe.
    assert propostas.regra_ativa(conn, resultado.run_id) is None
    assert not resultado.regra_veio_do_cerebro


def test_rejeicao_nao_troca_a_regra_ativa_anterior(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """A regra ativa e DERIVADA: uma rejeicao nao tem o que apontar."""
    dataset_id, cfg = cenario
    run_id, _ = abrir_run(
        conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD
    )
    barras = executor.carregar_janela(conn, dataset_id)
    dep = grafo.Dependencias(
        conn=conn, config=cfg, settings=settings,
        adaptador=AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK]),
    )
    grafo.rodar(dep, run_id=run_id, barras=barras)
    ativa_antes = propostas.regra_ativa(conn, run_id)
    assert ativa_antes is not None

    # Sem isto o segundo run acerta o cache e recebe a resposta VALIDA de
    # novo - o pedido e byte a byte o mesmo. O acaso mostrou que o cache
    # funciona; aqui o que se quer testar e a rejeicao.
    conn.execute("DELETE FROM llm_cache")

    dep_ruim = grafo.Dependencias(
        conn=conn, config=cfg, settings=settings,
        adaptador=AdaptadorFalso([INTERPRETACAO_OK, "{}"]),
    )
    grafo.rodar(dep_ruim, run_id=run_id, barras=barras)

    ativa_depois = propostas.regra_ativa(conn, run_id)
    assert ativa_depois["rule_id"] == ativa_antes["rule_id"]
    assert [p["status"] for p in propostas.do_run(conn, run_id)] == [
        "aceita", "rejeitada"
    ]


def test_proposta_e_imutavel(conn: sqlite3.Connection, cenario, settings) -> None:
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    with pytest.raises(sqlite3.IntegrityError, match="imutavel"):
        conn.execute(
            "UPDATE rule_proposal SET expectation = 'outra' WHERE run_id = ?",
            (resultado.run_id,),
        )


def test_o_banco_recusa_proposta_aceita_sem_regra(conn: sqlite3.Connection) -> None:
    """A coerencia esta no CHECK, nao na disciplina de quem grava."""
    run_id, _ = abrir_run(
        conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO rule_proposal (run_id, agent_event_id, proposed_at,"
            " status, raw_response_json) VALUES (?, 1, 'agora', 'aceita', '{}')",
            (run_id,),
        )


# ===========================================================================
# CRITERIO 3 - teto respeitado, maos rapidas continuam
# ===========================================================================


def test_teto_zero_completa_o_run_sem_nenhuma_chamada(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """Secao 3.6 regra 2: o cerebro para, as maos rapidas seguem."""
    dataset_id, cfg = cenario
    sem_cerebro = cfg.model_copy(update={"max_llm_calls_per_run": 0})
    adaptador = AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])

    resultado = ciclo.rodar(
        conn, dataset_id=dataset_id, config=sem_cerebro, config_version_id=1,
        settings=settings, adaptador=adaptador,
    )

    assert adaptador.pedidos == []          # nenhuma chamada aconteceu
    assert resultado.reflexoes == 0
    assert not resultado.regra_veio_do_cerebro
    assert resultado.parou_em == "interpretar"
    # E as maos rapidas rodaram a regra padrao ate o fim.
    assert resultado.execucao["execucoes"] > 0
    assert resultado.gasto["gasto_cents"] == 0


def test_teto_de_gasto_no_meio_para_o_cerebro_dali_em_diante(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    dataset_id, cfg = cenario
    # Teto que cabe uma chamada e nao cabe duas: a reserva conservadora da
    # segunda ja estoura.
    apertado = cfg.model_copy(update={"max_llm_usd_per_run_cents": 1})
    adaptador = AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])

    resultado = ciclo.rodar(
        conn, dataset_id=dataset_id, config=apertado, config_version_id=1,
        settings=settings, adaptador=adaptador,
    )
    assert len(adaptador.pedidos) <= 1
    assert resultado.parou_em in ("interpretar", "propor_regra")
    assert resultado.execucao["execucoes"] > 0   # o run terminou mesmo assim


def test_teto_do_ambiente_vence_o_teto_da_config(cenario, settings) -> None:
    """Secao 12.1: o limite inviolavel nao depende do banco."""
    _, cfg = cenario
    generoso = cfg.model_copy(update={"max_llm_usd_per_run_cents": 10**9})
    assert tetos.teto_de_gasto_cents(generoso, settings) == int(
        settings.llm_max_usd_absolute * 100
    )


def test_o_teto_e_lido_do_ledger_e_nao_de_contador(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """Um processo reiniciado nao pode zerar a contagem do run."""
    dataset_id, cfg = cenario
    # O run do ciclo, e nao "o ultimo run": desde que o ciclo produz seu
    # proprio B1 casado, o maior id e o do controle, nao o do agente.
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    run_id = resultado.run_id

    # Consulta feita "do zero", sem nenhum estado em memoria do run anterior.
    veredito = tetos.consultar(
        conn, run_id=run_id, config=cfg, settings=settings
    )
    assert veredito.chamadas_feitas == 2
    assert veredito.gasto_cents > 0


# ===========================================================================
# CRITERIO 4 - reprodutibilidade sem gasto
# ===========================================================================


def test_cache_quente_reproduz_o_digest_sem_gastar_real(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """O run barato tem o mesmo digest e nao gasta nenhum real."""
    frio = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    # O adaptador do segundo run tem roteiro VAZIO: se ele for chamado,
    # levanta. A unica forma de o run passar e todo pedido bater no cache.
    quente = _rodar_ciclo(conn, cenario, settings, AdaptadorFalso([]))

    assert quente.execucao["digest"] == frio.execucao["digest"]
    assert quente.regra_hash == frio.regra_hash
    assert quente.gasto["gasto_real_brl_cents"] == 0
    assert frio.gasto["gasto_real_brl_cents"] > 0
    # O agente pagou pelo pensamento nos dois: o cache e nosso, nao dele.
    assert quente.gasto["gasto_cents"] == frio.gasto["gasto_cents"]


def test_a_chave_do_cache_muda_quando_a_pergunta_muda() -> None:
    import dataclasses

    base = Pedido(
        provider="anthropic", model="modelo-de-teste", sistema="s", mensagens=(("user", "a"),),
        schema={"x": 1}, schema_nome="n", max_tokens=10,
    )
    for mudanca in (
        {"model": "outro"},
        {"sistema": "outro"},
        {"mensagens": (("user", "b"),)},
        {"schema": {"x": 2}},
        {"schema_nome": "outro"},
        {"max_tokens": 11},
    ):
        assert base.chave() != dataclasses.replace(base, **mudanca).chave()
    # E nao muda quando nada muda: cache que erra o acerto nao serve.
    assert base.chave() == dataclasses.replace(base).chave()


def test_o_cache_recusa_trocar_a_resposta_de_uma_chave(
    conn: sqlite3.Connection
) -> None:
    pedido = Pedido(
        provider="anthropic", model="modelo-de-teste", sistema="s", mensagens=(("user", "a"),),
        schema={}, schema_nome="n", max_tokens=10,
    )
    cache.guardar(conn, pedido, Resposta(texto="primeira", uso=Uso()), custo_micro=0)
    cache.guardar(conn, pedido, Resposta(texto="segunda", uso=Uso()), custo_micro=0)
    acerto = cache.buscar(conn, pedido)
    assert acerto is not None and acerto.resposta.texto == "primeira"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE llm_cache SET response_json = '{}' WHERE key = ?",
                     (pedido.chave(),))


# ===========================================================================
# CRITERIO 9 - um evento por no, inclusive quando falha
# ===========================================================================


def test_um_evento_por_no_com_pai_encadeado(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    caminho = ciclo.caminho_percorrido(conn, resultado.run_id)

    # Os quatro NOS DO GRAFO, nesta ordem. A separacao importa: o quinto
    # evento e a avaliacao posterior, que nao e no do grafo nenhum - ela roda
    # depois das maos rapidas, e as maos rapidas nao sao nos (regra 3).
    nos_do_grafo = [e["node"] for e in caminho if e["kind"] != "avaliacao"]
    assert nos_do_grafo == [
        "observar", "interpretar", "propor_regra", "registrar_intencao"
    ]
    assert [e["node"] for e in caminho][-1] == "avaliar_resultado"

    # Encadeados: do ultimo NO se chega ao primeiro subindo por
    # parent_event_id.
    por_id = {e["id"]: e for e in caminho}
    atual = [e for e in caminho if e["node"] == "registrar_intencao"][-1]
    subidas = 0
    while atual["parent_event_id"] is not None:
        atual = por_id[atual["parent_event_id"]]
        subidas += 1
    assert subidas == 3 and atual["node"] == "observar"


def test_no_que_falha_grava_o_evento_de_erro(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """Um no que some do registro faz o caminho contar so a parte boa."""
    adaptador = AdaptadorFalso([], erro=ErroDoProvedor("provedor fora do ar"))
    resultado = _rodar_ciclo(conn, cenario, settings, adaptador)

    caminho = ciclo.caminho_percorrido(conn, resultado.run_id)
    assert [e["node"] for e in caminho] == ["observar", "interpretar"]
    assert caminho[-1]["kind"] == "parada"
    assert resultado.parou_em == "interpretar"
    assert "fora do ar" in (resultado.motivo or "")
    # E o run terminou com a regra padrao, nao abortou.
    assert resultado.execucao["execucoes"] > 0


def test_agent_event_e_imutavel(conn: sqlite3.Connection, cenario, settings) -> None:
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    with pytest.raises(sqlite3.IntegrityError, match="imutavel"):
        conn.execute(
            "UPDATE agent_event SET node = 'outro' WHERE run_id = ?",
            (resultado.run_id,),
        )


# ===========================================================================
# CRITERIO 10 - expectativa declarada ANTES da execucao
# ===========================================================================


def test_a_intencao_e_gravada_antes_da_primeira_execucao(
    conn: sqlite3.Connection, cenario, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R25.3: a ordem temporal e verificavel, nao prometida."""
    # Nao da para comparar relogios: `execution` nao tem um, e o
    # `occurred_at` da transacao dela e o timestamp DA BARRA, nao da
    # gravacao - usa-lo aqui compararia epocas diferentes e o teste passaria
    # ou falharia por motivo nenhum.
    #
    # A pergunta do criterio 10 e sobre ORDEM, entao a resposta e observar a
    # ordem: no instante em que as maos rapidas sao chamadas, a intencao ja
    # tem de estar gravada e nao pode existir execucao nenhuma ainda.
    visto: dict = {}
    original = ciclo.executor.rodar

    def espiao(conn_, **kwargs):
        run = kwargs["run_id"]
        visto["proposta"] = propostas.regra_ativa(conn_, run)
        visto["execucoes_antes"] = conn_.execute(
            "SELECT COUNT(*) AS n FROM execution WHERE run_id = ?", (run,)
        ).fetchone()["n"]
        return original(conn_, **kwargs)

    monkeypatch.setattr(ciclo.executor, "rodar", espiao)
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )

    assert visto["execucoes_antes"] == 0
    assert visto["proposta"] is not None
    assert visto["proposta"]["expectation"]
    assert visto["proposta"]["confidence_ppm"] is not None
    assert resultado.expectativa == visto["proposta"]["expectation"]


def test_expectativa_declarada_sobrevive_ao_resultado(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """A avaliacao posterior sera evento NOVO; a decisao nao muda (regra 17)."""
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    assert resultado.expectativa
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE rule_proposal SET confidence_ppm = 0 WHERE run_id = ?",
            (resultado.run_id,),
        )


# ===========================================================================
# CRITERIO 11 - perfil presente e inerte
# ===========================================================================


def test_profile_id_presente_e_nenhum_ramo_o_le(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    perfis = {
        l["profile_id"]
        for l in conn.execute(
            "SELECT profile_id FROM agent_event WHERE run_id = ?",
            (resultado.run_id,),
        )
    }
    assert perfis == {"neutro@1"}

    # Gravar e ler e legitimo. O que a regra 18 proibe e um ramo DEPENDER
    # do valor - e comparacao e como essa dependencia se escreve.
    import re

    comparacao = re.compile(
        r"profile_id\s*(==|!=|<|>|in|is)|"
        r"(==|!=|in)\s*[\"']?profile_id"
    )
    raiz = pathlib.Path(__file__).resolve().parents[1] / "app"
    for arquivo in raiz.rglob("*.py"):
        for numero, linha in enumerate(
            arquivo.read_text(encoding="utf-8").splitlines(), 1
        ):
            despida = linha.strip()
            if "profile_id" not in despida or despida.startswith(("#", "--")):
                continue
            assert not comparacao.search(despida), f"{arquivo.name}:{numero} {despida}"


# ===========================================================================
# CRITERIO 12 - sem segunda fonte de verdade sobre dinheiro
# ===========================================================================


def test_o_saldo_vem_do_ledger_e_nao_do_fluxo_de_eventos(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    antes = carteira(conn, run_id=resultado.run_id)

    # Um evento cognitivo com custo, gravado A MAO e SEM lancamento nenhum.
    # Se a carteira lesse `agent_event`, ela mudaria aqui.
    conn.execute(
        "INSERT INTO agent_event (run_id, occurred_at, node, kind, provider,"
        " model, cost_usd_minor, cost_usd_micro)"
        " VALUES (?, 'agora', 'inventado', 'reflexao', 'x', 'y', 99999, 999990000)",
        (resultado.run_id,),
    )
    assert carteira(conn, run_id=resultado.run_id) == antes

    # E a conferencia acusa o orfao, que e o outro lado da mesma garantia:
    # evento com custo sem contrapartida no ledger e defeito, nao saldo.
    vinculo = conferir_vinculo_inferencia(conn)
    assert vinculo["eventos_com_custo_sem_lancamento"]


# Tabelas com "state" no nome que NAO sao estado de grafo. A lista e explicita
# de proposito: acrescentar uma exige escrever aqui por que ela nao e
# checkpointer, e a guarda continua pegando o caso que importa.
TABELAS_DE_ESTADO_LEGITIMAS = {
    # Maquina de estados do CONHECIMENTO (§8.1, incremento 10). Nao tem
    # relacao com o grafo: guarda em que ponto do protocolo de validacao cada
    # HIPOTESE esta, e o grafo nem sabe que ela existe - quem escreve nela e
    # o validador.
    "hypothesis_state",
}


def test_o_estado_do_grafo_nao_e_persistido(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """Criterio 12: o estado do GRAFO e efemero. Nao existe tabela para ele.

    A guarda foi afiada no incremento 10, e nao afrouxada. Ela proibia
    qualquer tabela com "state" no nome; `hypothesis_state` e legitima e
    passou a constar de uma lista explicita. O que a guarda continua pegando -
    um checkpointer do LangGraph, que tornaria o estado do grafo duravel e
    faria dele uma segunda fonte de verdade - segue proibido.
    """
    tabelas = {
        l["name"]
        for l in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    suspeitas = {
        t for t in tabelas
        if ("state" in t or "checkpoint" in t)
        and t not in TABELAS_DE_ESTADO_LEGITIMAS
    }
    assert not suspeitas, f"tabela de estado nao declarada: {suspeitas}"

    # A guarda nao pode ser vazia: se `hypothesis_state` sumisse, este teste
    # passaria por nao haver nada a filtrar.
    assert "hypothesis_state" in tabelas

    # E o que ela protege continua verdade: nenhuma tabela guarda o estado do
    # GRAFO. As colunas de `hypothesis_state` sao sobre a hipotese, nao sobre
    # nos percorridos.
    colunas = {
        l["name"]
        for l in conn.execute("PRAGMA table_info(hypothesis_state)")
    }
    assert not {c for c in colunas if "node" in c or "checkpoint" in c}


# ===========================================================================
# Contabilidade: o dinheiro fecha, e os dois registros se apontam
# ===========================================================================


def test_o_ciclo_fecha_as_partidas_dobradas(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    assert conferir_partidas_dobradas(conn) == []
    assert conferir_arredondamento_do_custo(conn) == []
    vinculo = conferir_vinculo_inferencia(conn)
    assert vinculo["transacoes_sem_evento"] == []
    assert vinculo["eventos_com_custo_sem_lancamento"] == []
    assert vinculo["vinculos_assimetricos"] == []
    assert resultado.gasto["gasto_cents"] > 0


def test_o_custo_de_reflexao_sai_do_caixa_do_agente(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """O agente paga pelo proprio pensamento, dentro do resultado dele."""
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    tesouraria = saldo_da_conta(
        conn, contas.TESOURARIA_SIM, run_id=resultado.run_id
    )
    assert tesouraria == resultado.gasto["gasto_cents"] > 0


def test_reflexao_de_custo_zero_nao_cria_transacao(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """Os nos deterministicos nao movem dinheiro e nao deixam transacao aberta."""
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    abertas = conn.execute(
        "SELECT COUNT(*) AS n FROM ledger_transaction WHERE posted_at IS NULL"
    ).fetchone()["n"]
    assert abertas == 0
    observar = conn.execute(
        "SELECT ledger_transaction_id FROM agent_event"
        " WHERE run_id = ? AND node = 'observar'",
        (resultado.run_id,),
    ).fetchone()
    assert observar["ledger_transaction_id"] is None


# ===========================================================================
# Fronteira e procedencia
# ===========================================================================


def test_o_executor_nao_sabe_de_onde_a_regra_veio(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """A regra proposta roda pelo mesmo caminho do B3, sem adaptacao."""
    from app.maos_rapidas import baselines

    dataset_id, cfg = cenario
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    run_b3, _ = abrir_run(
        conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD
    )
    b3 = baselines.rodar_b3(conn, run_id=run_b3, dataset_id=dataset_id, config=cfg)

    # A proposta falsa e exatamente o B3, entao as DECISOES coincidem barra
    # a barra: mesmos lados, mesmas barras de decisao. E o que prova que nao
    # ha caminho especial para a regra vinda do cerebro.
    def decisoes(run: int) -> list[tuple[str, int]]:
        return [
            (l["side"], l["decision_bar_ms"])
            for l in conn.execute(
                "SELECT side, decision_bar_ms FROM execution"
                " WHERE run_id = ? ORDER BY id",
                (run,),
            )
        ]

    assert decisoes(resultado.run_id) == decisoes(run_b3)

    # Os digests NAO coincidem, e isso esta certo: o agente pagou pelo
    # proprio pensamento antes de operar, entao entrou no mercado com menos
    # caixa e comprou quantidades menores. Se coincidissem, o custo cognitivo
    # nao estaria dentro do resultado do agente - que e o contrario do que a
    # secao 3.6 exige.
    assert resultado.execucao["digest"] != b3.digest


def test_a_procedencia_da_regra_vem_do_experimento_e_nao_do_modelo(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    _, cfg = cenario
    regra = conn.execute(
        "SELECT condicoes_validade_json FROM rule WHERE id = ?",
        (resultado.rule_id,),
    ).fetchone()
    condicoes = json.loads(regra["condicoes_validade_json"])
    assert condicoes == {
        "venue": cfg.market_venue,
        "symbol": cfg.market_symbol,
        "timeframe": cfg.timeframe,
        "fidelity_level": cfg.fidelity_level,
    }


def test_a_sobreposicao_amostral_e_calculada_e_nao_afirmada(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """O numero mudou de 100% para ZERO sem a funcao ser tocada.

    Na 0A este teste exigia `em_amostra is True` e `sobreposicao_bps ==
    10_000`, porque a D22 fazia o cerebro observar a mesma janela que executa
    - nao havia separacao, e declarar a sobreposicao era a unica alternativa
    honesta a fingir que ela nao existia.

    Com os quatro conjuntos da D27, o cerebro observa `exploracao` e as maos
    executam `in_sample`. A sobreposicao caiu a zero, e **`sobreposicao_amostral`
    nao foi alterada** - ela sempre calculou do que ficou gravado.

    E o retorno concreto de a D22 ter sido escrita como NUMERO CALCULADO em vez
    de frase de prosa. Uma frase dizendo "o resultado e em amostra" teria
    sobrevivido intacta a este incremento, descrevendo um desenho extinto.
    """
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    assert resultado.sobreposicao["em_amostra"] is False
    assert resultado.sobreposicao["sobreposicao_bps"] == 0


def test_condicoes_do_run_acompanham_o_resultado(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    assert "Fidelidade 1" in resultado.condicoes_validade
    assert "Nenhuma conclusao estatistica" in resultado.condicoes_validade


def test_a_chave_do_provedor_nunca_entra_no_cache_nem_no_evento(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """Secao 10.2.4: segredo nao aparece em lugar nenhum que seja gravado."""
    adaptador = AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    _rodar_ciclo(conn, cenario, settings, adaptador)
    segredo = settings.anthropic_api_key.get_secret_value()
    assert adaptador.chaves_recebidas and all(
        c == segredo for c in adaptador.chaves_recebidas
    )

    for tabela, coluna in (
        ("llm_cache", "request_json"),
        ("llm_cache", "response_json"),
        ("agent_event", "usage_bruto_json"),
        ("agent_event", "inputs_digest"),
    ):
        for linha in conn.execute(f"SELECT {coluna} AS v FROM {tabela}"):
            assert segredo not in (linha["v"] or "")


def test_o_prefixo_de_cache_tem_folga_sobre_o_minimo() -> None:
    """Prefixo curto demais nunca e gravado no cache, e o criterio 6 falharia
    por um motivo que nao e invalidacao - mandando procurar no lugar errado."""
    assert not prompts.prefixo_curto_demais()


def test_o_bloco_de_sistema_e_identico_nas_duas_chamadas(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """Qualquer variacao aqui zera o `cache_read` sem erro aparente."""
    adaptador = AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    _rodar_ciclo(conn, cenario, settings, adaptador)
    sistemas = {p.sistema for p in adaptador.pedidos}
    assert len(adaptador.pedidos) == 2 and len(sistemas) == 1


# ===========================================================================
# Testes que exigem rede. Pulados sem chave - e o relatorio diz isso.
# ===========================================================================


# Interruptor explicito, e nao uma segunda copia da chave. A garantia que
# importa e "rodar a suite nao gasta dinheiro por descuido", e a forma honesta
# de escrever isso e um campo que diz exatamente isso - nao um segredo
# duplicado no mesmo arquivo, que so multiplica o lugar de onde ele pode
# vazar.
#
#   RODAR_TESTES_DE_REDE=1 python -m pytest -m rede
INTERRUPTOR = "RODAR_TESTES_DE_REDE"


def _rede_desligada() -> bool:
    import os

    return os.getenv(INTERRUPTOR, "") not in ("1", "true", "sim")


def _chave_do_arquivo(nome: str) -> str:
    """Le a chave do `.env` do servico, sem passar pelo ambiente.

    Tem de ser assim: a fixture `ambiente` injeta chaves FALSAS em variavel de
    ambiente para provar que segredo nao vaza, e variavel de ambiente vence o
    arquivo. Um teste de rede que lesse do ambiente autenticaria com a chave
    falsa e falharia com 401 - dizendo "o adaptador nao funciona" quando o que
    nao funcionou foi o teste.

    A chave lida aqui nunca e gravada, logada nem devolvida em nada.
    """
    arquivo = pathlib.Path(__file__).resolve().parents[1] / ".env"
    if not arquivo.exists():
        return ""
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        despida = linha.strip()
        if despida.startswith(f"{nome}="):
            return despida.split("=", 1)[1].strip().strip("\"'")
    return ""


@pytest.mark.rede
@pytest.mark.skipif(
    _rede_desligada(),
    reason=f"criterio 6 gasta dinheiro de verdade; ligue com {INTERRUPTOR}=1",
)
def test_cache_de_prompt_funciona_na_segunda_reflexao(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """Criterio 6: `cache_read_input_tokens` > 0 a partir da segunda reflexao.

    **Medido em 2026-09-02, e mudou o que este teste pode afirmar:** o schema
    de saida faz parte do prefixo cacheado. Sistema identico com schema
    diferente vem FRIO (`read=0, write=3567`). Como os dois nos pedem
    respostas de formatos diferentes, cada um tem sua propria entrada de
    cache - e a segunda reflexao de um run inteiramente frio nao tem como ler
    um prefixo que nunca foi escrito.

    Isso NAO e o invalidador silencioso que o criterio persegue. A diferenca
    esta em ser medida: um `datetime.now()` no prefixo zeraria o cache de
    TODAS as chamadas, sempre. Aqui cada prefixo esquenta na primeira vez e
    e lido em toda vez seguinte.

    Entao o teste roda o ciclo DUAS vezes, esvaziando o nosso cache de
    respostas entre elas para que as chamadas aconteçam de verdade. No
    segundo ciclo, TODA reflexao tem de ler cache. Se alguma vier zero ali,
    ai sim ha invalidador - e o defeito e nosso.
    """
    from pydantic import SecretStr

    chave = _chave_do_arquivo("ANTHROPIC_API_KEY")
    if not chave:
        pytest.skip("ANTHROPIC_API_KEY ausente do .env do servico")
    reais = settings.model_copy(
        update={
            "anthropic_api_key": SecretStr(chave),
            # Chave ligada a identidade exige o id do workspace junto; chave
            # de workspace ignora o cabecalho. Manda-se quando existe.
            "anthropic_workspace_id": _chave_do_arquivo("ANTHROPIC_WORKSPACE_ID"),
        }
    )

    def reflexoes(run_id: int) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT node, tokens_cache_read, tokens_cache_write"
                " FROM agent_event WHERE run_id = ? AND provider IS NOT NULL"
                " ORDER BY id",
                (run_id,),
            )
        )

    primeiro = _rodar_ciclo(conn, cenario, reais, None)
    assert len(reflexoes(primeiro.run_id)) >= 2, (
        f"o cerebro nao chegou a chamar duas vezes: parou em "
        f"{primeiro.parou_em!r} porque {primeiro.motivo!r}"
    )
    # Na primeira passagem cada prefixo ou foi ESCRITO no cache, ou ja estava
    # quente de antes e foi LIDO. Exigir escrita seria exigir que o cache
    # estivesse frio, que e uma condicao que este teste nao controla - o
    # prefixo pode ter esquentado num run anterior, e isso e o cache
    # funcionando, nao falhando. O que nao pode e ele nao engajar de forma
    # nenhuma.
    for linha in reflexoes(primeiro.run_id):
        engajou = (linha["tokens_cache_write"] or 0) + (
            linha["tokens_cache_read"] or 0
        )
        assert engajou > 0, (
            f"{linha['node']}: o prefixo nao foi lido nem gravado no cache -"
            " provavelmente esta abaixo do minimo cacheavel do modelo"
        )

    # Esvazia o NOSSO cache para que o segundo ciclo chame de verdade. Sem
    # isto ele seria servido localmente e nao mediria cache nenhum.
    conn.execute("DELETE FROM llm_cache")
    segundo = _rodar_ciclo(conn, cenario, reais, None)

    leituras = reflexoes(segundo.run_id)
    assert len(leituras) >= 2
    for linha in leituras:
        assert linha["tokens_cache_read"] and linha["tokens_cache_read"] > 0, (
            f"{linha['node']}: cache_read zero com o prefixo ja quente."
            " Ha invalidador silencioso no prefixo, e o defeito e nosso."
        )


@pytest.mark.rede
@pytest.mark.skipif(
    _rede_desligada(),
    reason=f"criterio 7b gasta dinheiro de verdade; ligue com {INTERRUPTOR}=1",
)
def test_segundo_provedor_valida_contra_o_mesmo_schema(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """Criterio 7b: viabilidade nunca exercitada e suposicao (ADR 0009)."""
    from app.cerebro.provedores import adaptador_de

    _, cfg = cenario
    # O modelo vem da CONFIGURACAO, como tudo (secao 3.9): o teste nao escolhe
    # modelo, ele exercita o tier alternativo que a config declara.
    provedor, modelo = reflexao.resolver_tier(cfg, "padrao_alt")
    chave = _chave_do_arquivo("OPENAI_API_KEY")
    if not chave:
        pytest.skip("OPENAI_API_KEY ausente do .env do servico")

    pedido = Pedido(
        provider=provedor, model=modelo, sistema=prompts.SISTEMA,
        mensagens=(("user", prompts.mensagem_propor(
            contexto.resumir(executor.carregar_janela(conn, cenario[0]), cfg),
            Interpretacao.model_validate_json(INTERPRETACAO_OK),
        )),),
        schema=SCHEMA_PROPOSTA, schema_nome="proposta_de_regra", max_tokens=2_000,
    )
    resposta = adaptador_de(provedor).chamar(
        pedido, credenciais=Credenciais(api_key=chave)
    )
    bruta = PropostaBruta.model_validate_json(resposta.texto)
    montar_regra(bruta, cfg)
    # E os campos de uso chegaram ao normalizador.
    assert resposta.uso.tokens_in is not None
    assert resposta.uso.tokens_out is not None


# ===========================================================================
# Rotas
# ===========================================================================


def test_agente_exige_token(client) -> None:
    sem_token = {"Authorization": ""}
    assert client.post("/api/agente", json={"author": "x"},
                       headers=sem_token).status_code == 401
    assert client.get("/api/agente", headers=sem_token).status_code == 401


def test_agente_sem_run_ainda_responde_vazio(client) -> None:
    corpo = client.get("/api/agente").json()
    assert corpo["run_id"] is None
    assert corpo["caminho"] == [] and corpo["propostas"] == []


def test_agente_recusa_sem_dataset(client) -> None:
    """Gastar token de LLM antes de haver dado seria gastar por nada."""
    resposta = client.post("/api/agente", json={"author": "teste"})
    assert resposta.status_code == 409
    assert "dataset" in resposta.json()["detail"]


def test_agente_recusa_com_run_ativo(client, conn) -> None:
    criar_dataset(conn, precos_passeio(2_500))
    abrir_run(conn, config_version_id=1, seed_capital_usd_cents=SEMENTE_USD)
    resposta = client.post("/api/agente", json={"author": "teste"})
    assert resposta.status_code == 409
    assert "run ativo" in resposta.json()["detail"]


def test_a_rota_do_agente_mostra_o_caminho_percorrido(
    client, conn, settings
) -> None:
    dataset_id = criar_dataset(conn, precos_passeio(2_500))
    ciclo.rodar(
        conn, dataset_id=dataset_id, config=ExperimentConfig(),
        config_version_id=1, settings=settings,
        adaptador=AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK]),
    )
    corpo = client.get("/api/agente").json()
    # Cinco eventos: os quatro nos do grafo e a avaliacao posterior, que nao e
    # no de grafo nenhum - ela roda depois das maos rapidas (regra 3).
    assert [e["node"] for e in corpo["caminho"]] == [
        "observar", "interpretar", "propor_regra", "registrar_intencao",
        "avaliar_resultado",
    ]
    assert corpo["regra_ativa"]["expectation"]
    assert corpo["gasto"]["gasto_cents"] > 0
    assert corpo["arredondamento_do_custo_ok"] is True
    # ZERO desde o incremento 9: o cerebro observa `exploracao` e as maos
    # executam `in_sample` (D27). Era 10.000 na 0A, e o campo nao foi tocado.
    assert corpo["sobreposicao_amostral"]["sobreposicao_bps"] == 0


def test_nenhuma_rota_expoe_a_chave_do_provedor(client, conn, settings) -> None:
    """Secao 10.2.4: segredo nao aparece em pagina, log ou /api/substrato/health."""
    dataset_id = criar_dataset(conn, precos_passeio(2_500))
    ciclo.rodar(
        conn, dataset_id=dataset_id, config=ExperimentConfig(),
        config_version_id=1, settings=settings,
        adaptador=AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK]),
    )
    segredo = settings.anthropic_api_key.get_secret_value()
    for rota in ("/api/substrato/health", "/api/agente", "/api/config", "/api/ledger"):
        assert segredo not in client.get(rota).text


# ===========================================================================
# B1 casado com o giro do agente (D19 aplicada ao agente)
# ===========================================================================


def test_o_ciclo_produz_o_b1_casado_com_o_proprio_giro(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """Cada ida e volta paga pedagio fixo e o acaso nao tem vantagem nenhuma.

    Um B1 que gire mais que o agente perde por atrito, e o agente pareceria
    bom por ter operado menos. Casar o giro e o que faz a comparacao medir
    escolha de momento em vez de custo.
    """
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    assert resultado.b1_casado is not None
    assert resultado.b1_casado["operacoes_alvo"] == resultado.execucao["operacoes"]
    assert resultado.b1_casado["p5"] <= resultado.b1_casado["p50"]
    assert resultado.b1_casado["p50"] <= resultado.b1_casado["p95"]

    corpo = resultado.como_dict()
    # Regra 14: desempenho como excesso sobre baseline, nunca absoluto.
    assert corpo["excesso_sobre_b1_p50_cents"] == (
        resultado.patrimonio_final_cents - resultado.b1_casado["p50"]
    )


def test_o_b1_do_agente_nao_contamina_o_b1_da_comparacao(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """Dois B1 coexistem, com giros diferentes, e nao podem se misturar.

    Antes do filtro por marcador, `resumo_comparacao` pegava "o ultimo B1 que
    existe" - e passaria a mostrar a distribuicao casada com o agente ao lado
    de um B3 com outro giro, em silencio.
    """
    from app.maos_rapidas import baselines

    dataset_id, cfg = cenario
    comparacao = baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1, semente=42
    )
    giro_do_b3 = comparacao["B1"]["operacoes_alvo"]

    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )

    resumo = baselines.resumo_comparacao(conn)
    assert resumo["B1"]["operacoes_alvo"] == giro_do_b3

    do_agente = baselines.b1_do_agente(conn)
    assert do_agente["operacoes_alvo"] == resultado.execucao["operacoes"]
    assert do_agente["run_id"] != resumo["B1"].get("run_id", -1)


def test_sem_operacao_nao_ha_b1_para_casar(
    conn: sqlite3.Connection, settings
) -> None:
    """Zero operacoes nao tem controle possivel, e o campo diz isso com None.

    Serie estritamente crescente: a media rapida NASCE acima da lenta e nunca
    cruza. O sinal e evento, nao estado - entao nao ha entrada nenhuma, e
    casar o acaso com zero operacoes seria sortear zero pares.
    """
    dataset_id = criar_dataset(conn, list(range(50_000, 52_500)))
    cfg = ExperimentConfig().model_copy(update={"max_llm_calls_per_run": 0})

    resultado = ciclo.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        settings=settings, adaptador=AdaptadorFalso([]),
    )
    assert resultado.execucao["operacoes"] == 0
    assert resultado.b1_casado is None
    assert resultado.como_dict()["excesso_sobre_b1_p50_cents"] is None


def test_o_b1_casado_usa_o_mesmo_tamanho_de_posicao_da_regra(
    conn: sqlite3.Connection, cenario, settings
) -> None:
    """Secao 14.3: "mesmo tamanho de posicao e as mesmas taxas".

    Casar o giro e nao casar o tamanho mede DIMENSIONAMENTO em vez de timing,
    que e o mesmo erro da D19 um nivel abaixo. O custo de cada ida e volta e
    proporcional ao nocional: operar com 30% do caixa paga 30% do pedagio por
    operacao, e com centenas de operacoes isso domina qualquer efeito de
    escolha de momento.

    Foi assim que o primeiro resultado do agente em producao apareceu "acima
    do p95" de um acaso que operava com o caixa inteiro contra uma regra que
    operava com 30%.
    """
    proposta_com_fracao = json.dumps(
        {
            "familia": "banda_desvio", "rapida": None, "lenta": None,
            "periodo": 50, "desvios_milesimos": 2500,
            "position_fraction_bps": 3000, "stop_loss_bps": 800,
            "pre_registro": PRE_REGISTRO_OK,
            "confianca_ppm": 300_000,
        }
    )
    resultado = _rodar_ciclo(
        conn, cenario, settings,
        AdaptadorFalso([INTERPRETACAO_OK, proposta_com_fracao]),
    )
    assert resultado.b1_casado is not None
    assert resultado.b1_casado["fracao_bps"] == 3000

    from app.maos_rapidas import baselines

    assert baselines.b1_do_agente(conn)["fracao_bps"] == 3000


def test_fracao_menor_faz_o_acaso_perder_menos(conn: sqlite3.Connection, cenario) -> None:
    """A prova de que o casamento importa: se nao importasse, nao precisaria.

    Mesma semente, mesmo giro, mesma janela - so o tamanho da posicao muda.
    Se as duas distribuicoes fossem parecidas, casar o tamanho seria
    preciosismo. Elas nao sao.
    """
    from app.maos_rapidas import baselines

    dataset_id, cfg = cenario
    barras = executor.carregar_janela(conn, dataset_id)
    cheio = baselines.b1_casado_com(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        operacoes_alvo=60, fracao_bps=10_000, semente=42, barras=barras,
    )
    parcial = baselines.b1_casado_com(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        operacoes_alvo=60, fracao_bps=3_000, semente=42, barras=barras,
    )
    assert parcial["p50"] > cheio["p50"]
