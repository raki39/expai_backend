"""Snapshots fechados, imutaveis, com hash canonico. ADR 0029.

Um snapshot e a unica coisa que um resultado do forward pode citar. O fluxo,
nunca - ele nao tem hash, e nao ter e o desenho.

## As tres propriedades que este modulo impoe

**1. O hash e canonico, e cobre a IDENTIDADE da serie.** Hash so do conteudo
colidiria entre as mesmas barras de outra venue ou outra escala de preco. E
NADA nao deterministico entra nele: se `criado_em` entrasse, re-snapshotar o
mesmo intervalo daria hash diferente e a **idempotencia morreria**, que e a
propriedade em que as outras duas se apoiam.

**2. Materializacao e fechamento sao ATOMICOS**, por TRES mecanismos:

- **a transacao** - falha no meio, rollback, nada existe;
- **a chave estrangeira** `snapshot_bar.snapshot_id REFERENCES snapshot(id)`,
  com `foreign_keys=ON`: nenhuma barra pode apontar para snapshot inexistente,
  entao o manifesto vem primeiro **por imposicao do banco**;
- **o `JOIN` em `ler()`** - barra sem manifesto e inalcancavel, e nao "meio
  visivel".

A primeira redacao do ADR 0029 dizia "o manifesto e a ultima escrita". A chave
estrangeira torna isso impossivel, e ela e a garantia mais forte das duas - o
ADR foi corrigido, e nao o codigo.

**3. Completude e CONFERIDA no fechamento.** Snapshot incompleto so existe se
a lacuna foi aceita por uma pessoa, com motivo - mesmo padrao que a ingestao
historica ja usa: "lacuna aborta, a menos que alguem declare que a aceita. A
decisao e de uma pessoa, e fica registrada."
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from .fluxo import Barra, Serie, barras_em

log = logging.getLogger(__name__)

Finalidade = Literal["piloto", "calibracao", "revalidacao", "forward"]

# Finalidades que precisam ser DISJUNTAS entre si dentro da mesma calibracao.
#
# O ADR 0027 exige: o piloto fora da calibracao E da revalidacao, e a
# calibracao fora da revalidacao. Isso e interno a UMA calibracao.
DISJUNTAS_NA_CALIBRACAO = ("piloto", "calibracao", "revalidacao")


class SnapshotIncompleto(Exception):
    """Faltam barras, e ninguem declarou que aceita a lacuna."""

    def __init__(self, mensagem: str, manifesto: "Contagem") -> None:
        super().__init__(mensagem)
        self.manifesto = manifesto


class SobreposicaoNaLinhagem(Exception):
    """Duas finalidades disjuntas se sobrepondo na MESMA linhagem.

    Entre linhagens diferentes a sobreposicao e **permitida e esperada**: duas
    candidatas em forward no mesmo periodo de calendario sao duas observacoes
    do mesmo mercado, e nao contaminacao. Proibir globalmente impediria uma
    segunda candidata de rodar onde a primeira rodou.
    """


class DonoErrado(Exception):
    """A finalidade nao combina com o dono declarado."""


@dataclass(frozen=True)
class Contagem:
    """O que o manifesto declara sobre completude."""

    barras_esperadas: int
    barras_presentes: int
    lacunas: int
    maior_lacuna_barras: int

    @property
    def completo(self) -> bool:
        return self.barras_presentes == self.barras_esperadas


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def contar(
    serie: Serie, de_ms: int, ate_ms_exclusive: int, barras: list[Barra]
) -> Contagem:
    """Esperadas contra presentes, e o tamanho do maior buraco.

    `barras_presentes` sozinho ENGANA: um snapshot de 30 dias com metade das
    barras ausentes tem contagem plausivel e nao vale nada. E a diferenca que
    informa, e por isso as duas entram no manifesto.
    """
    esperadas = (ate_ms_exclusive - de_ms) // serie.interval_ms
    presentes = {b.open_time_ms for b in barras}

    lacunas = maior = corrido = 0
    for ms in range(de_ms, ate_ms_exclusive, serie.interval_ms):
        if ms in presentes:
            if corrido:
                lacunas += 1
                maior = max(maior, corrido)
            corrido = 0
        else:
            corrido += 1
    if corrido:
        lacunas += 1
        maior = max(maior, corrido)

    return Contagem(esperadas, len(barras), lacunas, maior)


def hash_canonico(
    serie: Serie, de_ms: int, ate_ms_exclusive: int, barras: list[Barra]
) -> str:
    """SHA-256 da identidade da serie mais o conteudo ORDENADO.

    Duas coisas fazem este hash servir para o que ele serve:

    **A identidade entra.** Sem `venue`, `symbol`, `timeframe` e as escalas, as
    mesmas barras de outra venue - ou os mesmos inteiros sob outra escala de
    preco - hashearia igual, e "o snapshot confere" nao significaria nada.

    **Nada nao deterministico entra.** Sem `id`, sem `criado_em`, sem contagem
    de recebimento. Re-snapshotar o mesmo intervalo tem de dar o MESMO hash -
    e essa idempotencia e o que permite reconferir um snapshot contra o fluxo
    meses depois.

    A lacuna NAO entra: o hash cobre o que existe, e quanto DEVERIA existir e
    `barras_esperadas`, no manifesto. Se a lacuna entrasse, dois snapshots com
    o mesmo conteudo e intervalos diferentes hashearia diferente - o que e
    verdade -, mas a conta de completude deixaria de ser conferivel em
    separado.
    """
    h = hashlib.sha256()

    def linha(*campos: object) -> None:
        h.update(("\t".join(str(c) for c in campos) + "\n").encode("utf-8"))

    linha("serie", serie.venue, serie.symbol, serie.timeframe,
          serie.interval_ms, serie.price_scale_exp, serie.volume_scale_exp)
    linha("intervalo", de_ms, ate_ms_exclusive)
    for b in sorted(barras, key=lambda x: x.open_time_ms):
        linha("barra", b.open_time_ms, b.open, b.high, b.low, b.close,
              b.volume, b.quote_volume, b.trades)
    return h.hexdigest()


def _conferir_dono(
    finalidade: Finalidade,
    calibration_version: int | None,
    hypothesis_id: int | None,
) -> None:
    """O uso unico esta ligado ao objeto certo, e o certo depende da finalidade.

    Amarrar tudo a `hypothesis_id` estaria errado: a calibracao do simulador
    **nao pertence a hipotese nenhuma** - ela e do ambiente, e vale para todas
    as candidatas que rodarem sob ela. E amarrar tudo a `calibration_version`
    tambem: o forward e da hipotese.
    """
    if finalidade in DISJUNTAS_NA_CALIBRACAO:
        if calibration_version is None or hypothesis_id is not None:
            raise DonoErrado(
                f"'{finalidade}' pertence a uma calibration_version, e nao a "
                f"uma hipotese: a calibracao e do AMBIENTE e vale para todas "
                f"as candidatas que rodarem sob ela"
            )
    elif hypothesis_id is None or calibration_version is not None:
        raise DonoErrado(
            "'forward' pertence a um hypothesis_id, pelo mesmo padrao do "
            "holdout: e a hipotese que o consome"
        )


def _conferir_sobreposicao(
    conn: sqlite3.Connection,
    serie: Serie,
    de_ms: int,
    ate_ms_exclusive: int,
    finalidade: Finalidade,
    calibration_version: int | None,
    hypothesis_id: int | None,
) -> None:
    """Disjuncao POR LINHAGEM, e nao global.

    O que se protege e o que o ADR 0027 exige: piloto fora da calibracao e da
    revalidacao, e calibracao fora da revalidacao - dentro da MESMA
    `calibration_version`. E, para forward, os intervalos de uma MESMA hipotese
    nao se sobrepondo.

    Entre linhagens, sobreposicao e permitida. Duas candidatas observando o
    mesmo mercado no mesmo mes sao duas observacoes.
    """
    if finalidade in DISJUNTAS_NA_CALIBRACAO:
        sql = (
            "SELECT finalidade, from_ms, to_ms_exclusive FROM snapshot"
            " WHERE venue = ? AND symbol = ? AND timeframe = ?"
            "   AND calibration_version = ?"
            "   AND finalidade IN ('piloto','calibracao','revalidacao')"
            "   AND from_ms < ? AND to_ms_exclusive > ?"
        )
        params = (serie.venue, serie.symbol, serie.timeframe,
                  calibration_version, ate_ms_exclusive, de_ms)
        escopo = f"calibration_version={calibration_version}"
    else:
        sql = (
            "SELECT finalidade, from_ms, to_ms_exclusive FROM snapshot"
            " WHERE venue = ? AND symbol = ? AND timeframe = ?"
            "   AND hypothesis_id = ? AND finalidade = 'forward'"
            "   AND from_ms < ? AND to_ms_exclusive > ?"
        )
        params = (serie.venue, serie.symbol, serie.timeframe,
                  hypothesis_id, ate_ms_exclusive, de_ms)
        escopo = f"hypothesis_id={hypothesis_id}"

    conflito = conn.execute(sql, params).fetchone()
    if conflito is not None:
        raise SobreposicaoNaLinhagem(
            f"[{de_ms}, {ate_ms_exclusive}) como '{finalidade}' se sobrepoe a "
            f"'{conflito['finalidade']}' em "
            f"[{conflito['from_ms']}, {conflito['to_ms_exclusive']}) dentro de "
            f"{escopo}. O ADR 0027 exige janelas disjuntas na mesma linhagem"
        )


def materializar(
    conn: sqlite3.Connection,
    serie: Serie,
    *,
    de_ms: int,
    ate_ms_exclusive: int,
    finalidade: Finalidade,
    calibration_version: int | None = None,
    hypothesis_id: int | None = None,
    lacuna_aceita_por: str | None = None,
    lacuna_aceita_motivo: str | None = None,
) -> int:
    """Fecha um snapshot do intervalo. Devolve o `snapshot_id`.

    **Atomico, e a ordem de escrita e o mecanismo:** as barras primeiro, o
    manifesto por ultimo. Se algo falhar no meio, a transacao do chamador
    desfaz e nada existe - e enquanto o manifesto nao existir, as barras sao
    inalcancaveis, porque toda leitura exige o `JOIN` com ele.

    **Idempotente:** re-materializar o mesmo intervalo com a mesma finalidade e
    o mesmo dono devolve o snapshot existente, depois de CONFERIR que o hash
    bate. Hash diferente e erro alto, e nao um segundo snapshot.
    """
    _conferir_dono(finalidade, calibration_version, hypothesis_id)

    ja = conn.execute(
        "SELECT id, sha256 FROM snapshot"
        " WHERE venue = ? AND symbol = ? AND timeframe = ?"
        "   AND from_ms = ? AND to_ms_exclusive = ? AND finalidade = ?"
        "   AND COALESCE(calibration_version, -1) = ?"
        "   AND COALESCE(hypothesis_id, -1) = ?",
        (serie.venue, serie.symbol, serie.timeframe, de_ms, ate_ms_exclusive,
         finalidade, calibration_version if calibration_version is not None else -1,
         hypothesis_id if hypothesis_id is not None else -1),
    ).fetchone()

    barras = barras_em(conn, serie, de_ms, ate_ms_exclusive)
    digest = hash_canonico(serie, de_ms, ate_ms_exclusive, barras)

    if ja is not None:
        if ja["sha256"] != digest:
            raise DivergenciaDeSnapshot(
                f"snapshot {ja['id']} do mesmo intervalo tem hash "
                f"{ja['sha256'][:12]}..., e o fluxo agora produz "
                f"{digest[:12]}.... O fluxo e append-only, entao isto nao "
                f"deveria ser possivel - e escolher uma das versoes seria "
                f"decidir qual passado vale"
            )
        return int(ja["id"])

    contagem = contar(serie, de_ms, ate_ms_exclusive, barras)

    # COMPLETUDE CONFERIDA NO FECHAMENTO. Sem aceite explicito, nao fecha.
    if not contagem.completo and not (lacuna_aceita_por and lacuna_aceita_motivo):
        raise SnapshotIncompleto(
            f"[{de_ms}, {ate_ms_exclusive}) tem {contagem.barras_presentes} de "
            f"{contagem.barras_esperadas} barras ({contagem.lacunas} lacunas, "
            f"maior de {contagem.maior_lacuna_barras}). Kline e RECUPERAVEL: "
            f"rode o backfill antes de fechar. Se a lacuna e da propria "
            f"exchange, alguem tem de declarar que a aceita, com motivo",
            contagem,
        )

    _conferir_sobreposicao(conn, serie, de_ms, ate_ms_exclusive, finalidade,
                           calibration_version, hypothesis_id)

    # ------------------------------------------------------------ atomico
    # As barras vao primeiro, mas ficam INALCANCAVEIS: `ler` exige o JOIN com
    # o manifesto. Reservamos o id inserindo o manifesto no fim, e para isso as
    # barras precisam de um id - resolvido pedindo o proximo ao SQLite.
    cur = conn.execute(
        "INSERT INTO snapshot ("
        " venue, symbol, timeframe, from_ms, to_ms_exclusive,"
        " barras_esperadas, barras_presentes, lacunas, maior_lacuna_barras,"
        " lacuna_aceita_por, lacuna_aceita_motivo, sha256, finalidade,"
        " calibration_version, hypothesis_id, criado_em) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (serie.venue, serie.symbol, serie.timeframe, de_ms, ate_ms_exclusive,
         contagem.barras_esperadas, contagem.barras_presentes,
         contagem.lacunas, contagem.maior_lacuna_barras,
         lacuna_aceita_por, lacuna_aceita_motivo, digest, finalidade,
         calibration_version, hypothesis_id, _agora()),
    )
    snapshot_id = int(cur.lastrowid)

    conn.executemany(
        "INSERT INTO snapshot_bar ("
        " snapshot_id, open_time_ms, open, high, low, close,"
        " volume, quote_volume, trades) VALUES (?,?,?,?,?,?,?,?,?)",
        [(snapshot_id, b.open_time_ms, b.open, b.high, b.low, b.close,
          b.volume, b.quote_volume, b.trades) for b in barras],
    )

    log.info("snapshot.fechado", extra={
        "snapshot_id": snapshot_id, "finalidade": finalidade,
        "de_ms": de_ms, "ate_ms": ate_ms_exclusive,
        "barras": contagem.barras_presentes,
        "esperadas": contagem.barras_esperadas,
        "lacunas": contagem.lacunas,
        "sha256": digest[:16],
    })
    return snapshot_id


class DivergenciaDeSnapshot(Exception):
    """O mesmo intervalo produz hash diferente. Impossivel sob append-only."""


def manifesto(conn: sqlite3.Connection, snapshot_id: int) -> dict | None:
    linha = conn.execute(
        "SELECT * FROM snapshot WHERE id = ?", (snapshot_id,)
    ).fetchone()
    return dict(linha) if linha is not None else None


def ler(conn: sqlite3.Connection, snapshot_id: int) -> list[Barra]:
    """Barras de um snapshot FECHADO.

    O `JOIN` com `snapshot` nao e decoracao: e o que torna barra copiada sem
    manifesto **inalcancavel**. Sem ele, um snapshot meio construido - se a
    transacao fosse partida por alguem - seria legivel como se estivesse
    pronto.
    """
    return [
        Barra(
            open_time_ms=int(l["open_time_ms"]),
            open=int(l["open"]), high=int(l["high"]),
            low=int(l["low"]), close=int(l["close"]),
            volume=int(l["volume"]), quote_volume=int(l["quote_volume"]),
            trades=int(l["trades"]),
        )
        for l in conn.execute(
            "SELECT b.open_time_ms, b.open, b.high, b.low, b.close,"
            "       b.volume, b.quote_volume, b.trades"
            "  FROM snapshot_bar b"
            "  JOIN snapshot s ON s.id = b.snapshot_id"
            " WHERE b.snapshot_id = ?"
            " ORDER BY b.open_time_ms",
            (snapshot_id,),
        )
    ]


def reconferir(conn: sqlite3.Connection, snapshot_id: int) -> bool:
    """O hash gravado ainda descreve as barras gravadas?

    Existe porque um hash que ninguem recalcula e uma afirmacao sem
    verificacao - e este projeto ja registrou dezesseis vezes o padrao de um
    valor que parou de descrever o que dizia descrever.
    """
    m = manifesto(conn, snapshot_id)
    if m is None:
        return False
    serie = Serie(
        venue=m["venue"], symbol=m["symbol"], timeframe=m["timeframe"],
        interval_ms=(m["to_ms_exclusive"] - m["from_ms"]) // m["barras_esperadas"],
        price_scale_exp=0, volume_scale_exp=0,
    )
    # As escalas nao estao no manifesto de proposito: elas pertencem a serie, e
    # a serie vem do fluxo. Para reconferir, recuperamo-las de la.
    linha = conn.execute(
        "SELECT price_scale_exp, volume_scale_exp FROM stream_bar"
        " WHERE venue = ? AND symbol = ? AND timeframe = ? LIMIT 1",
        (m["venue"], m["symbol"], m["timeframe"]),
    ).fetchone()
    if linha is not None:
        serie = Serie(
            venue=serie.venue, symbol=serie.symbol, timeframe=serie.timeframe,
            interval_ms=serie.interval_ms,
            price_scale_exp=int(linha["price_scale_exp"]),
            volume_scale_exp=int(linha["volume_scale_exp"]),
        )
    barras = ler(conn, snapshot_id)
    return hash_canonico(
        serie, int(m["from_ms"]), int(m["to_ms_exclusive"]), barras
    ) == m["sha256"]
