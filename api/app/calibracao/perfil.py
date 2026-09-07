"""Perfil de calibracao: override POR REGIME. ADR 0033.

A frase que corrigiu o desenho anterior, e ela e do usuario:

    "Usar o maior valor observado nao significa ser pessimista onde nada foi
     observado. A etiqueta de 'nao calibrado' nao corrigiria o preco usado."

A parte 2c aplicava, num campo unico, o valor do regime mais pessimista entre
os calibrados. Mas o maximo sobre uma amostra de regimes **nao e limite
superior** para os regimes fora dela - e `vol_alta` e, por construcao da
taxonomia, o tercil de MAIOR volatilidade, onde o custo de execucao tende a
ser pior. Aplicar ali um numero medido no tercil mais calmo e otimismo com
aparencia de conservadorismo.

## A resolucao, e o passo 3 e o ponto inteiro

    1. classificar o regime de `t`, CAUSALMENTE, com a janela [t-672, t-1]
    2. ha override para esse regime?   -> usa o override
    3. nao ha, ou o regime e INDEFINIDO? -> usa o valor-BASE, e marca
                                            `nao_calibrado`

O passo 3 usa a **base**, e nao o override de outro regime.

## A ausencia de linha E a informacao

Um regime sem override nao tem linha em `calibracao_perfil_regime`. Gravar uma
linha com valor "herdado" tornaria a heranca invisivel; a ausencia a torna
impossivel - e ha `CHECK` proibindo linha para `indefinido`, que nao e regime
da taxonomia.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from ..regime import deteccao

log = logging.getLogger(__name__)

REGIMES_DA_TAXONOMIA = ("vol_baixa", "vol_media", "vol_alta")


class RegimeInvalido(Exception):
    """`indefinido` nao e regime da taxonomia, e nao pode ter override."""


@dataclass(frozen=True)
class Override:
    regime: str
    spread_bps_x1000: int
    n: int
    p10_mili_bps: int


@dataclass(frozen=True)
class Perfil:
    hash: str
    spread_bps_base_x1000: int
    overrides: dict[str, Override]
    taxonomia: dict[str, int]

    def spread_bps_x1000(self, regime: str | None) -> tuple[int, bool]:
        """O `spread_bps` efetivo do instante, e se ele foi CALIBRADO.

        `regime` vem do detector e pode ser `None` (indefinido). Nos dois
        casos sem override, devolve a BASE - nunca o override de outro regime.
        """
        if regime is not None and regime in self.overrides:
            return self.overrides[regime].spread_bps_x1000, True
        return self.spread_bps_base_x1000, False


def _agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def calcular_hash(
    *, spread_bps_base_x1000: int, overrides: list[Override],
) -> str:
    """`sha256` sobre a taxonomia declarada e os overrides ORDENADOS.

    A taxonomia entra porque, se os cortes da D40 mudassem, `vol_alta`
    passaria a nomear outra coisa e o override deixaria de descrever o que
    descrevia. Sem ela no hash, o perfil sobreviveria a uma mudanca que o
    invalida - e sobreviveria calado.

    Ordenado porque conjunto nao tem ordem e hash tem: sem `sort_keys`, dois
    perfis identicos teriam hashes diferentes conforme a ordem de insercao.
    """
    corpo = {
        "taxonomia": {
            "corte_inferior_mili_bps": deteccao.CORTE_INFERIOR_MILI_BPS,
            "corte_superior_mili_bps": deteccao.CORTE_SUPERIOR_MILI_BPS,
            "janela_barras": deteccao.JANELA_BARRAS,
            "permanencia_barras": deteccao.PERMANENCIA_BARRAS,
        },
        "spread_bps_base_x1000": spread_bps_base_x1000,
        "overrides": {
            o.regime: {
                "spread_bps_x1000": o.spread_bps_x1000,
                "n": o.n,
                "p10_mili_bps": o.p10_mili_bps,
            }
            for o in sorted(overrides, key=lambda x: x.regime)
        },
    }
    canonico = json.dumps(corpo, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def gravar(
    conn: sqlite3.Connection, *, spread_bps_base_x1000: int,
    overrides: list[Override],
) -> Perfil:
    """Grava o perfil. Idempotente pelo hash: mesmo conteudo, mesmo perfil.

    Um override para `indefinido` e RECUSADO aqui e no banco. Ele nao e um
    quarto regime - e a declaracao de que faltam 672 barras anteriores para
    classificar, e dar-lhe parametro seria calibrar uma classe que nao existe.
    """
    for o in overrides:
        if o.regime not in REGIMES_DA_TAXONOMIA:
            raise RegimeInvalido(
                f"{o.regime!r} nao e regime da taxonomia {REGIMES_DA_TAXONOMIA}. "
                f"`indefinido` nunca pode herdar parametro de outro regime"
            )

    h = calcular_hash(
        spread_bps_base_x1000=spread_bps_base_x1000, overrides=overrides
    )
    existente = ler(conn, h)
    if existente is not None:
        return existente

    conn.execute(
        "INSERT INTO calibracao_perfil (hash,"
        " taxonomia_corte_inferior_mili_bps, taxonomia_corte_superior_mili_bps,"
        " taxonomia_janela_barras, taxonomia_permanencia_barras,"
        " spread_bps_base_x1000, criado_em)"
        " VALUES (?,?,?,?,?,?,?)",
        (h, deteccao.CORTE_INFERIOR_MILI_BPS, deteccao.CORTE_SUPERIOR_MILI_BPS,
         deteccao.JANELA_BARRAS, deteccao.PERMANENCIA_BARRAS,
         spread_bps_base_x1000, _agora()),
    )
    for o in sorted(overrides, key=lambda x: x.regime):
        conn.execute(
            "INSERT INTO calibracao_perfil_regime (perfil_hash, regime,"
            " spread_bps_x1000, n, p10_mili_bps) VALUES (?,?,?,?,?)",
            (h, o.regime, o.spread_bps_x1000, o.n, o.p10_mili_bps),
        )

    log.info("calibracao.perfil_gravado", extra={
        "hash": h[:16], "base_x1000": spread_bps_base_x1000,
        "regimes_com_override": [o.regime for o in overrides],
        "regimes_na_base": [
            r for r in REGIMES_DA_TAXONOMIA
            if r not in {o.regime for o in overrides}
        ],
    })
    return ler(conn, h)  # type: ignore[return-value]


def ler(conn: sqlite3.Connection, perfil_hash: str) -> Perfil | None:
    linha = conn.execute(
        "SELECT hash, spread_bps_base_x1000,"
        "       taxonomia_corte_inferior_mili_bps AS corte_inferior,"
        "       taxonomia_corte_superior_mili_bps AS corte_superior,"
        "       taxonomia_janela_barras AS janela,"
        "       taxonomia_permanencia_barras AS permanencia"
        "  FROM calibracao_perfil WHERE hash = ?",
        (perfil_hash,),
    ).fetchone()
    if linha is None:
        return None
    overrides = {
        r["regime"]: Override(
            regime=r["regime"], spread_bps_x1000=int(r["spread_bps_x1000"]),
            n=int(r["n"]), p10_mili_bps=int(r["p10_mili_bps"]),
        )
        for r in conn.execute(
            "SELECT regime, spread_bps_x1000, n, p10_mili_bps"
            "  FROM calibracao_perfil_regime WHERE perfil_hash = ?"
            " ORDER BY regime",
            (perfil_hash,),
        )
    }
    return Perfil(
        hash=str(linha["hash"]),
        spread_bps_base_x1000=int(linha["spread_bps_base_x1000"]),
        overrides=overrides,
        taxonomia={
            "corte_inferior_mili_bps": int(linha["corte_inferior"]),
            "corte_superior_mili_bps": int(linha["corte_superior"]),
            "janela_barras": int(linha["janela"]),
            "permanencia_barras": int(linha["permanencia"]),
        },
    )


def da_config_version(
    conn: sqlite3.Connection, config_version_id: int
) -> Perfil | None:
    """O perfil vinculado a uma `config_version`, ou `None`.

    `None` nas versoes antigas, e e assim de proposito: o vinculo e uma COLUNA
    da tabela e nao um campo do payload, entao o `config_hash` das versoes ja
    gravadas fica exatamente como sempre foi.

    Sem perfil, o simulador usa `config.spread_bps` como sempre usou - nenhum
    caminho historico muda de comportamento, e R12 continua valendo.
    """
    linha = conn.execute(
        "SELECT calibracao_perfil_hash FROM config_version WHERE id = ?",
        (config_version_id,),
    ).fetchone()
    if linha is None or linha["calibracao_perfil_hash"] is None:
        return None
    return ler(conn, str(linha["calibracao_perfil_hash"]))


# NAO HA `vincular`, e a ausencia foi descoberta implementando.
#
# Eu havia escrito uma funcao que fazia `UPDATE config_version SET
# calibracao_perfil_hash`, com uma checagem recusando a troca. Ela NUNCA
# poderia funcionar: `config_version` e imutavel por gatilho, e o `UPDATE` e
# recusado pelo banco antes de a checagem significar coisa alguma.
#
# Uma funcao que nao tem caminho e a forma do `BLOCOS`: declarada, com um
# comentario afirmando o que ela protege, e sem nada por tras. O vinculo e
# feito no INSERT, por `config_service.criar_versao`, e "nao se troca o perfil
# de uma versao" e verdade porque a LINHA nao muda - nao porque alguem confere.

# ===========================================================================
# O que um resultado pode afirmar
# ===========================================================================


@dataclass(frozen=True)
class Fidelidade:
    conclusiva: bool
    regimes_tocados: list[str]
    regimes_calibrados: list[str]
    regimes_nao_calibrados: list[str]
    motivo: str


def fidelidade(
    perfil: Perfil | None, regimes_tocados: list[str | None]
) -> Fidelidade:
    """Um resultado que atravessa regime nao calibrado fica INCONCLUSIVO.

    Nao reprovado, nao aprovado - a mesma forma que §8.4.1.1 da ao nivel de
    fidelidade: *"o simulador declara seu nivel, e o nivel vira condicao de
    validade do conhecimento"*.

    E o shadow **continua rodando** num regime nao calibrado: acumular
    evidencia ali e exatamente como ele deixa de ser nao calibrado, e parar
    garantiria que ele nunca saisse desse estado.
    """
    nomes = sorted({r if r is not None else "indefinido" for r in regimes_tocados})
    overrides = set(perfil.overrides) if perfil is not None else set()
    calibrados = sorted(n for n in nomes if n in overrides)
    faltando = sorted(n for n in nomes if n not in overrides)

    if not nomes:
        return Fidelidade(
            conclusiva=False, regimes_tocados=[], regimes_calibrados=[],
            regimes_nao_calibrados=[],
            motivo="nenhum instante classificado: nao ha o que afirmar",
        )
    if not faltando:
        return Fidelidade(
            conclusiva=True, regimes_tocados=nomes,
            regimes_calibrados=calibrados, regimes_nao_calibrados=[],
            motivo="todo regime tocado tem override calibrado",
        )
    return Fidelidade(
        conclusiva=False, regimes_tocados=nomes,
        regimes_calibrados=calibrados, regimes_nao_calibrados=faltando,
        motivo=(
            f"o periodo atravessa regime(s) NAO CALIBRADO(S): "
            f"{', '.join(faltando)}. Ali o simulador usou o valor-BASE, que e "
            f"hipotese e nao medicao - entao o resultado e INCONCLUSIVO quanto "
            f"a fidelidade. O shadow segue rodando para acumular evidencia"
        ),
    )
