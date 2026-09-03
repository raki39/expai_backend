"""Prova de reprodutibilidade: tres digests, no banco (criterio 2 do inc. 7).

A R12 pede que **mesma semente, config e dataset** produzam digest identico. A
prova completa tem duas metades, e uma sem a outra nao vale nada:

1. **Mesma semente -> mesmo digest.** Sem isto o experimento nao se reproduz.
2. **Semente diferente -> digest diferente**, com `config_hash` igual. Sem
   isto o digest poderia ser uma constante disfarcada: um digest que nunca
   muda passa na primeira metade sempre, inclusive quando nada esta sendo
   medido. Foi assim que `volume_gravavel` deu falsa confianca no incremento 0.

## Por que a prova roda sobre B1, e nao sobre a regra do agente

B3 e a regra do agente sao **deterministas**: o digest delas nao depende de
semente nenhuma, e esta certo que nao dependa. Rodar a segunda metade da prova
sobre elas produziria "semente diferente, digest igual" - e o teste passaria a
medir a ausencia de aleatoriedade, nao a sensibilidade a semente.

A semente vira lancamento contabil exatamente em um lugar: nos momentos de
entrada e saida sorteados de B1. Por isso a prova usa
`rodar_b1_representativa`, que e a repeticao de B1 que passa pelo ledger.

## A semente e entrada do run, nao campo do hash

`default_seed` na config e o **valor padrao** da semente; a semente efetiva e
entrada de cada run. E o que torna coerente a exigencia do criterio: digest
diferente **com `config_hash` igual**. Se a semente entrasse no hash, a
segunda metade da prova seria impossivel de enunciar - qualquer troca de
semente mudaria o hash junto, e nunca se saberia qual dos dois mudou o digest.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Sequence

from ..config.schema import ExperimentConfig
from ..ledger import livro
from ..dataset.loader import BarraCarregada
from ..maos_rapidas import baselines, executor
from ..regra.registro import registrar_baseline

log = logging.getLogger(__name__)

# Operacoes por run da prova. Nao e o giro do experimento: e o menor numero
# que ainda produz uma sequencia de lancamentos longa o bastante para que uma
# colisao de digest por acidente seja impensavel. Prova barata para poder ser
# rodada sempre que o relatorio for gerado.
OPERACOES_DA_PROVA = 24


def _uma_passada(
    conn: sqlite3.Connection,
    *,
    dataset_id: int,
    config: ExperimentConfig,
    config_version_id: int,
    semente: int,
    barras: Sequence[BarraCarregada],
    marcador: str,
) -> dict:
    """Um run isolado de B1 com a semente dada. Devolve digest e config_hash.

    Run proprio, e nao um trecho de outro: desde a migracao 5 cada run tem
    carteira propria, e foi um defeito de contas globais que quase inviabilizou
    o incremento 4. Dois runs de prova dividindo caixa produziriam digests
    diferentes por interferencia, nao por semente.
    """
    run_id, _ = livro.abrir_run(
        conn,
        config_version_id=config_version_id,
        seed_capital_usd_cents=config.seed_capital_usd_cents,
        agent_id=marcador,
    )
    ultima_decidivel = len(barras) - 1 - config.latency_bars
    pares = baselines.sortear_pares(
        semente, ultima_decidivel, 0, OPERACOES_DA_PROVA
    )
    rule_id = registrar_baseline(
        conn,
        "aleatorio",
        # `operacoes` aqui e PARAMETRO da regra, e nao contagem de resultado:
        # "faca N idas e voltas". Fica com este nome de proposito - ele entra
        # no `content_hash` da regra, e renomea-lo trocaria o hash de uma
        # regra que nao mudou. O digest publicado nao depende dele (ele hasheia
        # lancamentos, nao parametros), mas trocar um hash para ganhar
        # consistencia de vocabulario num campo que ninguem le como contagem
        # e preco sem retorno.
        {"operacoes": OPERACOES_DA_PROVA, "prova": "reprodutibilidade"},
        condicoes=baselines.condicoes(config),
    )
    resultado = baselines.rodar_b1_representativa(
        conn,
        run_id=run_id,
        dataset_id=dataset_id,
        config=config,
        pares=pares,
        rule_id=rule_id,
        barras=barras,
    )
    livro.encerrar_run(conn, run_id, "concluido")

    config_hash = conn.execute(
        "SELECT config_hash FROM config_version WHERE id = ?", (config_version_id,)
    ).fetchone()["config_hash"]

    return {
        "run_id": run_id,
        "semente": semente,
        "digest": resultado["digest"],
        "idas_e_voltas": resultado["idas_e_voltas"],
        "equity_final_cents": resultado["equity_final_cents"],
        "config_hash": config_hash,
    }


def provar(
    conn: sqlite3.Connection,
    *,
    dataset_id: int,
    config: ExperimentConfig,
    config_version_id: int,
    semente: int | None = None,
    barras: Sequence[BarraCarregada] | None = None,
) -> dict:
    """Roda as tres passadas e devolve o veredito com os tres digests."""
    barras = list(barras) if barras is not None else executor.carregar_janela(
        conn, dataset_id
    )
    if not barras:
        raise ValueError("janela vazia: nao ha o que reproduzir")

    base = semente if semente is not None else config.default_seed
    # A outra semente e DERIVADA da primeira, e nao `base + 1`: sementes
    # vizinhas correlacionam no inicio da sequencia, e a prova ficaria mais
    # fraca justamente na metade que precisa mostrar diferenca.
    outra = baselines.derivar_semente(base, 1)

    primeira = _uma_passada(
        conn, dataset_id=dataset_id, config=config,
        config_version_id=config_version_id, semente=base, barras=barras,
        marcador="prova-r12-a",
    )
    segunda = _uma_passada(
        conn, dataset_id=dataset_id, config=config,
        config_version_id=config_version_id, semente=base, barras=barras,
        marcador="prova-r12-b",
    )
    terceira = _uma_passada(
        conn, dataset_id=dataset_id, config=config,
        config_version_id=config_version_id, semente=outra, barras=barras,
        marcador="prova-r12-c",
    )

    iguais = primeira["digest"] == segunda["digest"]
    difere = terceira["digest"] != primeira["digest"]
    hash_estavel = (
        primeira["config_hash"] == segunda["config_hash"] == terceira["config_hash"]
    )

    prova = {
        "mesma_semente": {
            "semente": base,
            "digest_a": primeira["digest"],
            "digest_b": segunda["digest"],
            "iguais": iguais,
            "runs": [primeira["run_id"], segunda["run_id"]],
        },
        "semente_diferente": {
            "semente": outra,
            "digest": terceira["digest"],
            "difere_do_primeiro": difere,
            "run": terceira["run_id"],
        },
        "config_hash": primeira["config_hash"],
        "config_hash_igual_nas_tres": hash_estavel,
        "operacoes_por_passada": OPERACOES_DA_PROVA,
        # As duas metades e a estabilidade do hash. Qualquer uma falsa e a
        # prova nao vale - e o campo diz qual, em vez de so dizer "falhou".
        "provado": bool(iguais and difere and hash_estavel),
    }
    log.info(
        "reprodutibilidade.prova",
        extra={
            "iguais": iguais,
            "difere": difere,
            "hash_estavel": hash_estavel,
            "provado": prova["provado"],
        },
    )
    return prova


def ultima_prova(conn: sqlite3.Connection) -> dict | None:
    """A prova mais recente, reconstruida dos runs que a produziram.

    Nada e guardado em duplicata: os tres digests sao recalculados dos
    lancamentos, que e a mesma conta que os produziu na primeira vez. Um
    resumo gravado a parte viraria a segunda fonte de verdade que a regra 16
    proibe - e envelheceria no dia em que alguem esquecesse de atualiza-lo.
    """
    runs = {
        l["agent_id"]: dict(l)
        for l in conn.execute(
            "SELECT id, agent_id, config_version_id FROM run"
            " WHERE agent_id LIKE 'prova-r12-%' ORDER BY id"
        )
    }
    if len(runs) < 3:
        return None

    a, b, c = (runs.get(f"prova-r12-{x}") for x in ("a", "b", "c"))
    if not (a and b and c):
        return None

    digests = {x: executor.digest_do_run(conn, r["id"]) for x, r in
               (("a", a), ("b", b), ("c", c))}
    hashes = {
        x: conn.execute(
            "SELECT config_hash FROM config_version WHERE id = ?",
            (r["config_version_id"],),
        ).fetchone()["config_hash"]
        for x, r in (("a", a), ("b", b), ("c", c))
    }
    iguais = digests["a"] == digests["b"]
    difere = digests["c"] != digests["a"]
    hash_estavel = hashes["a"] == hashes["b"] == hashes["c"]

    return {
        "mesma_semente": {
            "digest_a": digests["a"],
            "digest_b": digests["b"],
            "iguais": iguais,
            "runs": [a["id"], b["id"]],
        },
        "semente_diferente": {
            "digest": digests["c"],
            "difere_do_primeiro": difere,
            "run": c["id"],
        },
        "config_hash": hashes["a"],
        "config_hash_igual_nas_tres": hash_estavel,
        "operacoes_por_passada": OPERACOES_DA_PROVA,
        "provado": bool(iguais and difere and hash_estavel),
        "reconstruida_dos_lancamentos": True,
    }
