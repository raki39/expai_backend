"""O A1a PARCELADO: oito etapas, uma cópia selada, nenhum worker. OP-1.

> *"Implemente uma execução de certificação parcelada, acionada sequencialmente
> pela API/painel, sem worker permanente."* — o usuário, 2026-09-10

## O plano saiu da MEDIÇÃO

Ver `escopos.PLANO_A1A`: uma etapa por unidade indivisível — a comparação de
baselines, o braço B4 e cada uma das seis famílias —, na ordem em que o caminho
monolítico as rodava.

## As onze garantias, e onde cada uma mora

| garantia | onde |
|---|---|
| alvo e plano congelados antes da 1ª etapa | `iniciar` grava os dois; toda etapa e o selo reconferem o alvo, e alvo mudado **aborta** |
| uma única cópia, identificada e selada | `certificacao_copia`, com a execução como chave primária: token, caminho e impressão sha256 |
| etapas com índice e estado persistidos | `certificacao_etapa_tentativa` e `certificacao_bloco` |
| no máximo uma em andamento | trava em processo (a API é **um** processo uvicorn) e gatilho `etapa_em_ordem` |
| retry idempotente | concluída devolve o gravado; interrompida só reroda com a cópia na impressão de antes |
| nenhum concluído sobrescrito | `certificacao_bloco`: `UNIQUE` e imutável |
| perda da cópia aborta | `_conferir_copia`, e `laboratorio.abrir_copia` **nunca cria** arquivo |
| manifesto só com tudo | os gatilhos das migrações 30 a 32 |
| conteúdo canônico | `canonico.sem_ids_temporarios`, no selo comum aos cinco escopos |
| descarte após conclusão ou aborto | `_descartar`, registrado em `certificacao_copia_descarte` |
| banco oficial intocado | fotografia no início, em cada etapa e no selo |

## A impressão, e por que ela é o que torna o retry seguro

Uma etapa interrompida — processo derrubado, deploy no meio — deixa uma
tentativa sem resultado. Rodá-la de novo só é idempotente se a cópia estiver
**exatamente** no estado em que a etapa começou. A impressão é o sha256 do
arquivo principal com o WAL zerado: duas leituras sem escrita no meio dão os
mesmos bytes, e qualquer escrita — até um `UPDATE` que não muda contagem nenhuma
— muda a impressão. Confere: reroda. Não confere: **aborta**, porque rodar
sobre um estado que o plano não descreve é reconstruir sobre outro estado.
"""

from __future__ import annotations

import json
import pathlib
import secrets
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone

from . import alvo as alvo_mod
from . import escopos as esc
from . import laboratorio
from . import suite


class EtapaEmAndamento(Exception):
    """Outra etapa desta execução está rodando agora. No máximo uma por vez."""


class ExecucaoAbortada(Exception):
    """A execução foi abortada, com motivo gravado. Nada mais roda nela."""


# A trava de "no maximo uma etapa em andamento". Em processo, e isso basta
# porque a API e UM processo uvicorn (`start-backend.sh`, sem `--workers`). Se
# o processo morre, a trava morre junto - e a etapa aparece como interrompida,
# que e exatamente o caso que a impressao da copia resolve.
_TRAVAS: dict[int, threading.Lock] = {}
_TRAVA_DAS_TRAVAS = threading.Lock()
# E uma para o INICIO: dois POSTs simultaneos sem execucao aberta criariam duas.
_TRAVA_DO_INICIO = threading.Lock()


def _trava(execucao_id: int) -> threading.Lock:
    with _TRAVA_DAS_TRAVAS:
        return _TRAVAS.setdefault(execucao_id, threading.Lock())


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pasta(execucao_id: int, token: str) -> pathlib.Path:
    return pathlib.Path(tempfile.gettempdir()) / (
        f"certificacao-a1a-{execucao_id}-{token}"
    )


# ---------------------------------------------------------------------------
# Leitura do registro
# ---------------------------------------------------------------------------


def _execucao(oficial: sqlite3.Connection, execucao_id: int) -> sqlite3.Row:
    linha = oficial.execute(
        "SELECT id, escopo, alvo_hash, componentes_json, congelado_json,"
        "       blocos_esperados, build_do_backend, intocado_json, iniciada_em"
        "  FROM certificacao_execucao WHERE id = ?",
        (execucao_id,),
    ).fetchone()
    if linha is None or linha["escopo"] != esc.A1A or not linha["blocos_esperados"]:
        raise ValueError(f"execucao a1a parcelada {execucao_id} nao existe")
    return linha


def _um(oficial, tabela: str, execucao_id: int) -> sqlite3.Row | None:
    return oficial.execute(
        f"SELECT * FROM {tabela} WHERE execucao_id = ?", (execucao_id,)
    ).fetchone()


def _blocos(oficial, execucao_id: int) -> dict[int, sqlite3.Row]:
    return {
        int(b["indice_bloco"]): b
        for b in oficial.execute(
            "SELECT * FROM certificacao_bloco WHERE execucao_id = ?"
            " ORDER BY indice_bloco",
            (execucao_id,),
        )
    }


def _concluidas(blocos: dict[int, sqlite3.Row]) -> int:
    k = 0
    while k in blocos and blocos[k]["estado"] == "concluido":
        k += 1
    return k


def ultima_do_alvo(oficial: sqlite3.Connection, alvo_hash: str) -> int | None:
    """A execução a1a parcelada mais recente deste alvo, se houver."""
    linha = oficial.execute(
        "SELECT id FROM certificacao_execucao"
        " WHERE escopo = 'a1a' AND alvo_hash = ? AND blocos_esperados > 0"
        " ORDER BY id DESC LIMIT 1",
        (alvo_hash,),
    ).fetchone()
    return int(linha["id"]) if linha else None


def _alvo_de_agora(oficial, componentes_json: str) -> dict:
    fonte = json.loads(componentes_json)["fonte_de_dados"]
    return alvo_mod.montar(
        oficial,
        dataset_hash=fonte.get("hash") if fonte["tipo"] == "dataset" else None,
        snapshot_hash=fonte.get("hash") if fonte["tipo"] == "snapshot" else None,
    )


def estado(oficial: sqlite3.Connection, execucao_id: int) -> dict:
    """O progresso inteiro, derivado do registro. É o que o painel lê."""
    linha = _execucao(oficial, execucao_id)
    congelado = json.loads(linha["congelado_json"])
    plano = congelado["plano"]
    blocos = _blocos(oficial, execucao_id)
    tentativas: dict[int, int] = {}
    for t in oficial.execute(
        "SELECT indice_etapa, COUNT(*) AS n FROM certificacao_etapa_tentativa"
        " WHERE execucao_id = ? GROUP BY indice_etapa",
        (execucao_id,),
    ):
        tentativas[int(t["indice_etapa"])] = int(t["n"])
    aborto = _um(oficial, "certificacao_aborto", execucao_id)
    selado = _um(oficial, "certificacao_manifesto", execucao_id)
    copia = _um(oficial, "certificacao_copia", execucao_id)
    descarte = _um(oficial, "certificacao_copia_descarte", execucao_id)
    em_andamento = _trava(execucao_id).locked()
    k = _concluidas(blocos)

    etapas = []
    for i, nome in enumerate(plano):
        b = blocos.get(i)
        if b is not None:
            situacao = "concluida" if b["estado"] == "concluido" else "falhou"
        elif tentativas.get(i) and em_andamento and i == k:
            situacao = "em_andamento"
        elif tentativas.get(i):
            situacao = "interrompida"
        else:
            situacao = "pendente"
        etapas.append(
            {
                "indice": i,
                "etapa": nome,
                "estado": situacao,
                "tentativas": tentativas.get(i, 0),
                "micros": int(b["micros"]) if b is not None else None,
                "concluida_em": b["criado_em"] if b is not None else None,
            }
        )
    fim = aborto is not None or selado is not None
    return {
        "execucao_id": execucao_id,
        "escopo": esc.A1A,
        "alvo_de_certificacao_hash": linha["alvo_hash"],
        "iniciada_em": linha["iniciada_em"],
        "plano": plano,
        "etapas": etapas,
        "concluidas": k,
        "total": len(plano),
        "proxima": None if fim or k >= len(plano) else k,
        "pode_selar": not fim and k == len(plano),
        "em_andamento": em_andamento,
        "abortada": (
            {"motivo": aborto["motivo"], "abortada_em": aborto["abortada_em"]}
            if aborto is not None
            else None
        ),
        "selado": (
            {"passa": bool(selado["passa"]), "selado_em": selado["selado_em"]}
            if selado is not None
            else None
        ),
        "copia": (
            {
                "token": copia["token"],
                "impressao_do_selo": copia["impressao"],
                "bytes_no_selo": int(copia["bytes"]),
                "micros_para_copiar": int(copia["micros_para_copiar"]),
                "selada_em": copia["selada_em"],
                "descartada": (
                    {
                        "motivo": descarte["motivo"],
                        "residuo": bool(descarte["residuo"]),
                        "descartada_em": descarte["descartada_em"],
                    }
                    if descarte is not None
                    else None
                ),
            }
            if copia is not None
            else None
        ),
        "insumos_congelados": congelado,
    }


# ---------------------------------------------------------------------------
# O ciclo: iniciar, etapa, selar - e o aborto
# ---------------------------------------------------------------------------


def iniciar(
    oficial: sqlite3.Connection,
    *,
    dataset_id: int,
    config,
    config_version_id: int,
    dataset_hash: str | None = None,
    snapshot_hash: str | None = None,
    build_do_backend: str | None = None,
    nova_execucao: bool = False,
) -> dict:
    """Congela alvo e plano, sela a cópia. **Antes da primeira etapa.**

    Idempotente: se já houver execução deste alvo aberta ou selada, devolve o
    estado dela. Se a última foi **abortada**, começar outra é decisão
    explícita (`nova_execucao`) — um clique automático do painel não pode
    transformar um aborto em recomeço sem ninguém ver.
    """
    from ..store import bloco_atomico

    with _TRAVA_DO_INICIO:
        o_alvo = alvo_mod.montar(
            oficial, dataset_hash=dataset_hash, snapshot_hash=snapshot_hash
        )
        alvo_hash = o_alvo["alvo_de_certificacao_hash"]
        ultima = ultima_do_alvo(oficial, alvo_hash)
        if ultima is not None:
            aborto = _um(oficial, "certificacao_aborto", ultima)
            if aborto is None:
                return estado(oficial, ultima)
            if not nova_execucao:
                raise ExecucaoAbortada(
                    f"a execucao {ultima} deste alvo foi ABORTADA:"
                    f" {aborto['motivo']}. Comecar outra e decisao explicita -"
                    " mande `nova_execucao: true`. Ela congela o alvo de novo e"
                    " copia o banco oficial de AGORA; nada da execucao abortada"
                    " e aproveitado"
                )

        congelado = {
            "plano": list(esc.PLANO_A1A),
            "dataset_id": dataset_id,
            "config_version_id": config_version_id,
            "congelado_em": _agora(),
            "por_que_congelado": (
                "alvo e plano sao fixados ANTES da primeira etapa. Decidir o"
                " corte depois de ver uma etapa rodar seria escolher olhando o"
                " resultado, e um alvo que mudasse no meio produziria um"
                " certificado de um laboratorio que nunca existiu"
            ),
        }
        with bloco_atomico(oficial, "a1a_iniciar"):
            cur = oficial.execute(
                "INSERT INTO certificacao_execucao (alvo_hash, componentes_json,"
                " build_do_backend, iniciada_em, casos_esperados, intocado_json,"
                " escopo, congelado_json, blocos_esperados)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    alvo_hash,
                    json.dumps(o_alvo["componentes"], ensure_ascii=False),
                    build_do_backend,
                    _agora(),
                    esc.CASOS_ESPERADOS[esc.A1A],
                    json.dumps(laboratorio.fotografia(oficial), ensure_ascii=False),
                    esc.A1A,
                    json.dumps(congelado, ensure_ascii=False),
                    len(esc.PLANO_A1A),
                ),
            )
        execucao_id = int(cur.lastrowid)

        token = secrets.token_hex(16)
        destino = _pasta(execucao_id, token) / "laboratorio.sqlite3"
        try:
            destino.parent.mkdir(parents=True, exist_ok=False)
            micros = laboratorio.criar_copia(oficial, destino)
            impressao = laboratorio.impressao(destino)
            tamanho = destino.stat().st_size
        except Exception as erro:  # noqa: BLE001 - a falha vira ABORTO gravado
            _abortar(
                oficial, execucao_id,
                f"a copia nao pode ser criada: {type(erro).__name__}: {erro}",
            )
            raise ExecucaoAbortada(
                f"execucao {execucao_id} abortada: a copia nao pode ser criada"
            ) from erro
        with bloco_atomico(oficial, "a1a_copia"):
            oficial.execute(
                "INSERT INTO certificacao_copia (execucao_id, token, caminho,"
                " impressao, bytes, micros_para_copiar, selada_em)"
                " VALUES (?,?,?,?,?,?,?)",
                (execucao_id, token, str(destino), impressao, tamanho, micros,
                 _agora()),
            )
        return estado(oficial, execucao_id)


def _conferir_copia(caminho: pathlib.Path, esperada: str) -> str | None:
    """`None` quando a cópia está onde a etapa anterior a deixou; o motivo, se não."""
    if not caminho.exists():
        return (
            f"a copia temporaria SUMIU ({caminho.parent.name}). A execucao"
            " aborta em vez de reconstruir sobre outro estado: uma copia nova"
            " seria do banco oficial de agora, e nao do que as etapas ja"
            " concluidas transformaram"
        )
    try:
        atual = laboratorio.impressao(caminho)
    except Exception as erro:  # noqa: BLE001
        return f"a copia nao abre: {type(erro).__name__}: {erro}"
    if atual != esperada:
        return (
            f"a copia nao esta no estado em que a etapa anterior a deixou:"
            f" impressao {atual[:16]}, esperada {esperada[:16]}. Alguem escreveu"
            " nela - uma etapa interrompida no meio, ou outra coisa -, e rodar"
            " por cima seria rodar sobre um laboratorio que o plano nao descreve"
        )
    return None


def _executar(conn: sqlite3.Connection, nome: str, congelado: dict, config) -> dict:
    """Uma unidade do plano, pelas MESMAS funções do caminho de uma vez."""
    dataset_id = congelado["dataset_id"]
    config_version_id = congelado["config_version_id"]
    if nome == "preparo.baselines":
        return {"casos": [], "preparo": suite._preparar_baselines(
            conn, dataset_id=dataset_id, config=config,
            config_version_id=config_version_id,
        )}
    if nome == "preparo.b4":
        return {"casos": [], "preparo": suite._preparar_b4(
            conn, dataset_id=dataset_id, config=config,
            config_version_id=config_version_id,
        )}
    if nome.startswith("a1a."):
        from ..a1a import braco, catalogo

        familia = catalogo.POR_CHAVE[nome.removeprefix("a1a.")]
        contexto = braco.abrir(
            conn, dataset_id=dataset_id, config=config,
            config_version_id=config_version_id,
        )
        r = braco.rodar_familia(
            conn, familia, dataset_id=dataset_id, config=config,
            config_version_id=config_version_id, contexto=contexto,
        )
        caso = suite._caso_canonico(r)
        # O MECANISMO, e nao so `promovido: False` - ver
        # `escopos.classificar_bloqueio`.
        caso["bloqueio"] = esc.classificar_bloqueio(r)
        caso["contido"] = caso["bloqueio"]["contido"]
        return {"casos": [caso], "preparo": {}}
    raise ValueError(f"etapa desconhecida no plano congelado: {nome!r}")


def rodar_etapa(oficial: sqlite3.Connection, *, execucao_id: int, config) -> dict:
    """A PRÓXIMA etapa do plano. Idempotente, e nunca duas ao mesmo tempo."""
    trava = _trava(execucao_id)
    if not trava.acquire(blocking=False):
        raise EtapaEmAndamento(
            f"a execucao {execucao_id} ja tem uma etapa em andamento: no maximo"
            " uma por vez. Leia o estado e chame de novo quando ela terminar -"
            " o pedido repetido nao roda nada"
        )
    try:
        return _rodar_etapa(oficial, execucao_id=execucao_id, config=config)
    finally:
        trava.release()


def _rodar_etapa(oficial, *, execucao_id: int, config) -> dict:
    from ..store import bloco_atomico

    linha = _execucao(oficial, execucao_id)
    aborto = _um(oficial, "certificacao_aborto", execucao_id)
    if aborto is not None:
        raise ExecucaoAbortada(
            f"execucao {execucao_id} abortada: {aborto['motivo']}"
        )
    if _um(oficial, "certificacao_manifesto", execucao_id) is not None:
        return {**estado(oficial, execucao_id), "rodou_agora": False,
                "por_que": "a execucao ja foi selada"}

    congelado = json.loads(linha["congelado_json"])
    plano = congelado["plano"]
    blocos = _blocos(oficial, execucao_id)
    k = _concluidas(blocos)
    if k >= len(plano):
        return {**estado(oficial, execucao_id), "rodou_agora": False,
                "por_que": "todas as etapas concluidas: falta selar"}

    # ------------------------------------------------ 1. o ALVO nao mudou
    agora = _alvo_de_agora(oficial, linha["componentes_json"])
    if agora["alvo_de_certificacao_hash"] != linha["alvo_hash"]:
        motivo = (
            f"o alvo mudou entre etapas: congelado"
            f" {linha['alvo_hash'][:16]}, laboratorio de agora"
            f" {agora['alvo_de_certificacao_hash'][:16]}. As etapas ja"
            " concluidas descrevem outro laboratorio"
        )
        _abortar(oficial, execucao_id, motivo)
        raise ExecucaoAbortada(motivo)

    # ---------------------------------------- 2. a COPIA e a mesma cadeia
    copia = _um(oficial, "certificacao_copia", execucao_id)
    if copia is None:
        motivo = "a execucao nao tem copia selada: o inicio nao terminou"
        _abortar(oficial, execucao_id, motivo)
        raise ExecucaoAbortada(motivo)
    esperada = (
        copia["impressao"]
        if k == 0
        else json.loads(blocos[k - 1]["conteudo_json"])["impressao_depois"]
    )
    caminho = pathlib.Path(copia["caminho"])
    problema = _conferir_copia(caminho, esperada)
    if problema:
        _abortar(oficial, execucao_id, problema)
        raise ExecucaoAbortada(problema)

    # ------------------------------------------------- 3. a TENTATIVA
    tentativa = 1 + int(
        oficial.execute(
            "SELECT COUNT(*) FROM certificacao_etapa_tentativa"
            " WHERE execucao_id = ? AND indice_etapa = ?",
            (execucao_id, k),
        ).fetchone()[0]
    )
    with bloco_atomico(oficial, "a1a_tentativa"):
        oficial.execute(
            "INSERT INTO certificacao_etapa_tentativa (execucao_id,"
            " indice_etapa, tentativa, impressao_antes, iniciada_em)"
            " VALUES (?,?,?,?,?)",
            (execucao_id, k, tentativa, esperada, _agora()),
        )

    # ---------------------------------------------------- 4. a ETAPA
    nome = plano[k]
    inicio = time.perf_counter_ns()
    conn = laboratorio.abrir_copia(caminho)
    try:
        feito = _executar(conn, nome, congelado, config)
    except Exception as erro:  # noqa: BLE001 - a falha FICA no registro
        conn.close()
        with bloco_atomico(oficial, "a1a_falhou"):
            oficial.execute(
                "INSERT INTO certificacao_bloco (execucao_id, escopo,"
                " indice_bloco, estado, quantas, conteudo_json, micros,"
                " criado_em) VALUES (?,?,?,?,?,?,?,?)",
                (
                    execucao_id, esc.A1A, k, "falhou", 0,
                    json.dumps(
                        {"etapa": nome, "tentativa": tentativa,
                         "impressao_antes": esperada,
                         "erro": f"{type(erro).__name__}: {erro}"},
                        ensure_ascii=False,
                    ),
                    (time.perf_counter_ns() - inicio) // 1_000, _agora(),
                ),
            )
        motivo = (
            f"a etapa {k} ({nome}) falhou: {type(erro).__name__}: {erro}. A"
            " copia pode ter escrita pela metade, e rerodar sobre esse estado"
            " seria reconstruir sobre outro estado"
        )
        _abortar(oficial, execucao_id, motivo)
        raise ExecucaoAbortada(motivo) from erro
    conn.close()
    micros = (time.perf_counter_ns() - inicio) // 1_000
    depois = laboratorio.impressao(caminho)

    with bloco_atomico(oficial, "a1a_etapa"):
        oficial.execute(
            "INSERT INTO certificacao_bloco (execucao_id, escopo, indice_bloco,"
            " estado, quantas, conteudo_json, micros, criado_em)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                execucao_id, esc.A1A, k, "concluido", len(feito["casos"]),
                json.dumps(
                    {
                        "etapa": nome,
                        "tentativa": tentativa,
                        "casos": feito["casos"],
                        "preparo": feito["preparo"],
                        "impressao_antes": esperada,
                        "impressao_depois": depois,
                        # O banco OFICIAL, a cada etapa: se algo andar, fica
                        # dito em QUAL etapa - e o selo recusa de todo modo.
                        "fotografia_oficial": laboratorio.fotografia(oficial),
                    },
                    ensure_ascii=False,
                ),
                micros, _agora(),
            ),
        )
    return {**estado(oficial, execucao_id), "rodou_agora": True, "etapa": nome}


def selar(oficial: sqlite3.Connection, *, execucao_id: int, config) -> suite.Certificado:
    """Junta as oito etapas e sela. **Só com todas concluídas**, e o banco confere."""
    trava = _trava(execucao_id)
    if not trava.acquire(blocking=False):
        raise EtapaEmAndamento(
            f"a execucao {execucao_id} tem uma etapa em andamento: o selo espera"
        )
    try:
        return _selar(oficial, execucao_id=execucao_id)
    finally:
        trava.release()


def _selar(oficial, *, execucao_id: int) -> suite.Certificado:
    linha = _execucao(oficial, execucao_id)
    aborto = _um(oficial, "certificacao_aborto", execucao_id)
    if aborto is not None:
        raise ExecucaoAbortada(f"execucao {execucao_id} abortada: {aborto['motivo']}")
    ja = _um(oficial, "certificacao_manifesto", execucao_id)
    if ja is not None:
        # Idempotente: selar de novo devolve o que foi selado.
        return suite.Certificado(
            execucao_id=execucao_id, alvo_hash=ja["alvo_hash"],
            passa=bool(ja["passa"]), manifesto=json.loads(ja["manifesto_json"]),
        )

    congelado = json.loads(linha["congelado_json"])
    plano = congelado["plano"]
    blocos = _blocos(oficial, execucao_id)
    k = _concluidas(blocos)
    if k != len(plano):
        raise suite.CertificacaoRecusada(
            f"{k} de {len(plano)} etapas concluidas: manifesto parcial e"
            " ilegivel"
        )

    agora = _alvo_de_agora(oficial, linha["componentes_json"])
    if agora["alvo_de_certificacao_hash"] != linha["alvo_hash"]:
        motivo = (
            f"o alvo mudou entre a ultima etapa e o selo: congelado"
            f" {linha['alvo_hash'][:16]}, agora"
            f" {agora['alvo_de_certificacao_hash'][:16]}"
        )
        _abortar(oficial, execucao_id, motivo)
        raise ExecucaoAbortada(motivo)

    copia = _um(oficial, "certificacao_copia", execucao_id)
    caminho = pathlib.Path(copia["caminho"])
    ultima = json.loads(blocos[k - 1]["conteudo_json"])
    problema = _conferir_copia(caminho, ultima["impressao_depois"])
    if problema:
        _abortar(oficial, execucao_id, problema)
        raise ExecucaoAbortada(problema)
    bytes_da_copia = sum(
        p.stat().st_size for p in caminho.parent.glob(caminho.name + "*")
        if p.is_file()
    )

    conteudos = [json.loads(blocos[i]["conteudo_json"]) for i in range(k)]
    casos = [c for conteudo in conteudos for c in conteudo["casos"]]
    preparo: dict = {"micros_por_etapa": {}}
    for i, conteudo in enumerate(conteudos):
        preparo["micros_por_etapa"][conteudo["etapa"]] = int(blocos[i]["micros"])
        for chave, valor in (conteudo.get("preparo") or {}).items():
            if chave != "micros_por_etapa":
                preparo[chave] = valor
    preparo["etapas"] = (
        f"{k} etapas numa copia selada, cada uma conferida contra a"
        " impressao em que a anterior a deixou. O token da copia NAO entra"
        " aqui: ele identifica um arquivo que nao existe mais"
    )

    from .por_escopo import Resultado

    resultado = Resultado(
        casos=casos,
        micros_para_copiar=int(copia["micros_para_copiar"]),
        micros_da_suite=sum(int(blocos[i]["micros"]) for i in range(k)),
        bytes_da_copia=bytes_da_copia,
        intocado_antes=json.loads(linha["intocado_json"]),
        intocado_depois=laboratorio.fotografia(oficial),
        preparo=preparo,
    )
    try:
        certificado = suite._selar(
            oficial, escopo=esc.A1A, resultado=resultado, o_alvo=agora,
            build_do_backend=linha["build_do_backend"], congelado=congelado,
            execucao_id=execucao_id,
        )
    except suite.CertificacaoRecusada as erro:
        _abortar(oficial, execucao_id, f"o selo foi RECUSADO: {erro}")
        raise
    _descartar(oficial, execucao_id, "selada")
    return certificado


def _descartar(oficial, execucao_id: int, motivo: str) -> None:
    """Apaga a cópia e REGISTRA. Idempotente, e nunca levanta por resíduo."""
    from ..store import bloco_atomico

    copia = _um(oficial, "certificacao_copia", execucao_id)
    if copia is None or _um(oficial, "certificacao_copia_descarte", execucao_id):
        return
    pasta = pathlib.Path(copia["caminho"]).parent
    tamanho = (
        sum(p.stat().st_size for p in pasta.iterdir() if p.is_file())
        if pasta.exists()
        else 0
    )
    if pasta.exists():
        laboratorio._apagar(pasta)
    with bloco_atomico(oficial, "a1a_descarte"):
        oficial.execute(
            "INSERT INTO certificacao_copia_descarte (execucao_id, motivo,"
            " residuo, bytes_no_descarte, descartada_em) VALUES (?,?,?,?,?)",
            (execucao_id, motivo, 1 if pasta.exists() else 0, tamanho, _agora()),
        )


def _abortar(oficial, execucao_id: int, motivo: str) -> None:
    from ..store import bloco_atomico

    if _um(oficial, "certificacao_aborto", execucao_id) is None:
        with bloco_atomico(oficial, "a1a_aborto"):
            oficial.execute(
                "INSERT INTO certificacao_aborto (execucao_id, motivo,"
                " abortada_em) VALUES (?,?,?)",
                (execucao_id, motivo, _agora()),
            )
    _descartar(oficial, execucao_id, "abortada")


def abortar(oficial: sqlite3.Connection, *, execucao_id: int, motivo: str) -> dict:
    """Aborto PEDIDO, e não detectado. Mesmo registro, mesmo descarte."""
    _execucao(oficial, execucao_id)
    if _um(oficial, "certificacao_manifesto", execucao_id) is not None:
        raise ValueError(f"a execucao {execucao_id} ja foi selada")
    trava = _trava(execucao_id)
    if not trava.acquire(blocking=False):
        raise EtapaEmAndamento(
            f"a execucao {execucao_id} tem uma etapa em andamento: abortar no"
            " meio deixaria a copia pela metade sem que a etapa soubesse"
        )
    try:
        _abortar(oficial, execucao_id, motivo)
    finally:
        trava.release()
    return estado(oficial, execucao_id)


def de_uma_vez(
    oficial: sqlite3.Connection,
    *,
    dataset_id: int,
    config,
    config_version_id: int,
    dataset_hash: str | None = None,
    snapshot_hash: str | None = None,
    build_do_backend: str | None = None,
) -> suite.Certificado:
    """As oito etapas em sequência, no mesmo processo — o MESMO caminho.

    Para quem chama de dentro (a suíte de testes, `suite.executar`). Pela API o
    a1a é sempre uma etapa por requisição.
    """
    atual = iniciar(
        oficial, dataset_id=dataset_id, config=config,
        config_version_id=config_version_id, dataset_hash=dataset_hash,
        snapshot_hash=snapshot_hash, build_do_backend=build_do_backend,
        nova_execucao=True,
    )
    while atual["proxima"] is not None:
        atual = rodar_etapa(oficial, execucao_id=atual["execucao_id"], config=config)
    return selar(oficial, execucao_id=atual["execucao_id"], config=config)
