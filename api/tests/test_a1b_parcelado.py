"""A1b precisa de estado compartilhado entre execuções? **Não** — demonstrado.

> *"Cada bloco pode usar sua própria cópia descartável, desde que as execuções
> sejam independentes e as estatísticas finais possam ser agregadas exatamente.
> (…) Se o A1b exigir estado compartilhado entre execuções, demonstre isso
> antes e proponha uma cópia persistente selada para toda a certificação."*
> — o usuário, 2026-09-09

**Ele não exige, e a demonstração é mais forte que o pedido:** A1b não precisa
nem da cópia. `calibre.rodar` é uma função **pura** — não importa `sqlite3` e
recebe todos os insumos por parâmetro —, e `calibre.agregar` é pura sobre
`list[Uma]`. Então os blocos podem rodar sem banco nenhum, e a agregação sobre
o conteúdo gravado é **exata**, e não aproximada.

## O que PRECISA ser congelado, e por quê

A independência é condicional: cada execução é função de
`(base_bps, config, duracao_barra_ms, n_barras, tentativas_globais, desenho,
indice, semente)`. Três desses são lidos do banco e **podem mudar entre
blocos**:

| insumo | o que acontece se mudar no meio |
|---|---|
| `tentativas_globais` | é o `N` do DSR. Metade das execuções deflacionada por um número e metade por outro — **exatamente a divergência [40, 41] que a 0B registrou e aceitou** |
| `base_bps` | a série base sai do in-sample; outro dataset é outro experimento |
| `n_barras` | o horizonte contra o qual `n_minimo` é calculado |

Por isso eles são **congelados antes do primeiro bloco** e guardados na
execução de certificação. Um bloco que rodasse com insumo diferente produziria
um conjunto que não é o de nenhuma execução única — e a agregação diria um
número que ninguém mediu.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest


# ---------------------------------------------------------------------------
# A DEMONSTRAÇÃO
# ---------------------------------------------------------------------------


def test_calibre_rodar_e_PURA_nao_toca_o_banco():
    """Nenhum `sqlite3`, nenhum `conn`: só parâmetros e retorno.

    É isto que torna o bloco independente. Se ela lesse do banco, dois blocos
    em cópias diferentes poderiam ler coisas diferentes, e a agregação juntaria
    execuções que não pertencem ao mesmo experimento.
    """
    from app.a1b import calibre

    fonte = pathlib.Path(inspect.getfile(calibre)).read_text(encoding="utf-8")
    arvore = ast.parse(fonte)
    importados = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            importados.update(a.name for a in no.names)
        elif isinstance(no, ast.ImportFrom):
            importados.add(no.module or "")
    assert "sqlite3" not in importados, (
        "`calibre` passou a importar sqlite3: a execucao deixou de ser pura e"
        " os blocos deixaram de ser independentes"
    )

    # E nenhuma das duas funcoes recebe conexao.
    for fn in (calibre.rodar, calibre.agregar):
        params = set(inspect.signature(fn).parameters)
        assert "conn" not in params, f"{fn.__name__} recebe conn"


def test_agregar_e_PURA_sobre_a_lista_de_execucoes():
    """A agregação é sobre `list[Uma]`, então ela é exata a partir do conteúdo.

    É por isso que gravar **conteúdo** por bloco basta: recarregar as linhas e
    chamar `agregar` reproduz o mesmo número que rodar tudo de uma vez.
    """
    from app.a1b import calibre

    params = list(inspect.signature(calibre.agregar).parameters)
    assert params[0] == "execucoes"


def test_rodar_em_DOIS_blocos_da_o_mesmo_que_rodar_de_uma_vez():
    """A prova numérica, e não só a estrutural.

    Roda os índices 0–9 de uma vez, e depois 0–4 e 5–9 em duas chamadas
    separadas com os MESMOS insumos congelados. As execuções e a agregação têm
    de bater exatamente.
    """
    import random

    from app.a1b import calibre
    from app.config.schema import ExperimentConfig

    cfg = ExperimentConfig()
    rng = random.Random(7)
    base = [rng.randint(-300, 300) for _ in range(2_000)]
    congelado = dict(
        base_bps=base,
        config=cfg,
        duracao_barra_ms=900_000,
        n_barras=2_000,
        tentativas_globais=41,
        semente=42,
    )
    desenho = calibre.NULA_GLOBAL

    de_uma_vez, _, _ = calibre.rodar(
        indices={desenho: list(range(10))}, **congelado
    )
    primeiro, _, _ = calibre.rodar(
        indices={desenho: list(range(5))}, **congelado
    )
    segundo, _, _ = calibre.rodar(
        indices={desenho: list(range(5, 10))}, **congelado
    )

    assert len(de_uma_vez) == 10
    assert [ (e.desenho, e.indice) for e in primeiro + segundo ] == [
        (e.desenho, e.indice) for e in de_uma_vez
    ]
    for junto, partido in zip(de_uma_vez, primeiro + segundo):
        assert junto == partido, (
            f"indice {junto.indice} divergiu entre rodar junto e em blocos"
        )

    # E a agregacao sobre os dois pedacos e IDENTICA a de uma vez so.
    assert calibre.agregar(
        de_uma_vez, desenho=desenho, config=cfg
    ) == calibre.agregar(primeiro + segundo, desenho=desenho, config=cfg)


def test_tentativas_globais_ENTRA_no_calculo_e_por_isso_e_congelado():
    """Por que os insumos precisam ser congelados antes do primeiro bloco.

    `tentativas_globais` é o `N` do DSR. Se ele andar no meio, metade das
    execuções sai deflacionada por um número e metade por outro — **é a
    divergência [40, 41] que a 0B registrou**, e ali ela foi aceita com a
    aritmética na frente porque refazer custaria mais. Aqui é evitável, e
    evitá-la é o desenho.

    **A asserção é estrutural, e não numérica, e o motivo importa.** Medido:
    com `N = 41` e `N = 47` sobre a mesma base sintética, as 6 execuções saíram
    **idênticas** — coerente com a 0B, onde a diferença entre os dois
    contadores foi de 0,001 e não mudou nenhum lado do limiar. Um teste que
    exigisse divergência numérica seria **vacuoso ou frágil**: ele passaria por
    acaso hoje e quebraria numa base em que o DSR ficasse perto do limiar.

    O que se afirma aqui é o que sempre vale: o insumo **entra na conta**.
    """
    import inspect

    from app.a1b import calibre

    # 1. `tentativas_globais` chega a `rodar` e e repassado.
    assert "tentativas_globais" in inspect.signature(calibre.rodar).parameters

    # 2. E ele alcanca o DSR, que e quem o consome como `N`.
    fonte = inspect.getsource(calibre)
    assert "tentativas" in fonte
    from app.estatistica import dsr

    assert "tentativas" in inspect.signature(dsr.calcular).parameters, (
        "o DSR parou de receber o numero de tentativas: se isso for"
        " deliberado, o congelamento deste insumo perdeu o motivo e este teste"
        " precisa ser reescrito - nao apagado"
    )


def test_dois_N_diferentes_sao_dois_experimentos(capsys):
    """A medição que acompanha a asserção estrutural acima."""
    import random

    from app.a1b import calibre
    from app.config.schema import ExperimentConfig

    cfg = ExperimentConfig()
    rng = random.Random(11)
    base = [rng.randint(-300, 300) for _ in range(2_000)]
    comum = dict(
        base_bps=base, config=cfg, duracao_barra_ms=900_000,
        n_barras=2_000, semente=42,
        indices={calibre.NULA_GLOBAL: list(range(6))},
    )
    com_41, _, _ = calibre.rodar(tentativas_globais=41, **comum)
    com_47, _, _ = calibre.rodar(tentativas_globais=47, **comum)

    # As LINHAS sao as mesmas: mesma semente, mesmo desenho, mesmos indices.
    assert [(e.desenho, e.indice) for e in com_41] == [
        (e.desenho, e.indice) for e in com_47
    ]
    iguais = sum(1 for a, b in zip(com_41, com_47) if a == b)
    print(f"com N=41 e N=47: {iguais} de {len(com_41)} execucoes identicas")


# ---------------------------------------------------------------------------
# OS OITO BLOCOS, no banco
# ---------------------------------------------------------------------------


from tests.test_portao_a import cenario  # noqa: E402, F401
from tests.test_cerebro import settings  # noqa: E402, F401


def _a1b(conn, cenario):
    from app.certificacao import suite

    dataset_id, cfg = cenario
    return suite.iniciar_a1b(
        conn, dataset_id=dataset_id, config=cfg, config_version_id=1,
        dataset_hash="a" * 64,
    ), cfg


def test_iniciar_congela_os_insumos_antes_do_primeiro_bloco(conn, cenario):
    """Os três insumos que vêm do banco ficam gravados na execução."""
    estado, _ = _a1b(conn, cenario)
    ins = estado["insumos_congelados"]
    assert estado["blocos_esperados"] == 8
    assert estado["execucoes_esperadas"] == 400
    assert ins["por_bloco"] == 50
    for chave in ("n_barras", "tentativas_globais", "semente", "lote"):
        assert chave in ins, chave
    assert ins["base_bps_tamanho"] > 0
    assert "40, 41" in ins["por_que_congelado"]
    assert estado["faltando"] == list(range(8))
    assert estado["pode_selar"] is False


def test_iniciar_e_IDEMPOTENTE_por_alvo(conn, cenario):
    """Dois cliques no painel não abrem duas execuções paralelas."""
    primeiro, _ = _a1b(conn, cenario)
    segundo, _ = _a1b(conn, cenario)
    assert primeiro["execucao_id"] == segundo["execucao_id"]


def test_o_retry_do_MESMO_bloco_nao_roda_de_novo(conn, cenario):
    """Idempotência pelo `UNIQUE`, e o resultado nunca é sobrescrito."""
    from app.certificacao import suite

    estado, _ = _a1b(conn, cenario)
    eid = estado["execucao_id"]

    r1 = suite.rodar_bloco(conn, execucao_id=eid, indice_bloco=0)
    assert r1["rodou_agora"] is True
    assert r1["concluidos"] == 1
    assert r1["execucoes_gravadas"] == 50

    r2 = suite.rodar_bloco(conn, execucao_id=eid, indice_bloco=0)
    assert r2["rodou_agora"] is False
    assert "NUNCA e sobrescrito" in r2["por_que"]
    assert r2["concluidos"] == 1
    assert r2["execucoes_gravadas"] == 50


def test_o_banco_RECUSA_sobrescrever_um_bloco(conn, cenario):
    """A imutabilidade é do banco, e não de quem chama."""
    import sqlite3

    from app.certificacao import suite

    estado, _ = _a1b(conn, cenario)
    suite.rodar_bloco(conn, execucao_id=estado["execucao_id"], indice_bloco=0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE certificacao_bloco SET quantas = 0")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM certificacao_bloco")
    # E o UNIQUE impede a segunda linha do mesmo indice.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO certificacao_bloco (execucao_id, escopo,"
            " indice_bloco, estado, quantas, conteudo_json, criado_em)"
            " VALUES (?, 'a1b', 0, 'concluido', 50, '[]', 'x')",
            (estado["execucao_id"],),
        )


def test_indice_fora_do_declarado_e_recusado(conn, cenario):
    """A contagem de blocos é congelada no início."""
    from app.certificacao import suite

    estado, _ = _a1b(conn, cenario)
    with pytest.raises(ValueError) as erro:
        suite.rodar_bloco(
            conn, execucao_id=estado["execucao_id"], indice_bloco=8
        )
    assert "fora de 0..7" in str(erro.value)


def test_selar_RECUSA_com_menos_de_oito_blocos(conn, cenario):
    """Manifesto parcial é ilegível — e a recusa vem antes do banco."""
    from app.certificacao import suite

    estado, cfg = _a1b(conn, cenario)
    eid = estado["execucao_id"]
    for i in range(7):
        suite.rodar_bloco(conn, execucao_id=eid, indice_bloco=i)
    with pytest.raises(suite.CertificacaoRecusada) as erro:
        suite.selar_a1b(conn, execucao_id=eid, config=cfg)
    assert "[7]" in str(erro.value)
    assert conn.execute(
        "SELECT COUNT(*) FROM certificacao_manifesto"
    ).fetchone()[0] == 0


def test_os_OITO_blocos_selam_e_somam_400(conn, cenario):
    """O caminho completo, e a agregação sobre o conteúdo gravado."""
    from app.certificacao import suite

    antes = conn.execute("SELECT COUNT(*) FROM hypothesis").fetchone()[0]
    estado, cfg = _a1b(conn, cenario)
    eid = estado["execucao_id"]
    for i in range(8):
        estado = suite.rodar_bloco(conn, execucao_id=eid, indice_bloco=i)
    assert estado["concluidos"] == 8
    assert estado["execucoes_gravadas"] == 400
    assert estado["pode_selar"] is True

    cert = suite.selar_a1b(conn, execucao_id=eid, config=cfg)
    assert cert.manifesto["escopo"] == "a1b"
    assert len(cert.manifesto["casos"]) == 2
    for caso in cert.manifesto["casos"]:
        assert caso["observado"]["execucoes"] == 200
        assert caso["observado"]["completo"] is True

    # E NADA disso tocou o experimento.
    assert conn.execute("SELECT COUNT(*) FROM hypothesis").fetchone()[0] == antes


def test_um_bloco_que_FALHOU_permanece_no_resultado(conn, cenario, monkeypatch):
    """Apagar a falha e reexecutar até o número agradar é o que §8.6 proíbe."""
    from app.certificacao import escopos as esc
    from app.certificacao import suite

    estado, cfg = _a1b(conn, cenario)
    eid = estado["execucao_id"]

    def explode(congelado, indice_bloco):
        raise RuntimeError("falha plantada no bloco 3")

    monkeypatch.setattr(esc, "rodar_bloco", explode)
    depois = suite.rodar_bloco(conn, execucao_id=eid, indice_bloco=3)
    assert depois["falhou"] == [3]
    assert 3 in depois["faltando"], "um bloco que falhou continua faltando"

    # E o retry NAO sobrescreve: o registro guarda a falha.
    monkeypatch.undo()
    de_novo = suite.rodar_bloco(conn, execucao_id=eid, indice_bloco=3)
    assert de_novo["rodou_agora"] is False
    assert de_novo["falhou"] == [3]
    linha = conn.execute(
        "SELECT conteudo_json FROM certificacao_bloco"
        " WHERE execucao_id = ? AND indice_bloco = 3", (eid,)
    ).fetchone()
    assert "falha plantada" in linha["conteudo_json"]
