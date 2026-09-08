"""Quem entra na quarentena. E na 0C, ninguem. ADR 0034.

## Este modulo existe para RECUSAR

Nao e um portao que as vezes deixa passar. Na 0C ele recusa **sempre**, e o
motivo e escrito em cada recusa - porque uma ausencia que ninguem declara vira
silencio, e silencio e lido como esquecimento.

    admitir()  ->  sempre levanta, com o motivo
    avaliar()  ->  puro, noutro modulo, e e ele que a suite exercita

**A separacao e a garantia.** Se `admitir` pudesse deixar passar em algum caso,
o controle positivo sintetico teria de passar por ele - e ai ele seria um
caminho de producao capaz de produzir candidato admitido. Nao ha.

## Por que nao um `if fase == "0C"`

Porque a fase muda, e um `if` sobre ela deixaria a porta destrancada no dia da
virada, sem ninguem decidir. A recusa e o **corpo inteiro da funcao**: quando a
0C acabar, alguem tem de vir aqui e escrever o caminho de admissao, e vai ter de
decidir o que ele exige. Um `if` transformaria essa decisao em consequencia.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from ..store import bloco_atomico
from . import abordagem as abordagem_mod
from .veredito import Limiares

log = logging.getLogger(__name__)

# O `agent_id` dos baselines. O B3 roda em shadow como controle negativo, e
# **nao e candidata** - a fronteira e estrutural, e nao uma checagem que
# alguem possa remover.
PREFIXO_BASELINE = "baseline-"


class NaoAdmitida(Exception):
    """A candidata nao entra, e a mensagem diz por que."""


class LimiarNaoCongelado(Exception):
    """Nao ha limiar congelado para esta config. Congele antes do primeiro tick."""


def _agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def congelar_limiares(
    conn: sqlite3.Connection, *, config_version_id: int,
    periodo_minimo_barras: int, regimes_minimos: int,
    magnitude_minima_ppm: int, reserva_maxima_barras: int,
) -> Limiares:
    """Congela os limiares. Idempotente na leitura, recusado na regravacao.

    Congelados ANTES do primeiro tick, pelo mesmo motivo que os cortes de
    regime (ADR 0026): com so o B3 rodando, **nada testa os limiares na
    direcao positiva**, e um afrouxamento feito "para ver o pipeline
    funcionar" FICA - a proxima candidata de verdade entraria numa regua ja
    cedida.
    """
    ja = ler_limiares(conn, config_version_id)
    if ja is not None:
        return ja
    with bloco_atomico(conn, "congelar_limiares"):
        conn.execute(
            "INSERT INTO quarentena_limiar (config_version_id,"
            " periodo_minimo_barras, regimes_minimos, magnitude_minima_ppm,"
            " reserva_maxima_barras, congelado_em) VALUES (?,?,?,?,?,?)",
            (config_version_id, periodo_minimo_barras, regimes_minimos,
             magnitude_minima_ppm, reserva_maxima_barras, _agora()),
        )
    log.info("quarentena.limiares_congelados", extra={
        "config_version_id": config_version_id,
        "periodo_minimo_barras": periodo_minimo_barras,
        "regimes_minimos": regimes_minimos,
        "magnitude_minima_ppm": magnitude_minima_ppm,
        "reserva_maxima_barras": reserva_maxima_barras,
    })
    return ler_limiares(conn, config_version_id)  # type: ignore[return-value]


def ler_limiares(
    conn: sqlite3.Connection, config_version_id: int
) -> Limiares | None:
    linha = conn.execute(
        "SELECT periodo_minimo_barras, regimes_minimos, magnitude_minima_ppm,"
        "       reserva_maxima_barras FROM quarentena_limiar"
        " WHERE config_version_id = ?",
        (config_version_id,),
    ).fetchone()
    if linha is None:
        return None
    return Limiares(
        periodo_minimo_barras=int(linha["periodo_minimo_barras"]),
        regimes_minimos=int(linha["regimes_minimos"]),
        magnitude_minima_ppm=int(linha["magnitude_minima_ppm"]),
        reserva_maxima_barras=int(linha["reserva_maxima_barras"]),
    )


def exigir_limiares(
    conn: sqlite3.Connection, config_version_id: int
) -> Limiares:
    lim = ler_limiares(conn, config_version_id)
    if lim is None:
        raise LimiarNaoCongelado(
            f"nao ha limiar congelado para a config_version "
            f"{config_version_id}. Congele ANTES do primeiro tick - depois de "
            f"ver o forward, escolher o limiar e ajustar a regua ao resultado"
        )
    return lim


@dataclass(frozen=True)
class Recusa:
    motivo: str
    abordagem: str | None = None


def admitir(
    conn: sqlite3.Connection, *, hypothesis_id: int, params: dict,
    extras: dict | None = None, agent_id: str,
) -> Recusa:
    """**Sempre recusa.** Devolve a recusa em vez de so levantar.

    Devolver o objeto e nao apenas levantar tem uma razao: o relatorio precisa
    declarar **o motivo de cada exclusao** (garantia 5), e uma excecao que
    ninguem captura nao chega ao relatorio.

    A ordem das conferencias e de fora para dentro, e ela importa: a mensagem
    que sai tem de nomear a razao MAIS ESPECIFICA. "Nenhuma candidata entra na
    0C" e verdade sobre a hipotese 41, mas dizer isso esconderia que ela
    tambem esta descartada por abordagem.
    """
    if agent_id.startswith(PREFIXO_BASELINE):
        return Recusa(
            motivo=(
                f"{agent_id} e BASELINE, e baseline nao e candidata. O B3 roda "
                f"em shadow como CONTROLE NEGATIVO do encanamento: sem "
                f"pre-registro, sem credito, sem familia e sem entrar no "
                f"contador do DSR. Se a maquinaria o promovesse, a maquinaria "
                f"estaria errada - e e para isso que ele esta ali"
            ),
            abordagem=abordagem_mod.assinatura_de(params, extras),
        )

    registro = abordagem_mod.ler(
        conn, abordagem_mod.assinatura_de(params, extras)
    )
    if registro is not None and registro.estado == "rejeitada":
        return Recusa(
            motivo=(
                f"abordagem DESCARTADA: {registro.motivo}. Variacao apenas "
                f"parametrica ou textual da mesma abordagem continua sendo "
                f"ela - o bloqueio olha a assinatura estrutural, e nao o "
                f"`content_hash`. A volta legitima e um reprojeto pela 0B"
            ),
            abordagem=registro.assinatura,
        )

    # E a recusa que vale para todo o resto. Ela e o CORPO da funcao, e nao um
    # `if` sobre a fase: quando a 0C acabar, alguem tem de vir aqui escrever o
    # caminho de admissao e decidir o que ele exige.
    return Recusa(
        motivo=(
            "NENHUMA CANDIDATA ENTRA NO FORWARD DA 0C (D38, ADR 0034). §14.2 "
            "previa a candidata retrospectiva como insumo da fase, e o Portao "
            "B rejeitou a unica que existia. Candidata nova exige reprojeto "
            "com pre-registro feito ANTES de olhar o forward, familia e "
            "creditos proprios - e isso passa pela 0B, nunca direto por aqui"
        ),
        abordagem=abordagem_mod.assinatura_de(params, extras),
    )


def exigir_nao_admitida(
    conn: sqlite3.Connection, *, hypothesis_id: int, params: dict,
    extras: dict | None = None, agent_id: str,
) -> None:
    """A versao que levanta, para quem chama de dentro de um caminho de escrita."""
    recusa = admitir(
        conn, hypothesis_id=hypothesis_id, params=params, extras=extras,
        agent_id=agent_id,
    )
    raise NaoAdmitida(recusa.motivo)
