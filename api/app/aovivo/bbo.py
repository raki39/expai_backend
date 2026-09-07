"""Amostra de BBO alinhada a grade. ADR 0032, incremento 18.

O insumo da calibracao (ADR 0027) e `E2 = P_exec_previsto - ask`, e o `ask`
existe so no volume do coletor, em Singapura. Este modulo e a ponta que recebe
o que atravessa.

## Uma observacao por barra, e o numero nao e escolha de engenharia

O ADR 0027 conta em BARRAS - "mediana de 1.114 barras em 90 dias", e
90 x 96 = 8.640. Entao atravessam 96 amostras por dia contra as 86.400 que o
coletor grava: **0,11% do fluxo**, e exatamente a taxa de escrita que a `api`
ja absorve do rele.

## O que este modulo NAO faz, e e o ponto

Ele nao decide nada sobre a calibracao. Recebe, valida e grava. Quem confere
se o contrato ainda descreve a semantica de execucao vigente e
`conferir_contrato`, e quem chama isso e a calibracao - **antes** de usar
qualquer amostra, e recusando em vez de continuar.

A recusa e da CALIBRACAO e nao da ingestao: as amostras continuam chegando
porque sao observacao de mercado e nao deixam de ser verdadeiras. Recusar a
ingestao perderia dado irrecuperavel para proteger uma conta que se pode
simplesmente nao fazer.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from ..store import bloco_atomico

log = logging.getLogger(__name__)

# Os motivos de indisponibilidade que o coletor produz (ADR 0028). Lista
# FECHADA: um motivo novo do outro lado tem de quebrar aqui, e nao entrar
# calado numa coluna de texto que ninguem le.
MOTIVOS = ("sem_mensagem", "desconectado", "defasada", "sem_amostra_na_janela")


class AmostraInvalida(Exception):
    """O conteudo nao faz sentido como amostra alinhada."""


class DivergenciaDeAmostra(Exception):
    """A mesma chave ja existe com outro conteudo. ERRO GRAVE.

    Nao e "aceito com aviso": ou a extracao mudou sem trocar de contrato, ou
    algo corrompeu o dado no caminho - e escolher uma das versoes seria
    decidir qual passado vale.
    """


class ContratoDesconhecido(Exception):
    """Nao ha contrato gravado com esse nome."""


class ContratoNaoDescreveMais(Exception):
    """A semantica de execucao mudou, e as amostras deixaram de cobrir.

    Amostrar na abertura da barra so cobre os instantes de execucao porque a
    D20 fixou `execution_reference` ali. Se ela mudar, as 96 amostras/dia
    deixam de cobrir o que importa - e sem esta excecao **nada acusaria**.
    """


@dataclass(frozen=True)
class Contrato:
    contrato: str
    timeframe: str
    execution_reference: str
    latency_bars: int
    grade_ms: int
    tolerancia_ms: int


@dataclass(frozen=True)
class Serie:
    venue: str
    symbol: str
    price_scale_exp: int
    volume_scale_exp: int


@dataclass(frozen=True)
class Amostra:
    """Uma observacao de calibracao. `disponivel=False` NUNCA carrega preco."""

    t_grid_ms: int
    disponivel: bool
    motivo: str | None = None

    bid: int | None = None
    bid_qty: int | None = None
    ask: int | None = None
    ask_qty: int | None = None
    u: int | None = None

    received_at_ms: int | None = None
    received_at_corrigido_ms: int | None = None
    sampled_at_ms: int | None = None
    defasagem_ms: int | None = None

    offset_us: int | None = None
    rtt_us: int | None = None
    incerteza_residual_us: int | None = None
    # De QUANDO e o offset que corrigiu esta observacao. `incerteza_residual`
    # cobre a assimetria da viagem; a idade cobre o envelhecimento, e sem ela
    # seis horas e trinta segundos ficam indistinguiveis.
    relogio_medido_em_ms: int | None = None


def _agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def corrigir(received_at_ms: int, offset_us: int) -> int:
    """A regra do horario corrigido, escrita UMA vez.

    Inteira de proposito: o coletor calcula, a `api` recalcula, e os dois sao
    comparados por IGUALDADE. Com float os dois lados discordariam por
    arredondamento e a divergencia apareceria como corrupcao de dado.

    `offset` positivo = o relogio da exchange esta ADIANTE do nosso, entao
    somar o offset leva o horario local para a escala da exchange - que e a
    escala em que `t_grid` vive.
    """
    return received_at_ms + offset_us // 1000


def ler_contrato(conn: sqlite3.Connection, contrato: str) -> Contrato:
    linha = conn.execute(
        "SELECT contrato, timeframe, execution_reference, latency_bars,"
        "       grade_ms, tolerancia_ms"
        "  FROM bbo_contrato WHERE contrato = ?",
        (contrato,),
    ).fetchone()
    if linha is None:
        raise ContratoDesconhecido(
            f"nao ha contrato {contrato!r} gravado. Um contrato novo entra por "
            f"migracao, e nao pela rota - ele declara a semantica sob a qual "
            f"toda amostra foi medida"
        )
    return Contrato(
        contrato=linha["contrato"], timeframe=linha["timeframe"],
        execution_reference=linha["execution_reference"],
        latency_bars=int(linha["latency_bars"]),
        grade_ms=int(linha["grade_ms"]),
        tolerancia_ms=int(linha["tolerancia_ms"]),
    )


def conferir_contrato(
    conn: sqlite3.Connection, contrato: str, *,
    timeframe: str, execution_reference: str, latency_bars: int,
) -> Contrato:
    """RECUSA se a semantica de execucao vigente nao for a do contrato.

    Requisito 5 do ADR 0032, e o que mais importa: a amostragem depende de uma
    escolha que mora noutro lugar (a config versionada). Sem esta conferencia,
    trocar `execution_reference` faria as amostras deixarem de cobrir os
    instantes de execucao **sem que nada acusasse** - a forma exata do defeito
    que este projeto conta dezoito vezes.

    Chamada pela CALIBRACAO, e nao pela ingestao.
    """
    c = ler_contrato(conn, contrato)
    divergencias = []
    if c.timeframe != timeframe:
        divergencias.append(f"timeframe: contrato {c.timeframe}, config {timeframe}")
    if c.execution_reference != execution_reference:
        divergencias.append(
            f"execution_reference: contrato {c.execution_reference}, "
            f"config {execution_reference}"
        )
    if c.latency_bars != latency_bars:
        divergencias.append(
            f"latency_bars: contrato {c.latency_bars}, config {latency_bars}"
        )
    if divergencias:
        raise ContratoNaoDescreveMais(
            f"o contrato {contrato} nao descreve mais a semantica de execucao "
            f"vigente: {'; '.join(divergencias)}. As amostras foram alinhadas "
            f"sob a semantica do contrato, e usa-las sob outra mediria o "
            f"instante errado. Extraia um contrato novo do arquivo bruto - as "
            f"amostras atuais continuam validas sob o contrato delas"
        )
    return c


def validar(a: Amostra, serie: Serie, c: Contrato) -> None:
    """Validacao INTEGRAL, e ela e da `api` mesmo o coletor sendo codigo nosso.

    Ele roda noutro lugar e fala pela rede: confiar na validacao dele moveria
    a fronteira de confianca para fora do processo que grava.
    """
    if a.t_grid_ms % c.grade_ms != 0:
        raise AmostraInvalida(
            f"t_grid {a.t_grid_ms} nao cai na grade de {c.grade_ms} ms - "
            f"amostra desalinhada nao e amostra da grade"
        )

    if not a.disponivel:
        if a.motivo not in MOTIVOS:
            raise AmostraInvalida(
                f"motivo {a.motivo!r} nao esta na lista fechada {MOTIVOS}"
            )
        if any(v is not None for v in (a.bid, a.bid_qty, a.ask, a.ask_qty)):
            raise AmostraInvalida(
                "amostra indisponivel com preco preenchido: a D41 e literal "
                "sobre nao interpolar, e repetir a cotacao anterior num campo "
                "que ninguem olha e a forma mais facil de violar isso"
            )
        return

    if a.motivo is not None:
        raise AmostraInvalida("amostra disponivel nao pode ter motivo")
    faltando = [
        n for n, v in (
            ("bid", a.bid), ("bid_qty", a.bid_qty), ("ask", a.ask),
            ("ask_qty", a.ask_qty), ("received_at_ms", a.received_at_ms),
            ("received_at_corrigido_ms", a.received_at_corrigido_ms),
            ("offset_us", a.offset_us),
            ("relogio_medido_em_ms", a.relogio_medido_em_ms),
        ) if v is None
    ]
    if faltando:
        raise AmostraInvalida(f"amostra disponivel sem {faltando}")

    assert a.bid is not None and a.ask is not None
    assert a.bid_qty is not None and a.ask_qty is not None
    if min(a.bid, a.ask, a.bid_qty, a.ask_qty) <= 0:
        raise AmostraInvalida("preco ou quantidade nao positivos")
    if a.bid > a.ask:
        raise AmostraInvalida(
            f"livro cruzado: bid {a.bid} > ask {a.ask}. No topo de livro de "
            f"spot isso nao acontece - e sinal de dado corrompido"
        )

    # O horario corrigido e RECALCULADO aqui, e nao aceito. E a mesma razao
    # pela qual a rota do rele revalida a barra inteira.
    assert a.received_at_ms is not None and a.offset_us is not None
    esperado = corrigir(a.received_at_ms, a.offset_us)
    if a.received_at_corrigido_ms != esperado:
        raise AmostraInvalida(
            f"horario corrigido nao bate: recebido {a.received_at_corrigido_ms}, "
            f"recalculado {esperado} de received_at {a.received_at_ms} + "
            f"offset {a.offset_us} us"
        )

    defasagem = a.t_grid_ms - esperado
    if a.defasagem_ms != defasagem:
        raise AmostraInvalida(
            f"defasagem nao bate: recebida {a.defasagem_ms}, recalculada "
            f"{defasagem}"
        )
    if defasagem < 0:
        raise AmostraInvalida(
            f"defasagem {defasagem} ms e NEGATIVA: a cotacao e posterior ao "
            f"instante da grade, e usa-la seria ler o futuro dele"
        )
    if defasagem > c.tolerancia_ms:
        raise AmostraInvalida(
            f"defasagem {defasagem} ms passa a tolerancia de "
            f"{c.tolerancia_ms} ms do contrato {c.contrato}, e a amostra veio "
            f"marcada como disponivel - deveria ser indisponivel por "
            f"'defasada'"
        )


_CONTEUDO = (
    "disponivel", "motivo", "bid", "bid_qty", "ask", "ask_qty", "u",
    "received_at_ms", "received_at_corrigido_ms", "sampled_at_ms",
    "defasagem_ms", "offset_us", "rtt_us", "incerteza_residual_us",
    "relogio_medido_em_ms",
    "grade_ms", "price_scale_exp", "volume_scale_exp",
)


@dataclass(frozen=True)
class Recebimento:
    aceitas: int
    repetidas: int
    primeira_ms: int | None
    ultima_ms: int | None


def receber(
    conn: sqlite3.Connection,
    serie: Serie,
    contrato: str,
    amostras: Iterable[Amostra],
) -> Recebimento:
    """Grava um lote, idempotente por `(venue, symbol, t_grid_ms, contrato)`.

    Tres caminhos, e a diferenca entre o segundo e o terceiro e o ponto:

      nova         grava
      IDENTICA     nao grava, e conta como repetida. Retomada normal
      DIFERENTE    `DivergenciaDeAmostra`. Erro grave, e nada e gravado

    A transacao e DESTE modulo, com `bloco_atomico` - a licao do incremento
    16, onde o `with conn:` da rota era no-op sob `isolation_level=None` e o
    docstring afirmava atomicidade que nao existia.
    """
    c = ler_contrato(conn, contrato)
    aceitas = repetidas = 0
    marcos: list[int] = []
    agora = _agora()

    with bloco_atomico(conn, "bbo_receber"):
        for a in amostras:
            validar(a, serie, c)
            chegando = {
                "disponivel": 1 if a.disponivel else 0,
                "motivo": a.motivo,
                "bid": a.bid, "bid_qty": a.bid_qty,
                "ask": a.ask, "ask_qty": a.ask_qty, "u": a.u,
                "received_at_ms": a.received_at_ms,
                "received_at_corrigido_ms": a.received_at_corrigido_ms,
                "sampled_at_ms": a.sampled_at_ms,
                "defasagem_ms": a.defasagem_ms,
                "offset_us": a.offset_us, "rtt_us": a.rtt_us,
                "incerteza_residual_us": a.incerteza_residual_us,
                "relogio_medido_em_ms": a.relogio_medido_em_ms,
                "grade_ms": c.grade_ms,
                "price_scale_exp": serie.price_scale_exp,
                "volume_scale_exp": serie.volume_scale_exp,
            }

            existente = conn.execute(
                f"SELECT {', '.join(_CONTEUDO)} FROM bbo_amostra"
                " WHERE venue = ? AND symbol = ? AND t_grid_ms = ?"
                "   AND contrato = ?",
                (serie.venue, serie.symbol, a.t_grid_ms, contrato),
            ).fetchone()

            if existente is not None:
                atual = {k: existente[k] for k in _CONTEUDO}
                if atual != chegando:
                    difs = [
                        f"{k}: gravado {atual[k]!r}, chegando {chegando[k]!r}"
                        for k in _CONTEUDO if atual[k] != chegando[k]
                    ]
                    raise DivergenciaDeAmostra(
                        f"{serie.symbol} em t_grid {a.t_grid_ms} sob "
                        f"{contrato}: {'; '.join(difs)}. A tabela e "
                        f"append-only e a extracao e deterministica - ou a "
                        f"regra mudou sem trocar de contrato, ou algo "
                        f"corrompeu o dado"
                    )
                repetidas += 1
                continue

            conn.execute(
                "INSERT INTO bbo_amostra ("
                " venue, symbol, t_grid_ms, contrato, grade_ms,"
                " disponivel, motivo, bid, bid_qty, ask, ask_qty, u,"
                " received_at_ms, received_at_corrigido_ms, sampled_at_ms,"
                " defasagem_ms, offset_us, rtt_us, incerteza_residual_us,"
                " relogio_medido_em_ms,"
                " price_scale_exp, volume_scale_exp, recebido_em)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    serie.venue, serie.symbol, a.t_grid_ms, contrato,
                    c.grade_ms, chegando["disponivel"], a.motivo,
                    a.bid, a.bid_qty, a.ask, a.ask_qty, a.u,
                    a.received_at_ms, a.received_at_corrigido_ms,
                    a.sampled_at_ms, a.defasagem_ms,
                    a.offset_us, a.rtt_us, a.incerteza_residual_us,
                    a.relogio_medido_em_ms,
                    serie.price_scale_exp, serie.volume_scale_exp, agora,
                ),
            )
            aceitas += 1
            marcos.append(a.t_grid_ms)

    return Recebimento(
        aceitas=aceitas, repetidas=repetidas,
        primeira_ms=min(marcos) if marcos else None,
        ultima_ms=max(marcos) if marcos else None,
    )


def ultimo_t_grid(
    conn: sqlite3.Connection, serie: Serie, contrato: str
) -> int | None:
    """O ultimo instante ja gravado, DISPONIVEL OU NAO.

    Indisponivel tambem e observacao: ela declara que naquele instante nao
    havia cotacao utilizavel, e a cobertura precisa dela. Retomar de antes
    dela faria a extracao reprocessar o que ja concluiu.
    """
    linha = conn.execute(
        "SELECT MAX(t_grid_ms) AS t FROM bbo_amostra"
        " WHERE venue = ? AND symbol = ? AND contrato = ?",
        (serie.venue, serie.symbol, contrato),
    ).fetchone()
    return None if linha is None or linha["t"] is None else int(linha["t"])


def cobertura(
    conn: sqlite3.Connection, serie: Serie, contrato: str,
    *, de_ms: int | None = None, ate_ms_exclusive: int | None = None,
) -> dict:
    """Quantas observacoes existem, e quantas servem.

    A fracao disponivel e o numero que a calibracao precisa declarar junto com
    qualquer estimativa - do mesmo jeito que a D40 conta cobertura de regime.
    """
    onde = ["venue = ?", "symbol = ?", "contrato = ?"]
    args: list[object] = [serie.venue, serie.symbol, contrato]
    if de_ms is not None:
        onde.append("t_grid_ms >= ?")
        args.append(de_ms)
    if ate_ms_exclusive is not None:
        onde.append("t_grid_ms < ?")
        args.append(ate_ms_exclusive)

    linha = conn.execute(
        "SELECT COUNT(*) AS total,"
        "       SUM(disponivel) AS validas,"
        "       MIN(t_grid_ms) AS primeira,"
        "       MAX(t_grid_ms) AS ultima"
        f"  FROM bbo_amostra WHERE {' AND '.join(onde)}",
        args,
    ).fetchone()

    total = int(linha["total"] or 0)
    validas = int(linha["validas"] or 0)
    por_motivo = {
        r["motivo"]: int(r["n"])
        for r in conn.execute(
            "SELECT motivo, COUNT(*) AS n FROM bbo_amostra"
            f" WHERE {' AND '.join(onde)} AND disponivel = 0"
            " GROUP BY motivo",
            args,
        )
    }
    return {
        "total": total,
        "validas": validas,
        "indisponiveis": total - validas,
        "fracao_valida_ppm": 0 if total == 0 else round(validas * 1_000_000 / total),
        "primeira_ms": linha["primeira"],
        "ultima_ms": linha["ultima"],
        "por_motivo": por_motivo,
    }
