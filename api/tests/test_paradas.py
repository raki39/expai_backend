"""Incremento 11b: a parada diz por que parou, e quem pode reivindicar o run.

Dois defeitos, um so sintoma. O run 27 de producao apareceu no painel com
`faixa: "entre_p50_e_p95"` sobre 244 idas e voltas, e:

- o cerebro havia **parado** em `propor_regra`, sem propor nada;
- as maos rapidas rodaram a regra padrao por baixo (o cruzamento do B3);
- o motivo da parada existia so no corpo do POST e no log da plataforma.

Ou seja: o painel afirmava competencia do agente sobre um run em que ele nao
decidiu nada, e nao havia como descobrir por que sem abrir o log de fora.

Os quatro cenarios da secao de baixo sao os quatro pedidos: proposta valida,
resposta incompativel com o schema, o modelo nao formular, e o provedor
indisponivel ate esgotar as tentativas. **Em nenhum deles pode existir
execucao atribuida ao agente sem decisao cognitiva registrada** - e e isso que
cada teste afirma, e nao apenas que o codigo nao explodiu.
"""

from __future__ import annotations

import json
import re
import sqlite3

import pytest

from app.cerebro import ciclo, paradas
from app.cerebro.provedores.base import Credenciais, ErroDoProvedor, Pedido, Resposta
from app.config.schema import ExperimentConfig
from app.ledger.livro import Uso, abrir_run
from app.maos_rapidas import executor
from app.validador import contador, lote
from tests.test_cerebro import (
    INTERPRETACAO_OK,
    PROPOSTA_OK,
    AdaptadorFalso,
    _rodar_ciclo,
    cenario,  # noqa: F401
    settings,  # noqa: F401
)


class ErroComStatus(Exception):
    """Imita a excecao de SDK: o que a classificacao le e `status_code`."""

    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.status_code = status


class AdaptadorQueContaChamadas:
    """Falha as N primeiras vezes e depois entrega o roteiro."""

    def __init__(self, falhas: list[BaseException], respostas: list[str]) -> None:
        self.provider = "anthropic"
        self.falhas = list(falhas)
        self.respostas = list(respostas)
        self.chamadas = 0
        self.uso = Uso(
            tokens_in=1_500, tokens_out=200, tokens_cache_read=0,
            tokens_cache_write=1_200, bruto={"falso": True},
        )

    def chamar(self, pedido: Pedido, *, credenciais: Credenciais) -> Resposta:
        self.chamadas += 1
        if self.falhas:
            raise self.falhas.pop(0)
        if not self.respostas:
            raise AssertionError("roteiro esgotado")
        return Resposta(texto=self.respostas.pop(0), uso=self.uso, bruto={})


def _sem_dormir(_segundos: float) -> None:
    """O retry nao dorme no teste. A espera e do provedor, nao da suite."""


def _transitorio() -> ErroDoProvedor:
    return ErroDoProvedor(
        "503 overloaded", categoria=paradas.ERRO_PROVEDOR, transitorio=True
    )


# ===========================================================================
# A LISTA FECHADA - duas copias, e uma so verdade
# ===========================================================================


def test_a_lista_de_categorias_e_a_mesma_no_python_e_no_banco() -> None:
    """Duas listas fechadas iguais em arquivos diferentes divergem.

    E o padrao que este projeto ja registrou dez vezes. Aqui a divergencia
    seria pior que cosmetica: uma categoria que o Python emite e o gatilho nao
    conhece derruba o run com `IntegrityError` no meio do ciclo, DEPOIS de o
    dinheiro da reflexao ja ter sido gasto.
    """
    from app import migrations

    sql = next(corpo for numero, _, corpo in migrations.MIGRACOES if numero == 13)
    trecho = sql.split("NOT IN (", 1)[1].split(")", 1)[0]
    no_banco = set(re.findall(r"'([a-z_]+)'", trecho))

    assert no_banco, "a extracao nao pode passar por vacuidade"
    assert no_banco == set(paradas.CATEGORIAS), (
        "a lista do gatilho e a de `paradas.CATEGORIAS` tem de ser identicas;"
        f" so no banco: {no_banco - set(paradas.CATEGORIAS)};"
        f" so no python: {set(paradas.CATEGORIAS) - no_banco}"
    )


def test_sem_hipotese_nao_e_uma_categoria_e_isso_e_deliberado() -> None:
    """A categoria obvia a acrescentar, e a que NAO entra.

    "O modelo nao conseguiu formular hipotese" e o terceiro dos quatro
    cenarios pedidos. Ele nao tem caminho hoje: `SCHEMA_PROPOSTA` exige uma
    `familia` do catalogo fechado, entao nao existe resposta valida que
    signifique "nao achei". Uma resposta assim viola o contrato e chega como
    `erro_schema` - que e o que o cenario 3 exercita mais abaixo.

    Declarar `sem_hipotese` na lista sem nada que a emita seria o defeito
    `BLOCOS` do incremento 6: uma constante declarada, nunca lida, sob um
    comentario afirmando que havia teste. Dar ao modelo como responder "nao
    achei" e mudanca no contrato de saida, que faz parte do prefixo cacheado
    (D31), e nao entra de carona neste incremento.
    """
    assert "sem_hipotese" not in paradas.CATEGORIAS


def test_o_banco_recusa_parada_sem_categoria_ou_sem_motivo(
    conn: sqlite3.Connection,
) -> None:
    """SQL cru, como as partidas dobradas: a regra e do BANCO.

    `NOT NULL` nao bastaria para o motivo - a string vazia e a de espacos
    passam por ele sem dizer nada -, e por isso o gatilho usa `TRIM(...) = ''`.
    Os tres casos sao tentados aqui.
    """
    run_id, _ = abrir_run(
        conn, config_version_id=1, seed_capital_usd_cents=100_000
    )

    def inserir(categoria, motivo):
        conn.execute(
            "INSERT INTO agent_event (run_id, occurred_at, node, kind,"
            " stop_category, stop_reason) VALUES (?, '2026-01-01T00:00:00Z',"
            " 'propor_regra', 'parada', ?, ?)",
            (run_id, categoria, motivo),
        )

    for categoria, motivo in [
        (None, "algum motivo"),
        (paradas.ERRO_PROVEDOR, None),
        (paradas.ERRO_PROVEDOR, ""),
        (paradas.ERRO_PROVEDOR, "   "),
    ]:
        with pytest.raises(sqlite3.IntegrityError, match="parada sem categoria"):
            inserir(categoria, motivo)

    with pytest.raises(sqlite3.IntegrityError, match="fora da lista fechada"):
        inserir("motivo_inventado", "qualquer coisa")

    # E o caminho felizardo funciona - senao os casos acima poderiam estar
    # passando porque o proprio `INSERT` esta quebrado por outro motivo.
    inserir(paradas.ERRO_PROVEDOR, "o provedor caiu")


def test_categoria_e_motivo_nao_entram_em_evento_de_outro_tipo(
    conn: sqlite3.Connection,
) -> None:
    """A simetria da migracao 8: o campo pertence ao evento que ele descreve."""
    run_id, _ = abrir_run(
        conn, config_version_id=1, seed_capital_usd_cents=100_000
    )
    with pytest.raises(sqlite3.IntegrityError, match="pertencem ao evento de parada"):
        conn.execute(
            "INSERT INTO agent_event (run_id, occurred_at, node, kind,"
            " stop_category, stop_reason) VALUES (?, '2026-01-01T00:00:00Z',"
            " 'observar', 'observacao', ?, 'x')",
            (run_id, paradas.TETO_ATINGIDO),
        )


# ===========================================================================
# CLASSIFICACAO E RETRY
# ===========================================================================


@pytest.mark.parametrize(
    "status, categoria, transitorio",
    [
        (400, paradas.PEDIDO_RECUSADO, False),
        (422, paradas.PEDIDO_RECUSADO, False),
        (401, paradas.PROVEDOR_INDISPONIVEL, False),
        (403, paradas.PROVEDOR_INDISPONIVEL, False),
        (429, paradas.ERRO_PROVEDOR, True),
        (500, paradas.ERRO_PROVEDOR, True),
        (529, paradas.ERRO_PROVEDOR, True),
        (404, paradas.ERRO_PROVEDOR, False),
    ],
)
def test_a_classificacao_separa_o_que_vale_tentar_de_novo(
    status: int, categoria: str, transitorio: bool
) -> None:
    """Um 400 nao e transitorio, e essa e a linha que importa.

    Reenviar o mesmo corpo a um 400 devolve o mesmo 400: seria latencia pura.
    Um 529 - o "overloaded" da Anthropic - e o caso oposto.
    """
    assert paradas.classificar(ErroComStatus(status)) == (categoria, transitorio)


def test_timeout_sem_status_e_transitorio() -> None:
    class APITimeoutError(Exception):
        pass

    assert paradas.classificar(APITimeoutError("estourou")) == (
        paradas.ERRO_PROVEDOR,
        True,
    )


def test_erro_desconhecido_sem_status_nao_e_transitorio() -> None:
    """Nao saber o que aconteceu nao autoriza gastar de novo."""
    assert paradas.classificar(RuntimeError("?")) == (paradas.ERRO_PROVEDOR, False)


def test_o_retry_repete_o_transitorio_e_o_run_completa(
    conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """Duas falhas transitorias e depois sucesso: o run termina com proposta."""
    adaptador = AdaptadorQueContaChamadas(
        [_transitorio(), _transitorio()], [INTERPRETACAO_OK, PROPOSTA_OK]
    )
    dataset_id, cfg = cenario
    resultado = ciclo.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        settings=settings, adaptador=adaptador, dormir=_sem_dormir,
    )
    assert adaptador.chamadas == 4  # 2 falhas + interpretar + propor
    assert resultado.parou_em is None
    assert resultado.atribuicao["atribuivel_ao_agente"] is True


def test_o_retry_nao_repete_o_que_nao_e_transitorio(
    conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """Um 400 chama UMA vez. O mesmo corpo produziria o mesmo 400."""
    adaptador = AdaptadorQueContaChamadas(
        [ErroDoProvedor("400", categoria=paradas.PEDIDO_RECUSADO, transitorio=False)],
        [],
    )
    dataset_id, cfg = cenario
    resultado = ciclo.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        settings=settings, adaptador=adaptador, dormir=_sem_dormir,
    )
    assert adaptador.chamadas == 1
    assert resultado.categoria_da_parada == paradas.PEDIDO_RECUSADO


# ===========================================================================
# OS QUATRO CENARIOS
# ===========================================================================


def test_cenario_1_proposta_valida_e_atribuivel_ao_agente(
    conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    assert resultado.parou_em is None
    assert resultado.regra_veio_do_cerebro is True
    assert resultado.atribuicao["atribuivel_ao_agente"] is True
    assert resultado.rule_id is not None and resultado.regra_hash
    assert resultado.execucao["ordens_executadas"] > 0

    # O vinculo que a atribuicao afirma: TODA ordem do run aponta para a regra
    # que a decisao registrou. Sem isto, "atribuivel" seria so um booleano.
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM execution WHERE run_id = ? AND rule_id = ?",
            (resultado.run_id, resultado.rule_id),
        ).fetchone()["n"]
        == resultado.execucao["ordens_executadas"]
    )
    assert resultado.proposal_id is not None


def test_cenario_2_resposta_fora_do_schema_nao_executa_nada(
    conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """A rejeicao fica REGISTRADA e nenhuma ordem e atribuida ao agente.

    A secao 8.6 exige que a tentativa fracassada permaneca no registro -
    "descartar tentativas fracassadas e o mecanismo exato que produz falsas
    descobertas". O que a D35 tira e a EXECUCAO, nao o registro.
    """
    adaptador = AdaptadorFalso([INTERPRETACAO_OK, '{"familia": "nao_existe"}'])
    resultado = _rodar_ciclo(conn, cenario, settings, adaptador)

    assert resultado.parou_em == "propor_regra"
    assert resultado.categoria_da_parada == paradas.ERRO_SCHEMA
    assert resultado.execucao["executou"] is False
    assert resultado.execucao["ordens_executadas"] == 0
    assert resultado.atribuicao["atribuivel_ao_agente"] is False
    assert resultado.rule_id is None

    rejeitada = conn.execute(
        "SELECT status, raw_response_json FROM rule_proposal WHERE run_id = ?",
        (resultado.run_id,),
    ).fetchone()
    assert rejeitada["status"] == "rejeitada"
    assert "nao_existe" in rejeitada["raw_response_json"]

    # Do BANCO, e nao do objeto de retorno.
    assert executor.ordens_executadas(conn, resultado.run_id) == 0


def test_cenario_3_o_modelo_nao_formula_cai_como_erro_de_schema(
    conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """O mais perto de "nao achei hipotese" que o contrato de hoje permite.

    Uma resposta bem formada em JSON e incoerente com a familia declarada -
    aqui `cruzamento_medias` sem `rapida` e `lenta`, com o `periodo` de outra
    familia. E o unico jeito de o modelo comunicar "nao tenho proposta" contra
    um schema que sempre pede uma.
    """
    incoerente = json.dumps(
        {
            "familia": "cruzamento_medias",
            "periodo": 20,
            "position_fraction_bps": 3_000,
            "pre_registro": json.loads(PROPOSTA_OK)["pre_registro"],
            "confianca_ppm": 500_000,
        }
    )
    resultado = _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, incoerente])
    )
    assert resultado.categoria_da_parada == paradas.ERRO_SCHEMA
    assert resultado.execucao["ordens_executadas"] == 0
    assert resultado.atribuicao["atribuivel_ao_agente"] is False


def test_cenario_4_provedor_indisponivel_ate_esgotar_os_retries(
    conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """Tres tentativas, nenhuma ordem, e o motivo NO EVENTO."""
    adaptador = AdaptadorQueContaChamadas([_transitorio() for _ in range(9)], [])
    dataset_id, cfg = cenario
    resultado = ciclo.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        settings=settings, adaptador=adaptador, dormir=_sem_dormir,
    )
    # Para no PRIMEIRO no que chama modelo, entao sao as 3 tentativas dele.
    assert adaptador.chamadas == 3
    assert resultado.parou_em == "interpretar"
    assert resultado.categoria_da_parada == paradas.ERRO_PROVEDOR
    assert resultado.execucao["ordens_executadas"] == 0
    assert resultado.atribuicao["atribuivel_ao_agente"] is False

    parada = ciclo.parada_do_run(conn, resultado.run_id)
    assert parada["categoria"] == paradas.ERRO_PROVEDOR
    assert parada["registro_completo"] is True
    assert "overloaded" in parada["motivo"]


# ===========================================================================
# O TETO E O CASO OPOSTO - a secao 3.6, regra 2
# ===========================================================================


def test_teto_atingido_executa_a_regra_padrao_mas_nao_e_do_agente(
    conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """A unica parada em que as maos rapidas seguem - e ainda assim nao e dele.

    Secao 3.6, regra 2: "Ao atingir o teto, ele continua operando com as maos
    rapidas, mas para de raciocinar ate o proximo ciclo." Parar as maos aqui
    contrariaria a especificacao. Chamar o resultado de "do agente" tambem:
    com o teto em zero o agente E o B3 (D23).
    """
    dataset_id, _ = cenario
    cfg = ExperimentConfig(max_llm_calls_per_run=0)
    resultado = ciclo.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        settings=settings, adaptador=AdaptadorFalso([]),
    )
    assert resultado.categoria_da_parada == paradas.TETO_ATINGIDO
    assert resultado.execucao["ordens_executadas"] > 0, "secao 3.6 regra 2"
    assert resultado.atribuicao["atribuivel_ao_agente"] is False
    assert "3.6" in resultado.atribuicao["por_que"]

    # A assimetria, dita de frente: uma categoria executa, a outra nao.
    assert paradas.executa_regra_padrao(paradas.TETO_ATINGIDO) is True
    assert paradas.executa_regra_padrao(paradas.ERRO_PROVEDOR) is False
    assert paradas.executa_regra_padrao(None) is True


# ===========================================================================
# O QUE O PAINEL LE
# ===========================================================================


def test_o_get_diz_por_que_parou_e_de_quem_e_o_resultado(
    client, conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """Era esta a metade que faltava: o POST sabia, o GET nao."""
    resultado = _rodar_ciclo(
        conn, cenario, settings,
        AdaptadorFalso([INTERPRETACAO_OK, '{"familia": "nao_existe"}']),
    )
    corpo = client.get("/api/agente").json()

    assert corpo["run_id"] == resultado.run_id
    assert corpo["parada"]["categoria"] == paradas.ERRO_SCHEMA
    assert corpo["parada"]["motivo"]
    assert corpo["parada"]["node"] == "propor_regra"
    assert corpo["atribuicao"]["atribuivel_ao_agente"] is False
    assert corpo["atribuicao"]["o_que_executou"] == "nada"

    # E o caminho tambem carrega a causa, evento por evento.
    parada = [e for e in corpo["caminho"] if e["kind"] == "parada"][-1]
    assert parada["stop_category"] == paradas.ERRO_SCHEMA
    assert parada["stop_reason"]


def test_a_faixa_nao_e_afirmada_quando_o_resultado_nao_e_do_agente(
    client, conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """`faixa` e uma afirmacao sobre a competencia do agente.

    Com o teto em zero o run EXECUTA e tem patrimonio - o que torna facil
    publicar uma faixa. Sobre um run em que o cerebro nao falou, ela seria
    uma afirmacao que ninguem pode fazer.
    """
    from app.maos_rapidas import baselines

    dataset_id, cfg_base = cenario
    cfg = ExperimentConfig(max_llm_calls_per_run=0)
    ciclo.rodar(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        settings=settings, adaptador=AdaptadorFalso([]),
    )
    baselines.rodar_comparacao(
        conn, dataset_id=dataset_id, config=cfg_base, config_version_id=1,
        semente=cfg_base.default_seed,
    )
    corpo = client.get("/api/agente").json()

    assert corpo["atribuicao"]["atribuivel_ao_agente"] is False
    assert corpo["faixa"] is None, (
        "com o teto em zero quem operou foi a regra padrao; publicar a faixa"
        " atribuiria ao agente o desempenho do B3"
    )
    # O patrimonio CONTINUA: e fato do ledger, e nao afirmacao sobre
    # competencia. Some-lo seria perder dado em vez de parar de mentir.
    assert corpo["patrimonio_final_cents"] > 0


def test_run_sem_proposta_fica_fora_do_lote_e_do_contador(
    conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """Sem hipotese nao ha membro do lote nem tentativa no DSR.

    Nao e escolha do lote: e consequencia de a hipotese so nascer depois de uma
    proposta aceita. O teste existe para provar que a consequencia vale, e nao
    para supo-la - um run que executou zero ordens ainda cria run, creditos e
    eventos, e qualquer um dos tres poderia ter virado tentativa.
    """
    antes = contador.total(conn)
    resultado = _rodar_ciclo(
        conn, cenario, settings,
        AdaptadorFalso([INTERPRETACAO_OK, '{"familia": "nao_existe"}']),
    )
    assert contador.total(conn) == antes
    assert resultado.hypothesis_id is None
    assert resultado.parecer_do_validador is None
    assert lote.membros(conn, 1) == []


# ===========================================================================
# O CONTRATO DE SAIDA - o que a suite pode conferir sem gastar dinheiro
# ===========================================================================


def test_o_schema_de_saida_obedece_o_modo_estrito_dos_dois_provedores() -> None:
    """A guarda que faltava, e ela nao custa uma chamada.

    O `SCHEMA_PROPOSTA` mudou no incremento 8 - a expectativa virou o
    pre-registro da secao 8.2, com objeto aninhado e array de objetos - e
    **nunca foi enviado a um provedor real** antes do run 27: os testes de
    rede sao pulados por padrao, e as ultimas chamadas de verdade foram do
    incremento 5, sob o schema antigo.

    O modo estrito dos dois provedores exige, em TODO objeto do schema:
    `additionalProperties: false` e `required` listando todas as
    propriedades. E a Anthropic recusa `name` no `format` com 400 - a
    descoberta 1 do incremento 5. Nada disso precisa de rede para ser
    conferido, e conferir aqui e a diferenca entre descobrir num teste e
    descobrir gastando.
    """
    from app.cerebro.contrato import SCHEMA_PROPOSTA

    problemas: list[str] = []

    def andar(no: object, caminho: str) -> None:
        if not isinstance(no, dict):
            return
        if no.get("type") == "object" or "properties" in no:
            props = set((no.get("properties") or {}).keys())
            if no.get("additionalProperties") is not False:
                problemas.append(f"{caminho}: sem additionalProperties=false")
            faltando = props - set(no.get("required") or [])
            if faltando:
                problemas.append(f"{caminho}: fora de required: {sorted(faltando)}")
        for chave, valor in (no.get("properties") or {}).items():
            andar(valor, f"{caminho}.{chave}")
        if "items" in no:
            andar(no["items"], f"{caminho}[]")

    andar(SCHEMA_PROPOSTA, "raiz")
    assert not problemas, problemas

    # O objeto aninhado e o array de objetos EXISTEM - senao as asercoes acima
    # passariam sobre um schema plano e nao provariam nada sobre o que mudou.
    pre = SCHEMA_PROPOSTA["properties"]["pre_registro"]
    assert pre["type"] == "object"
    assert pre["properties"]["condicoes_falseamento"]["type"] == "array"
    assert pre["properties"]["condicoes_falseamento"]["items"]["type"] == "object"

    # `name` e campo da OpenAI, e a Anthropic responde 400 com ele. O nome
    # segue existindo no `Pedido` - o outro adaptador precisa dele -, e o que
    # nao pode e vazar para dentro do schema.
    assert "name" not in SCHEMA_PROPOSTA


def test_ha_uma_politica_de_retry_so(  # noqa: D401
) -> None:
    """Duas politicas empilhadas davam 9 tentativas onde o teste afirma 3.

    Os dois SDKs tem retry proprio, e ele estava em 2. Somado ao nosso, o
    numero real de tentativas era o produto, e a metade de dentro nao logava,
    nao classificava e nao chegava ao evento de parada - invisivel de todo
    jeito que importa. Uma politica, num lugar, e a nossa fica porque e a que
    le `erro.transitorio` e alimenta `stop_category`.
    """
    from app.cerebro.provedores import anthropic_adaptador, openai_adaptador

    assert anthropic_adaptador.MAX_RETRIES == 0
    assert openai_adaptador.MAX_RETRIES == 0


def test_resposta_sem_texto_vira_categoria_max_tokens() -> None:
    """A resposta vazia nao pode chegar a validacao parecendo erro de JSON.

    E o caso que as notas de API registram: o pensamento adaptativo consome
    `max_tokens` antes de sair uma linha, e a mensagem resultante mandava
    procurar defeito no schema. A causa esta no `stop_reason`, e o teste
    exercita o caminho do ADAPTADOR - o unico lugar onde a excecao crua do
    SDK ainda existe - com uma resposta falsa em vez de uma chamada paga.
    """
    from app.cerebro.provedores.anthropic_adaptador import AdaptadorAnthropic

    class RespostaVazia:
        content: list = []
        stop_reason = "max_tokens"
        usage = None

    class ClienteFalso:
        class messages:  # noqa: N801
            @staticmethod
            def create(**_kwargs):
                return RespostaVazia()

    import anthropic

    original = anthropic.Anthropic
    anthropic.Anthropic = lambda **_k: ClienteFalso()
    try:
        with pytest.raises(ErroDoProvedor) as capturado:
            AdaptadorAnthropic().chamar(
                Pedido(
                    provider="anthropic", model="m", sistema="s",
                    mensagens=(("user", "u"),), schema={}, schema_nome="n",
                    max_tokens=10,
                ),
                credenciais=Credenciais(api_key="k"),
            )
    finally:
        anthropic.Anthropic = original

    assert capturado.value.categoria == paradas.MAX_TOKENS
    assert capturado.value.transitorio is False
    assert "max_tokens" in str(capturado.value)


def test_o_teto_de_saida_deixa_folga_larga_para_o_pensamento() -> None:
    """O texto do contrato cabe em ~300 tokens; o resto do teto e pensamento.

    Este teste NAO afirma que 16.000 basta - isso depende de quanto o modelo
    pensa, e nao e verificavel sem gastar. Ele fixa a unica relacao que e
    verificavel aqui: o teto tem de ficar muito acima do texto que o schema
    PEDE, para que "estourou o teto" signifique "pensou muito" e nunca "o
    contrato nao cabia". Crescer um campo de texto sem mexer no teto cai aqui,
    e nao numa chamada paga.
    """
    from app.cerebro import grafo
    from app.cerebro.contrato import SCHEMA_PROPOSTA

    pre = SCHEMA_PROPOSTA["properties"]["pre_registro"]["properties"]
    limites = [
        campo["maxLength"]
        for campo in pre.values()
        if isinstance(campo, dict) and "maxLength" in campo
    ]
    assert limites, "nenhum maxLength encontrado: a extracao ficou vazia"
    tokens_de_texto = sum(limites) // 4  # ~4 caracteres por token

    assert grafo.MAX_TOKENS_PROPOR >= 20 * tokens_de_texto, (
        f"o contrato pede ~{tokens_de_texto} tokens de texto e o teto e"
        f" {grafo.MAX_TOKENS_PROPOR}: a folga precisa ser larga o suficiente"
        " para que um estouro seja atribuivel ao pensamento, nunca ao texto"
    )


def test_o_contador_global_e_decomponivel_por_familia(
    conn: sqlite3.Connection, cenario, settings  # noqa: F811
) -> None:
    """Um total que ninguem consegue decompor nao e registro conferivel.

    Em producao o lote da `config_version` 5 apareceu com `membros: []` ao
    lado de `tentativas_globais: 1`, e nao havia como saber de qual familia
    era aquela tentativa. O total somar todas as familias e deliberado (secao
    8.6: o contador global e registro historico, e nao reseta); o que faltava
    era poder olhar de onde ele vem.
    """
    _rodar_ciclo(
        conn, cenario, settings, AdaptadorFalso([INTERPRETACAO_OK, PROPOSTA_OK])
    )
    quebra = contador.resumo(conn)["por_config_version"]
    assert quebra, "a quebra nao pode vir vazia com hipotese registrada"
    assert sum(l["tentativas"] for l in quebra) == contador.total(conn), (
        "a soma da quebra tem de fechar com o total que alimenta o DSR -"
        " senao a decomposicao descreveria outro numero"
    )
    assert all("config_version_id" in l for l in quebra)
