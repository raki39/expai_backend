"""A janela do piloto: derivada de regra anterior ao dado, e fechada UMA vez.

ADR 0032, requisito 7. A regra e do ADR 0027 e foi pre-declarada:

    encerra no MAIS TARDE entre 14 dias corridos e 1.000 observacoes validas

**E por ela ser anterior ao dado que o piloto pode ser reconstruido desde
2026-09-04 sem que isso seja escolher a janela depois de ver o resultado.** A
quinta pergunta do teste de escopo mira exatamente aqui - "isso permitiria
ajustar um criterio depois de ver o resultado?" -, e a resposta e nao por duas
travas:

- **o inicio e derivado**, e nao um parametro: o primeiro `t_grid` com amostra
  valida. Ninguem escolhe;
- **a janela e gravada UMA vez** e depois LIDA. Sem isso ela mudaria a cada
  amostra nova que chegasse, e "o piloto" nomearia coisas diferentes em dias
  diferentes - invisivelmente.

O `UNIQUE (contrato, venue, symbol)` da migracao 19 e o que torna a segunda
trava estrutura em vez de disciplina.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from ..aovivo.bbo import Serie
from ..store import bloco_atomico

log = logging.getLogger(__name__)

# As duas travas do ADR 0027, e as duas sao OBRIGATORIAS. O "mais tarde entre"
# existe porque cada uma sozinha tem um modo de falha:
#
#   so dias         um mercado parado daria 14 dias e amostra insuficiente
#   so observacoes  um mercado agitado daria 1.000 observacoes em 3 dias, e
#                   uma calibracao de 3 dias nao viu regime nenhum (D40 exige
#                   permanencia de 7 dias para um regime CONTAR)
DIAS_MINIMOS = 14
OBSERVACOES_MINIMAS = 1_000
MS_POR_DIA = 86_400_000


class PilotoNaoFechaAinda(Exception):
    """As duas travas ainda nao foram alcancadas pelo dado existente."""


class PilotoJaFechado(Exception):
    """Ja existe janela gravada para esta serie e contrato.

    Nao e erro de uso: e a trava que impede regravar. `ler` devolve a que
    existe.
    """


@dataclass(frozen=True)
class Janela:
    de_ms: int
    ate_ms_exclusive: int
    observacoes_validas: int
    observacoes_totais: int
    dias_corridos_x1000: int
    fechada_por: str

    @property
    def dias_corridos(self) -> float:
        return self.dias_corridos_x1000 / 1000


def _agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ler(
    conn: sqlite3.Connection, serie: Serie, contrato: str
) -> Janela | None:
    """A janela gravada, ou `None`. NUNCA recalcula."""
    linha = conn.execute(
        "SELECT de_ms, ate_ms_exclusive, observacoes_validas,"
        "       observacoes_totais, dias_corridos_x1000, fechada_por"
        "  FROM janela_piloto"
        " WHERE contrato = ? AND venue = ? AND symbol = ?",
        (contrato, serie.venue, serie.symbol),
    ).fetchone()
    if linha is None:
        return None
    return Janela(
        de_ms=int(linha["de_ms"]),
        ate_ms_exclusive=int(linha["ate_ms_exclusive"]),
        observacoes_validas=int(linha["observacoes_validas"]),
        observacoes_totais=int(linha["observacoes_totais"]),
        dias_corridos_x1000=int(linha["dias_corridos_x1000"]),
        fechada_por=str(linha["fechada_por"]),
    )


def derivar(
    conn: sqlite3.Connection, serie: Serie, contrato: str
) -> Janela:
    """Calcula a janela pelas duas travas. NAO grava.

    Separado de `fechar` de proposito: dizer "quanto falta" e uma pergunta
    legitima e frequente, e ela nao pode ter como efeito colateral congelar a
    fronteira do periodo de calibracao.
    """
    grade = conn.execute(
        "SELECT grade_ms FROM bbo_contrato WHERE contrato = ?", (contrato,)
    ).fetchone()
    if grade is None:
        raise ValueError(f"contrato {contrato!r} nao existe")
    grade_ms = int(grade["grade_ms"])

    inicio = conn.execute(
        "SELECT MIN(t_grid_ms) AS t FROM bbo_amostra"
        " WHERE venue = ? AND symbol = ? AND contrato = ? AND disponivel = 1",
        (serie.venue, serie.symbol, contrato),
    ).fetchone()
    if inicio is None or inicio["t"] is None:
        raise PilotoNaoFechaAinda(
            "nenhuma amostra valida ainda: o piloto comeca na PRIMEIRA "
            "observacao valida, e ela e derivada e nao escolhida"
        )
    de_ms = int(inicio["t"])

    # Trava 1: 14 dias corridos a partir do inicio.
    fim_por_dias = de_ms + DIAS_MINIMOS * MS_POR_DIA

    # Trava 2: o instante da 1.000-esima observacao VALIDA, exclusivo.
    linha = conn.execute(
        "SELECT t_grid_ms FROM bbo_amostra"
        " WHERE venue = ? AND symbol = ? AND contrato = ? AND disponivel = 1"
        " ORDER BY t_grid_ms LIMIT 1 OFFSET ?",
        (serie.venue, serie.symbol, contrato, OBSERVACOES_MINIMAS - 1),
    ).fetchone()
    if linha is None:
        validas = conn.execute(
            "SELECT COUNT(*) AS n FROM bbo_amostra"
            " WHERE venue = ? AND symbol = ? AND contrato = ?"
            "   AND disponivel = 1",
            (serie.venue, serie.symbol, contrato),
        ).fetchone()["n"]
        raise PilotoNaoFechaAinda(
            f"{validas} observacoes validas de {OBSERVACOES_MINIMAS}. As duas "
            f"travas sao obrigatorias, e esta ainda nao foi alcancada"
        )
    fim_por_observacoes = int(linha["t_grid_ms"]) + grade_ms

    # "O MAIS TARDE entre" - literal do ADR 0027.
    if fim_por_dias >= fim_por_observacoes:
        ate, fechada_por = fim_por_dias, "dias"
    else:
        ate, fechada_por = fim_por_observacoes, "observacoes"

    ultima = conn.execute(
        "SELECT MAX(t_grid_ms) AS t FROM bbo_amostra"
        " WHERE venue = ? AND symbol = ? AND contrato = ?",
        (serie.venue, serie.symbol, contrato),
    ).fetchone()["t"]
    if ultima is None:
        raise PilotoNaoFechaAinda("nao ha amostra nenhuma gravada")
    alcance = int(ultima) + grade_ms
    if alcance < ate:
        faltam = (ate - alcance) / MS_POR_DIA
        raise PilotoNaoFechaAinda(
            f"a janela terminaria em {ate} e o dado so alcanca {alcance}. "
            f"Faltam ~{faltam:.2f} dias de coleta - a janela nao pode ser "
            f"fechada antes de o periodo dela existir"
        )

    contagem = conn.execute(
        "SELECT COUNT(*) AS total, SUM(disponivel) AS validas"
        "  FROM bbo_amostra"
        " WHERE venue = ? AND symbol = ? AND contrato = ?"
        "   AND t_grid_ms >= ? AND t_grid_ms < ?",
        (serie.venue, serie.symbol, contrato, de_ms, ate),
    ).fetchone()

    return Janela(
        de_ms=de_ms,
        ate_ms_exclusive=ate,
        observacoes_validas=int(contagem["validas"] or 0),
        observacoes_totais=int(contagem["total"] or 0),
        dias_corridos_x1000=round((ate - de_ms) * 1000 / MS_POR_DIA),
        fechada_por=fechada_por,
    )


def fechar(
    conn: sqlite3.Connection, serie: Serie, contrato: str
) -> Janela:
    """Grava a janela UMA vez. Idempotente na leitura, recusada na regravacao.

    Se ja existe, devolve a existente **sem recalcular** - porque recalcular e
    devolver seria indistinguivel, para quem chama, de uma janela que nunca se
    move. E ela precisa nao se mover.
    """
    ja = ler(conn, serie, contrato)
    if ja is not None:
        return ja

    j = derivar(conn, serie, contrato)
    with bloco_atomico(conn, "piloto_fechar"):
        conn.execute(
            "INSERT INTO janela_piloto ("
            " contrato, venue, symbol, de_ms, ate_ms_exclusive,"
            " observacoes_validas, observacoes_totais, dias_corridos_x1000,"
            " fechada_por, criado_em) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (contrato, serie.venue, serie.symbol, j.de_ms, j.ate_ms_exclusive,
             j.observacoes_validas, j.observacoes_totais,
             j.dias_corridos_x1000, j.fechada_por, _agora()),
        )
    log.info("piloto.fechado", extra={
        "contrato": contrato, "symbol": serie.symbol,
        "de_ms": j.de_ms, "ate_ms": j.ate_ms_exclusive,
        "validas": j.observacoes_validas, "totais": j.observacoes_totais,
        "fechada_por": j.fechada_por,
    })
    return j
