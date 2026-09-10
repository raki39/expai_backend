"""Roda a suíte do Portão A na cópia e sela o manifesto. ADR 0040.

A ordem importa, e cada passo existe por um motivo medido:

1. **monta o alvo** — sete componentes, antes de rodar. Depois seria escolher o
   alvo olhando o resultado;
2. **fotografa o banco oficial** — as nove tabelas que a certificação não pode
   tocar;
3. **copia** — `backup()` consistente, ~4 ms/MB medido;
4. **roda a suíte na cópia** — o adaptador real, com gatilhos, créditos,
   transições e ledger. É a cópia que recebe as 6 hipóteses, os 6 runs e as
   2.195 linhas de ledger;
5. **captura o canônico** — pela `chave` do catálogo, nunca por id temporário:
   medido, dos 6 ids citados, **zero** existiam depois;
6. **descarta a cópia**;
7. **confere a fotografia** — qualquer diferença **recusa o certificado**;
8. **grava**, em transação separada no banco oficial.

## O que a certificação NÃO faz, e é estrutural

Nada aqui escreve em `hypothesis`, `test_credit_entry` ou `hypothesis_state` do
banco oficial — as escritas acontecem na cópia. Então **nenhuma consulta de DSR
ou FDR precisa filtrar certificações**: elas não estão na tabela que essas
consultas leem. A garantia não é disciplina de quem escreve o `SELECT`.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import alvo as alvo_mod
from . import laboratorio


#: O QUE este certificado cobre, e o que ele NAO cobre.
#:
#: Declarado, e nao implicito: um manifesto que dissesse "Portao A certificado"
#: cobrindo so uma das quatro familias de criterio seria a forma exata do
#: padrao que este projeto conta - um campo que descreve mais do que mediu.
ESCOPO = {
    "cobre": [
        "UM escopo por certificado. O Portao A e a COMPOSICAO dos cinco -"
        " a1a, a1b, a2, a3 e a4 - e so passa quando os cinco tiverem"
        " certificado para o MESMO alvo (`escopos.composicao`)",
        "a1a: as seis familias de defeito deterministico de §14.4, injetadas"
        " pelo caminho real numa copia descartavel SELADA, em oito etapas"
        " (OP-1): preparo de baselines, preparo de B4 e uma etapa por familia",
        "a1b: as 400 execucoes das nulas, em oito blocos de 50, sem copia -"
        " `calibre.rodar` e pura",
        "a2, a3 e a4: baselines, vazamento e reconciliacao do ledger",
    ],
    "NAO_cobre": [
        "o experimento: a certificacao nao registra hipotese, nao cobra"
        " credito, nao move o contador do DSR e nao toca o holdout do banco"
        " oficial",
    ],
    "por_que_isso_nao_e_um_manifesto_parcial": (
        "parcial seria um escopo incompleto - tres dos seis casos do a1a, sete"
        " das oito etapas -, e os gatilhos `manifesto_exige_suite_completa` e"
        " `manifesto_exige_todos_os_blocos` recusam isso no banco"
    ),
}


class CertificacaoRecusada(Exception):
    """A certificação rodou e o certificado NÃO pode existir.

    Dois casos, e os dois são graves de formas diferentes: o banco oficial
    mudou durante a execução (a cópia não foi onde a escrita aconteceu), ou um
    controle foi promovido (§14.4, tolerância zero).
    """


@dataclass(frozen=True)
class Certificado:
    execucao_id: int
    alvo_hash: str
    passa: bool
    manifesto: dict


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _caso_canonico(c) -> dict:
    """O resultado de um caso, sem nenhum id que não sobreviva à cópia."""
    return {
        "chave": c.chave,
        "familia_de_defeito": c.familia_de_defeito,
        "tipo": c.tipo,
        "barrado": c.barrado,
        "veredito": c.veredito,
        "motivo": c.motivo,
        "promovido": c.promovido,
        "creditos_cobrados": c.creditos_cobrados,
        "estado_final": c.estado_final,
        "tentativas": [
            {
                "o_que": t.get("o_que"),
                "barrada": t.get("barrada"),
                "mecanismo": t.get("mecanismo"),
            }
            for t in c.tentativas
        ],
        "observado": c.observado,
    }


def _preparar_baselines(
    copia, *, dataset_id: int, config, config_version_id: int
) -> dict:
    """B2, B3 e B1 sob esta config, NA CÓPIA — se ainda não houver B3.

    Uma das duas unidades do preparo. Separada para que a certificação
    parcelada a rode como etapa própria (OP-1), sem uma segunda definição.
    """
    from ..maos_rapidas import baselines

    if copia.execute(
        "SELECT 1 FROM run WHERE agent_id = 'baseline-B3'"
        " AND config_version_id = ?",
        (config_version_id,),
    ).fetchone():
        return {"baselines": "nao foi preciso: ja havia B3 sob esta config"}
    _t0 = time.perf_counter_ns()
    baselines.rodar_comparacao(
        copia,
        dataset_id=dataset_id,
        config=config,
        config_version_id=config_version_id,
        semente=config.default_seed,
    )
    return {
        "micros_por_etapa": {
            "baselines": (time.perf_counter_ns() - _t0) // 1_000
        },
        "baselines": "rodados na copia (nao havia B3 sob esta config)",
    }


def _preparar_b4(
    copia, *, dataset_id: int, config, config_version_id: int
) -> dict:
    """O braço B4 NA CÓPIA — se ainda não houver hipótese sob esta config.

    O controle de **duplicação disfarçada** precisa de uma hipótese real
    anterior para duplicar; sem ela, a família constaria como injetada sem ter
    injetado nada.
    """
    from ..b4 import braco as b4_braco

    if copia.execute(
        "SELECT 1 FROM hypothesis h JOIN run r ON r.id = h.run_id"
        " WHERE r.config_version_id = ?",
        (config_version_id,),
    ).fetchone():
        return {"b4": "nao foi preciso: ja havia hipotese sob esta config"}
    _t0 = time.perf_counter_ns()
    b4_braco.rodar(
        copia,
        dataset_id=dataset_id,
        config=config,
        config_version_id=config_version_id,
    )
    return {
        "micros_por_etapa": {"b4": (time.perf_counter_ns() - _t0) // 1_000},
        "b4": (
            "rodado na copia: o controle de DUPLICACAO precisa de uma hipotese"
            " real anterior para duplicar, e sem ela a familia constaria como"
            " injetada sem ter injetado nada"
        ),
    }


def _preparar_laboratorio(
    copia, *, dataset_id: int, config, config_version_id: int
) -> dict:
    """As pré-condições da suíte, estabelecidas NA CÓPIA.

    ## Por que isto existe, e é o desenho e não um remendo

    `a1a.braco.rodar` recusa sem um B3 sob a mesma `config_version` — a métrica
    dos controles estatísticos é `excesso_sobre_b3_cents`, e sem o baseline ela
    não tem contra o que ser medida. E o controle de **duplicação disfarçada**
    precisa de uma hipótese real anterior para duplicar; sem ela, ele registra a
    linha e marca `nao_injetado`, e a família passa a constar como injetada sem
    ter injetado nada.

    Medido em produção em 2026-09-09: certificar a `config_version` 9 devolveu
    **HTTP 500** por exatamente isso — ela é a config vigente e não tem run
    nenhum apontando para ela.

    **A resposta certa não é exigir que alguém rode baselines em produção antes
    de certificar.** Isso criaria runs reais só para permitir uma certificação,
    que é o oposto do objeto. A cópia é descartável: a suíte estabelece as
    próprias pré-condições dentro dela, e tudo vai embora junto.

    E isso torna o certificado **mais** forte, não menos: ele passa a exercitar
    também o caminho dos baselines e o de B4, e deixa de depender do que por
    acaso estava no banco.
    """
    # As MARCAS de tempo por unidade. Sem elas, "207 segundos" e um numero
    # sem endereco - e foi delas que o plano do A1a parcelado saiu (OP-1).
    # A ordem importa: B4 exige o B3 que a primeira unidade estabelece.
    feito: dict = {"micros_por_etapa": {}}
    for unidade in (_preparar_baselines, _preparar_b4):
        parte = unidade(
            copia,
            dataset_id=dataset_id,
            config=config,
            config_version_id=config_version_id,
        )
        feito["micros_por_etapa"].update(parte.pop("micros_por_etapa", {}))
        feito.update(parte)
    return feito


def _selar(
    oficial: sqlite3.Connection,
    *,
    escopo: str,
    resultado,
    o_alvo: dict,
    build_do_backend: str | None,
    congelado: dict | None = None,
    execucao_id: int | None = None,
) -> Certificado:
    """Confere, monta o manifesto e grava. Comum aos cinco escopos.

    `execucao_id` vem preenchido no A1b, cuja execução nasceu antes do primeiro
    bloco; nos outros ela nasce aqui.
    """
    from ..store import bloco_atomico
    from . import escopos as esc

    from . import canonico

    esperados = esc.CASOS_ESPERADOS[escopo]
    # CONTEUDO, e nunca id de linha que so existiu na copia. Medido nos
    # certificados SELADOS antes da janela: o a1a citava a hipotese 57 e o a2
    # os runs 82 e 99 - linhas da copia. Ver `canonico.py`.
    casos, ids_dos_casos = canonico.sem_ids_temporarios(resultado.casos)
    preparo, ids_do_preparo = canonico.sem_ids_temporarios(
        resultado.preparo
        or {"nada": "este escopo nao precisou preparar laboratorio"}
    )

    diferencas = laboratorio.conferir_intocado(
        resultado.intocado_antes, resultado.intocado_depois
    )
    if diferencas:
        raise CertificacaoRecusada(
            "o banco OFICIAL mudou durante a certificacao, entao a escrita nao"
            " ficou contida e o certificado nao pode ser selado:"
            f" {'; '.join(diferencas)}"
        )
    promovidos = [c["chave"] for c in casos if c.get("promovido")]
    if promovidos:
        raise CertificacaoRecusada(
            "um controle deterministico foi PROMOVIDO na certificacao:"
            f" {', '.join(promovidos)}. §14.4 e literal - uma unica promocao"
            " reprova a fase, e isso e a prova de um defeito no pipeline, nao"
            " um resultado a registrar"
        )
    if len(casos) != esperados:
        raise CertificacaoRecusada(
            f"o escopo {escopo} produziu {len(casos)} casos e esperava"
            f" {esperados}: manifesto parcial e ilegivel"
        )

    # `None` NAO e `False`, e nao pode virar certificado nem reprovacao.
    #
    # Um criterio que ninguem mediu nao e um criterio satisfeito - e tambem nao
    # e um criterio violado. E a licao do Portao A, que tem TRES resultados de
    # proposito. Medido: `b1_proporcional_ao_giro` sai `None` quando ha um giro
    # so, porque um ponto nao tem inclinacao, e a primeira versao disto selou
    # um manifesto com `passa=0` - transformando "nao medi" em "falhou".
    nao_medidos = [c["chave"] for c in casos if c.get("contido") is None]
    if nao_medidos:
        raise CertificacaoRecusada(
            f"o escopo {escopo} tem criterio(s) NAO MEDIDO(S):"
            f" {', '.join(nao_medidos)}. Um certificado sobre criterio nao"
            " medido afirmaria o que ninguem observou - e `None` nao e"
            " `False`, entao isto tambem nao e uma reprovacao"
        )
    nao_contidos = [c["chave"] for c in casos if c.get("contido") is False]
    passa = not nao_contidos

    manifesto = {
        "escopo": escopo,
        "pergunta": esc.PERGUNTA[escopo],
        "alvo_de_certificacao_hash": o_alvo["alvo_de_certificacao_hash"],
        "componentes": o_alvo["componentes"],
        "o_que_NAO_entra_no_hash": o_alvo["o_que_NAO_entra_no_hash"],
        "build_do_backend": build_do_backend,
        "suite": {
            "hash": o_alvo["componentes"]["suite_de_controles"]["hash"],
            "casos_esperados": esperados,
            "casos_gravados": len(casos),
            "nao_contidos": nao_contidos,
            "blocos": esc.BLOCOS.get(escopo, 0),
        },
        "casos": casos,
        "escopo_declarado": ESCOPO,
        "insumos_congelados": congelado,
        "preparo_do_laboratorio": preparo,
        "ids_temporarios_removidos": {
            "caminhos": ids_dos_casos + ids_do_preparo,
            "por_que": (
                "id de linha criada na COPIA nao existe depois do descarte: o"
                " certificado cita o CONTEUDO que o id apontava, que ja esta"
                " ao lado (content_hash, giro, percentis). Publicado, e nao"
                " apagado calado"
            ),
        },
        "passa": passa,
        "o_que_passa_significa": (
            f"o escopo {escopo} esta certificado para este alvo. **NAO** diz"
            " que o Portao A passou: ele e a composicao dos cinco escopos, e"
            " so passa quando os cinco tiverem certificado para o MESMO alvo"
        ),
        "banco_oficial_intocado": {
            "antes": resultado.intocado_antes,
            "depois": resultado.intocado_depois,
            "diferencas": [],
        },
        "custo_operacional": {
            "micros_para_copiar": resultado.micros_para_copiar,
            "micros_da_suite": resultado.micros_da_suite,
            "bytes_da_copia": resultado.bytes_da_copia,
            "o_que_isso_NAO_e": (
                "credito experimental. Os creditos de §8.6.1 medem escassez de"
                " DADO - holdout e reserva -, e isto e CPU e disco. Somar os"
                " dois faria uma recertificacao consumir orcamento de"
                " descoberta"
            ),
        },
        "o_que_a_certificacao_NAO_toca": (
            "hypothesis, tentativas, creditos, familia, BY, DSR e holdout."
            " Nenhuma consulta precisa filtrar certificacoes: elas nao estao na"
            " tabela que o DSR e o FDR leem"
        ),
        "selado_em": _agora(),
    }

    with bloco_atomico(oficial, "certificacao_selar"):
        if execucao_id is None:
            cur = oficial.execute(
                "INSERT INTO certificacao_execucao (alvo_hash,"
                " componentes_json, build_do_backend, iniciada_em,"
                " casos_esperados, micros_para_copiar, micros_da_suite,"
                " bytes_da_copia, intocado_json, escopo, congelado_json,"
                " blocos_esperados) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    o_alvo["alvo_de_certificacao_hash"],
                    json.dumps(o_alvo["componentes"], ensure_ascii=False),
                    build_do_backend, _agora(), esperados,
                    resultado.micros_para_copiar, resultado.micros_da_suite,
                    resultado.bytes_da_copia,
                    json.dumps(
                        {"antes": resultado.intocado_antes,
                         "depois": resultado.intocado_depois},
                        ensure_ascii=False,
                    ),
                    escopo,
                    json.dumps(congelado, ensure_ascii=False) if congelado else None,
                    esc.BLOCOS.get(escopo, 0),
                ),
            )
            execucao_id = int(cur.lastrowid)
        for caso in casos:
            oficial.execute(
                "INSERT INTO certificacao_caso (execucao_id, chave, familia,"
                " tipo, barrado, promovido, resultado_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    execucao_id, caso["chave"],
                    caso.get("familia_de_defeito") or "?",
                    caso.get("tipo") or "estrutural",
                    1 if caso.get("barrado") else 0,
                    1 if caso.get("promovido") else 0,
                    json.dumps(caso, ensure_ascii=False),
                ),
            )
        oficial.execute(
            "INSERT INTO certificacao_manifesto (execucao_id, alvo_hash,"
            " manifesto_json, selado_em, passa) VALUES (?, ?, ?, ?, ?)",
            (
                execucao_id, o_alvo["alvo_de_certificacao_hash"],
                json.dumps(manifesto, ensure_ascii=False),
                manifesto["selado_em"], 1 if passa else 0,
            ),
        )

    return Certificado(
        execucao_id=execucao_id,
        alvo_hash=o_alvo["alvo_de_certificacao_hash"],
        passa=passa,
        manifesto=manifesto,
    )


def executar(
    oficial: sqlite3.Connection,
    *,
    escopo: str,
    dataset_id: int,
    config,
    config_version_id: int,
    dataset_hash: str | None = None,
    snapshot_hash: str | None = None,
    build_do_backend: str | None = None,
) -> Certificado:
    """Um escopo inteiro numa chamada: a2, a3, a4 — e o a1a pelo parcelado.

    O a1a entra aqui pelo MESMO caminho de etapas de `parcelado` (as oito, em
    sequencia no mesmo processo). A rota HTTP nao oferece isso: pela API o
    a1a e sempre uma etapa por requisicao (OP-1).

    A1b é parcelado e tem caminho próprio — `iniciar_a1b`, `rodar_bloco` e
    `selar_a1b` —, porque 400 execuções não cabem numa requisição e a regra 1
    proíbe worker na Fase 0.
    """
    from . import escopos as esc
    from . import por_escopo

    if escopo == esc.A1B:
        raise ValueError(
            "a1b e parcelado: use `iniciar_a1b`, `rodar_bloco` e `selar_a1b`."
            " Uma requisicao unica de 400 execucoes e o que o ADR 0018 chama"
            " de aposta no timeout"
        )
    if escopo == esc.A1A:
        # O MESMO caminho parcelado - etapas, copia selada, impressoes - so que
        # chamado em sequencia no mesmo processo. Nao existe um segundo
        # caminho monolitico para o a1a: ele divergiria do parcelado no
        # primeiro campo novo.
        from . import parcelado

        return parcelado.de_uma_vez(
            oficial,
            dataset_id=dataset_id,
            config=config,
            config_version_id=config_version_id,
            dataset_hash=dataset_hash,
            snapshot_hash=snapshot_hash,
            build_do_backend=build_do_backend,
        )
    if escopo not in por_escopo.DE_UMA_VEZ:
        raise ValueError(f"escopo desconhecido: {escopo!r}")

    o_alvo = alvo_mod.montar(
        oficial, dataset_hash=dataset_hash, snapshot_hash=snapshot_hash
    )
    resultado = por_escopo.DE_UMA_VEZ[escopo](
        oficial, dataset_id=dataset_id, config=config,
        config_version_id=config_version_id,
    )
    return _selar(
        oficial, escopo=escopo, resultado=resultado, o_alvo=o_alvo,
        build_do_backend=build_do_backend,
    )


# ---------------------------------------------------------------------------
# A1b: oito blocos, sem worker
# ---------------------------------------------------------------------------


def iniciar_a1b(
    oficial: sqlite3.Connection,
    *,
    dataset_id: int,
    config,
    config_version_id: int,
    dataset_hash: str | None = None,
    snapshot_hash: str | None = None,
    build_do_backend: str | None = None,
) -> dict:
    """Congela os insumos e abre a execução. **Antes do primeiro bloco.**

    Idempotente por alvo: se já houver uma execução A1b aberta e não selada
    para o mesmo alvo, ela é devolvida em vez de outra ser criada — senão um
    clique repetido no painel abriria execuções paralelas e os blocos se
    espalhariam entre elas.
    """
    from . import escopos as esc

    o_alvo = alvo_mod.montar(
        oficial, dataset_hash=dataset_hash, snapshot_hash=snapshot_hash
    )
    alvo_hash = o_alvo["alvo_de_certificacao_hash"]

    aberta = oficial.execute(
        "SELECT e.id FROM certificacao_execucao e"
        " LEFT JOIN certificacao_manifesto m ON m.execucao_id = e.id"
        " WHERE e.escopo = ? AND e.alvo_hash = ? AND m.execucao_id IS NULL"
        " ORDER BY e.id DESC LIMIT 1",
        (esc.A1B, alvo_hash),
    ).fetchone()
    if aberta is not None:
        return estado_a1b(oficial, int(aberta["id"]))

    congelado = esc.congelar_insumos(
        oficial, dataset_id=dataset_id, config=config,
        config_version_id=config_version_id,
    )
    from ..store import bloco_atomico

    with bloco_atomico(oficial, "a1b_iniciar"):
        cur = oficial.execute(
            "INSERT INTO certificacao_execucao (alvo_hash, componentes_json,"
            " build_do_backend, iniciada_em, casos_esperados, intocado_json,"
            " escopo, congelado_json, blocos_esperados)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                alvo_hash,
                json.dumps(o_alvo["componentes"], ensure_ascii=False),
                build_do_backend, _agora(),
                esc.CASOS_ESPERADOS[esc.A1B],
                json.dumps(laboratorio.fotografia(oficial), ensure_ascii=False),
                esc.A1B,
                json.dumps(congelado, ensure_ascii=False),
                esc.BLOCOS[esc.A1B],
            ),
        )
    return estado_a1b(oficial, int(cur.lastrowid))


def estado_a1b(oficial: sqlite3.Connection, execucao_id: int) -> dict:
    """O progresso: quais blocos existem, quais faltam, e se já selou."""
    from . import escopos as esc

    linha = oficial.execute(
        "SELECT alvo_hash, congelado_json, blocos_esperados, iniciada_em"
        " FROM certificacao_execucao WHERE id = ?",
        (execucao_id,),
    ).fetchone()
    if linha is None:
        raise ValueError(f"execucao {execucao_id} nao existe")
    blocos = [
        {
            "indice_bloco": int(l["indice_bloco"]),
            "estado": l["estado"],
            "quantas": int(l["quantas"]),
            "micros": int(l["micros"]),
            "criado_em": l["criado_em"],
        }
        for l in oficial.execute(
            "SELECT indice_bloco, estado, quantas, micros, criado_em"
            " FROM certificacao_bloco WHERE execucao_id = ?"
            " ORDER BY indice_bloco",
            (execucao_id,),
        )
    ]
    faltando = esc.blocos_faltando(oficial, execucao_id)
    selado = oficial.execute(
        "SELECT passa, selado_em FROM certificacao_manifesto"
        " WHERE execucao_id = ?", (execucao_id,)
    ).fetchone()
    congelado = json.loads(linha["congelado_json"] or "{}")
    return {
        "execucao_id": execucao_id,
        "escopo": esc.A1B,
        "alvo_de_certificacao_hash": linha["alvo_hash"],
        "iniciada_em": linha["iniciada_em"],
        "blocos_esperados": int(linha["blocos_esperados"]),
        "blocos": blocos,
        "faltando": faltando,
        "concluidos": sum(1 for b in blocos if b["estado"] == "concluido"),
        "falhou": [b["indice_bloco"] for b in blocos if b["estado"] == "falhou"],
        "execucoes_gravadas": sum(
            b["quantas"] for b in blocos if b["estado"] == "concluido"
        ),
        "execucoes_esperadas": esc.BLOCOS[esc.A1B] * esc.POR_BLOCO,
        "pode_selar": not faltando,
        "selado": (
            {"passa": bool(selado["passa"]), "selado_em": selado["selado_em"]}
            if selado
            else None
        ),
        "insumos_congelados": {
            k: v for k, v in congelado.items() if k != "base_bps"
        }
        | {"base_bps_tamanho": len(congelado.get("base_bps") or [])},
        "proximo": faltando[0] if faltando else None,
    }


def rodar_bloco(
    oficial: sqlite3.Connection, *, execucao_id: int, indice_bloco: int
) -> dict:
    """Roda UM bloco de 50 e grava o conteúdo. Idempotente.

    Idempotente pelo `UNIQUE (execucao_id, escopo, indice_bloco)`: pedir o
    mesmo bloco duas vezes devolve o que já existe e **não** roda de novo. Sem
    isso, um clique repetido no painel dobraria as execuções daquele bloco e a
    agregação contaria duas vezes.

    **Sem cópia e sem banco na execução** — `calibre.rodar` é pura, e é isso
    que torna os blocos independentes. Demonstrado em `test_a1b_parcelado`.
    """
    from ..store import bloco_atomico
    from . import escopos as esc

    linha = oficial.execute(
        "SELECT congelado_json, blocos_esperados FROM certificacao_execucao"
        " WHERE id = ? AND escopo = ?",
        (execucao_id, esc.A1B),
    ).fetchone()
    if linha is None:
        raise ValueError(f"execucao a1b {execucao_id} nao existe")
    if not (0 <= indice_bloco < int(linha["blocos_esperados"])):
        raise ValueError(
            f"bloco {indice_bloco} fora de 0..{int(linha['blocos_esperados']) - 1}"
        )

    ja = oficial.execute(
        "SELECT estado, quantas FROM certificacao_bloco"
        " WHERE execucao_id = ? AND escopo = ? AND indice_bloco = ?",
        (execucao_id, esc.A1B, indice_bloco),
    ).fetchone()
    if ja is not None:
        return {
            **estado_a1b(oficial, execucao_id),
            "rodou_agora": False,
            "por_que": (
                f"o bloco {indice_bloco} ja existe com estado"
                f" {ja['estado']!r}: resultado de bloco NUNCA e sobrescrito, e"
                " um bloco que falhou permanece no resultado"
            ),
        }

    congelado = json.loads(linha["congelado_json"])
    try:
        feitas, micros = esc.rodar_bloco(congelado, indice_bloco)
        estado, conteudo = "concluido", [
            {
                "desenho": u.desenho, "indice": u.indice,
                "r_lote": u.r_lote, "v_lote": u.v_lote,
                "r_com_portao": u.r_com_portao, "v_com_portao": u.v_com_portao,
                "sinais_piso": u.sinais_piso,
                "promovidos_piso": u.promovidos_piso,
                "sinais_detectavel": u.sinais_detectavel,
                "promovidos_detectavel": u.promovidos_detectavel,
            }
            for u in feitas
        ]
    except Exception as erro:  # noqa: BLE001 - a falha FICA no registro
        estado, micros = "falhou", 0
        conteudo = [{"erro": f"{type(erro).__name__}: {erro}"}]
        feitas = []

    with bloco_atomico(oficial, "a1b_bloco"):
        oficial.execute(
            "INSERT INTO certificacao_bloco (execucao_id, escopo,"
            " indice_bloco, estado, quantas, conteudo_json, micros, criado_em)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                execucao_id, esc.A1B, indice_bloco, estado, len(feitas),
                json.dumps(conteudo, ensure_ascii=False), micros, _agora(),
            ),
        )
    return {**estado_a1b(oficial, execucao_id), "rodou_agora": True}


def selar_a1b(
    oficial: sqlite3.Connection, *, execucao_id: int, config
) -> Certificado:
    """Agrega os oito blocos e sela. Recusa com menos que os oito.

    A agregação é **exata**: `calibre.agregar` é pura sobre `list[Uma]`, e o
    conteúdo dos blocos reconstrói as 400 execuções. Não há aproximação.
    """
    from ..a1b import calibre
    from . import escopos as esc

    estado = estado_a1b(oficial, execucao_id)
    if not estado["pode_selar"]:
        raise CertificacaoRecusada(
            f"faltam os blocos {estado['faltando']}: manifesto parcial e"
            " ilegivel, e um bloco que falhou permanece no registro sem contar"
            " como concluido"
        )
    esperadas = estado["execucoes_esperadas"]
    if estado["execucoes_gravadas"] != esperadas:
        raise CertificacaoRecusada(
            f"os blocos somam {estado['execucoes_gravadas']} execucoes e o"
            f" desenho exige {esperadas} exatas (D29)"
        )

    execucoes = esc.execucoes_gravadas(oficial, execucao_id)
    linha = oficial.execute(
        "SELECT alvo_hash, componentes_json, congelado_json, build_do_backend"
        " FROM certificacao_execucao WHERE id = ?", (execucao_id,)
    ).fetchone()
    o_alvo = {
        "alvo_de_certificacao_hash": linha["alvo_hash"],
        "componentes": json.loads(linha["componentes_json"]),
        "o_que_NAO_entra_no_hash": {
            "build_do_backend": "rastreamento, e nao componente do alvo"
        },
    }
    # O alvo pode ter MUDADO desde o inicio - e ai a composicao nao vale.
    atual = alvo_mod.montar(
        oficial,
        dataset_hash=json.loads(linha["componentes_json"])["fonte_de_dados"]
        .get("hash")
        if json.loads(linha["componentes_json"])["fonte_de_dados"]["tipo"]
        == "dataset"
        else None,
        snapshot_hash=json.loads(linha["componentes_json"])["fonte_de_dados"]
        .get("hash")
        if json.loads(linha["componentes_json"])["fonte_de_dados"]["tipo"]
        == "snapshot"
        else None,
    )
    if atual["alvo_de_certificacao_hash"] != linha["alvo_hash"]:
        raise CertificacaoRecusada(
            "o alvo mudou entre o primeiro bloco e o selo: os blocos"
            f" descrevem {linha['alvo_hash'][:16]} e o laboratorio de agora e"
            f" {atual['alvo_de_certificacao_hash'][:16]}. A composicao nao vale"
            " - recomece a execucao sob o alvo novo"
        )

    casos = []
    for desenho in calibre.DESENHOS:
        agregado = calibre.agregar(execucoes, desenho=desenho, config=config)
        casos.append(
            {
                "chave": desenho,
                "familia_de_defeito": f"calibre do desenho {desenho}",
                "tipo": "estatistico",
                "observado": agregado,
                # O criterio e "limite superior do IC <= alvo" (D37, ADR 0024),
                # e ele so vale com o desenho COMPLETO - uma proporcao sobre 30
                # execucoes nao e o criterio que a D29 fixou antes do teste.
                "contido": bool(agregado.get("completo"))
                and agregado.get("promocao_do_lote", {}).get(
                    "limite_superior_ate_o_alvo"
                )
                is True,
                "promovido": False,
                "barrado": False,
            }
        )
    resultado = por_escopo_Resultado(
        casos=casos,
        micros_da_suite=sum(b["micros"] for b in estado["blocos"]),
        preparo={
            "blocos": (
                f"{estado['concluidos']} blocos de {esc.POR_BLOCO} execucoes,"
                " sem copia: `calibre.rodar` e pura"
            )
        },
        intocado=json.loads(
            oficial.execute(
                "SELECT intocado_json FROM certificacao_execucao WHERE id = ?",
                (execucao_id,),
            ).fetchone()["intocado_json"]
        ),
        oficial=oficial,
    )
    return _selar(
        oficial, escopo=esc.A1B, resultado=resultado, o_alvo=o_alvo,
        build_do_backend=linha["build_do_backend"],
        congelado={
            k: v
            for k, v in json.loads(linha["congelado_json"]).items()
            if k != "base_bps"
        },
        execucao_id=execucao_id,
    )


def por_escopo_Resultado(*, casos, micros_da_suite, preparo, intocado, oficial):
    """O `Resultado` do A1b, montado do que os blocos gravaram."""
    from . import por_escopo

    return por_escopo.Resultado(
        casos=casos,
        micros_para_copiar=0,
        micros_da_suite=micros_da_suite,
        bytes_da_copia=0,
        intocado_antes=intocado,
        intocado_depois=laboratorio.fotografia(oficial),
        preparo=preparo,
    )


def certificado_do_alvo(
    conn: sqlite3.Connection, alvo_hash: str, escopo: str = "a1a"
) -> dict | None:
    """O manifesto selado para este alvo, se existir. **A alternativa 3.**

    É por aqui que a reutilização por identidade acontece: uma reancoragem que
    não altere nenhum dos sete componentes produz o mesmo `alvo_hash`, e o
    certificado existente responde por ela. Uma mudança em `app/estatistica/`
    muda o componente `implementacao` e **não** responde.
    """
    linha = conn.execute(
        "SELECT m.manifesto_json, m.selado_em, m.passa"
        "  FROM certificacao_manifesto m"
        "  JOIN certificacao_execucao e ON e.id = m.execucao_id"
        " WHERE m.alvo_hash = ? AND e.escopo = ?"
        " ORDER BY m.execucao_id DESC LIMIT 1",
        (alvo_hash, escopo),
    ).fetchone()
    if linha is None:
        return None
    return {
        "manifesto": json.loads(linha["manifesto_json"]),
        "selado_em": linha["selado_em"],
        "passa": bool(linha["passa"]),
    }
