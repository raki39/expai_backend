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


def executar(
    oficial: sqlite3.Connection,
    *,
    dataset_id: int,
    config,
    config_version_id: int,
    dataset_hash: str | None = None,
    snapshot_hash: str | None = None,
    build_do_backend: str | None = None,
) -> Certificado:
    """A certificação inteira. Levanta `CertificacaoRecusada` quando não pode selar."""
    from ..a1a import braco as a1a_braco
    from ..a1a import catalogo as a1a_catalogo

    # 1. O alvo ANTES de rodar.
    o_alvo = alvo_mod.montar(
        oficial, dataset_hash=dataset_hash, snapshot_hash=snapshot_hash
    )
    esperados = a1a_catalogo.QUANTAS

    # 2. A fotografia do que nao pode ser tocado.
    antes = laboratorio.fotografia(oficial)

    # 3 a 6. A copia, a suite, o canonico, o descarte.
    inicio = time.perf_counter_ns()
    with laboratorio.laboratorio_descartavel(oficial) as copia:
        resultado = a1a_braco.rodar(
            copia.conn,
            dataset_id=dataset_id,
            config=config,
            config_version_id=config_version_id,
        )
        casos = [_caso_canonico(c) for c in resultado.controles]
        micros_copia = copia.micros_para_copiar
        bytes_copia = copia.bytes_copiados
    micros_suite = (time.perf_counter_ns() - inicio) // 1_000 - micros_copia

    # 7. A conferencia. RECUSA, e nao aviso.
    depois = laboratorio.fotografia(oficial)
    diferencas = laboratorio.conferir_intocado(antes, depois)
    if diferencas:
        raise CertificacaoRecusada(
            "o banco OFICIAL mudou durante a certificacao, entao a copia nao"
            " foi onde a escrita aconteceu e o certificado nao pode ser"
            f" selado: {'; '.join(diferencas)}"
        )
    promovidos = [c["chave"] for c in casos if c["promovido"]]
    if promovidos:
        raise CertificacaoRecusada(
            "um controle deterministico foi PROMOVIDO na certificacao:"
            f" {', '.join(promovidos)}. §14.4 e literal - uma unica promocao"
            " reprova a fase, e isso e a prova de um defeito no pipeline, nao"
            " um resultado a registrar"
        )
    if len(casos) != esperados:
        raise CertificacaoRecusada(
            f"a suite produziu {len(casos)} casos e a familia tem"
            f" {esperados}: manifesto parcial e ilegivel"
        )

    # 8. A gravacao, em transacao SEPARADA - a copia ja foi embora.
    from ..store import bloco_atomico

    familias_injetadas = {c["familia_de_defeito"] for c in casos}
    esperadas = {f.familia_de_defeito for f in a1a_catalogo.FAMILIAS}
    passa = not promovidos and not (esperadas - familias_injetadas)

    manifesto = {
        "alvo_de_certificacao_hash": o_alvo["alvo_de_certificacao_hash"],
        "componentes": o_alvo["componentes"],
        "o_que_NAO_entra_no_hash": o_alvo["o_que_NAO_entra_no_hash"],
        "build_do_backend": build_do_backend,
        "suite": {
            "hash": o_alvo["componentes"]["suite_de_controles"]["hash"],
            "casos_esperados": esperados,
            "casos_gravados": len(casos),
            "familias_faltando": sorted(esperadas - familias_injetadas),
        },
        "casos": casos,
        "passa": passa,
        "banco_oficial_intocado": {
            "antes": antes,
            "depois": depois,
            "diferencas": [],
        },
        "custo_operacional": {
            "micros_para_copiar": micros_copia,
            "micros_da_suite": micros_suite,
            "bytes_da_copia": bytes_copia,
            "o_que_isso_NAO_e": (
                "credito experimental. Os creditos de §8.6.1 medem escassez de"
                " DADO - holdout e reserva -, e isto e CPU e disco. Somar os"
                " dois faria uma recertificacao consumir orcamento de"
                " descoberta"
            ),
        },
        "o_que_a_certificacao_NAO_toca": (
            "hypothesis, tentativas, creditos, familia, BY, DSR e holdout. As"
            " escritas acontecem na COPIA, entao nenhuma consulta precisa"
            " filtrar certificacoes: elas nao estao na tabela que o DSR e o FDR"
            " leem"
        ),
        "selado_em": _agora(),
    }

    with bloco_atomico(oficial, "certificacao_selar"):
        cur = oficial.execute(
            "INSERT INTO certificacao_execucao (alvo_hash, componentes_json,"
            " build_do_backend, iniciada_em, casos_esperados,"
            " micros_para_copiar, micros_da_suite, bytes_da_copia,"
            " intocado_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                o_alvo["alvo_de_certificacao_hash"],
                json.dumps(o_alvo["componentes"], ensure_ascii=False),
                build_do_backend,
                _agora(),
                esperados,
                micros_copia,
                micros_suite,
                bytes_copia,
                json.dumps({"antes": antes, "depois": depois}, ensure_ascii=False),
            ),
        )
        execucao_id = int(cur.lastrowid)
        for caso in casos:
            oficial.execute(
                "INSERT INTO certificacao_caso (execucao_id, chave, familia,"
                " tipo, barrado, promovido, resultado_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    execucao_id,
                    caso["chave"],
                    caso["familia_de_defeito"] or "?",
                    caso["tipo"] or "estrutural",
                    1 if caso["barrado"] else 0,
                    1 if caso["promovido"] else 0,
                    json.dumps(caso, ensure_ascii=False),
                ),
            )
        # O manifesto por ultimo, e o GATILHO confere a completude: uma suite
        # que quebrou no terceiro caso nao produz documento nenhum.
        oficial.execute(
            "INSERT INTO certificacao_manifesto (execucao_id, alvo_hash,"
            " manifesto_json, selado_em, passa) VALUES (?, ?, ?, ?, ?)",
            (
                execucao_id,
                o_alvo["alvo_de_certificacao_hash"],
                json.dumps(manifesto, ensure_ascii=False),
                manifesto["selado_em"],
                1 if passa else 0,
            ),
        )

    return Certificado(
        execucao_id=execucao_id,
        alvo_hash=o_alvo["alvo_de_certificacao_hash"],
        passa=passa,
        manifesto=manifesto,
    )


def certificado_do_alvo(
    conn: sqlite3.Connection, alvo_hash: str
) -> dict | None:
    """O manifesto selado para este alvo, se existir. **A alternativa 3.**

    É por aqui que a reutilização por identidade acontece: uma reancoragem que
    não altere nenhum dos sete componentes produz o mesmo `alvo_hash`, e o
    certificado existente responde por ela. Uma mudança em `app/estatistica/`
    muda o componente `implementacao` e **não** responde.
    """
    linha = conn.execute(
        "SELECT manifesto_json, selado_em, passa FROM certificacao_manifesto"
        " WHERE alvo_hash = ? ORDER BY execucao_id DESC LIMIT 1",
        (alvo_hash,),
    ).fetchone()
    if linha is None:
        return None
    return {
        "manifesto": json.loads(linha["manifesto_json"]),
        "selado_em": linha["selado_em"],
        "passa": bool(linha["passa"]),
    }
