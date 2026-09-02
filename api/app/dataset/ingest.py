"""Ingestao unica do dataset, com relatorio de integridade.

Roda na propria Railway (ADR 0012): sem passo manual, sem arquivo carregado a
mao. O dataset existe porque um comando reproduzivel o produziu - que e do que
a regra 13 precisa.

Duas coisas que este modulo se recusa a fazer em silencio:

- **Aceitar lacuna.** O criterio 3 do incremento 1 diz que o relatorio "e
  aceito ou a janela e ajustada - nao se ignora lacuna". Entao lacuna aborta a
  ingestao, a menos que alguem declare que a aceita. A decisao e de uma
  pessoa, e fica registrada.

- **Sobrescrever.** Reingestao com os mesmos dados e no-op; reingestao com
  dados diferentes e erro alto. Um dataset "fixado" que pode ser trocado por
  baixo nao esta fixado.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Sequence

from ..config.schema import ExperimentConfig
from .binance import (
    BASE,
    PRICE_SCALE_EXP,
    VOLUME_SCALE_EXP,
    ArquivoBaixado,
    Barra,
    DadosInconsistentes,
    baixar_mes,
    intervalo_ms,
    meses_da_janela,
)

log = logging.getLogger(__name__)

# Quantas lacunas e barras de volume zero listar em detalhe. O resumo traz
# sempre a contagem total; a lista existe para inspecao, nao para completude.
LIMITE_DETALHE = 50


class DatasetJaExiste(Exception):
    """Reingestao identica. Nao e erro - e o resultado esperado (criterio 2)."""


class DivergenciaNaReingestao(Exception):
    """Mesma janela, dados diferentes. Sempre erro."""


class LacunasNaoAceitas(Exception):
    """Ha lacunas e ninguem as aceitou explicitamente."""

    def __init__(self, mensagem: str, relatorio: "RelatorioIntegridade"):
        super().__init__(mensagem)
        self.relatorio = relatorio


@dataclass(frozen=True)
class Lacuna:
    """Um buraco na serie: barras ausentes entre duas barras presentes."""

    apos_ms: int
    antes_ms: int
    barras_faltando: int

    @property
    def duracao_ms(self) -> int:
        return self.antes_ms - self.apos_ms

    def como_dict(self) -> dict:
        return {
            "apos_ms": self.apos_ms,
            "antes_ms": self.antes_ms,
            "apos_utc": _iso(self.apos_ms),
            "antes_utc": _iso(self.antes_ms),
            "barras_faltando": self.barras_faltando,
            "duracao_ms": self.duracao_ms,
            "duracao_horas": round(self.duracao_ms / 3_600_000, 2),
        }


@dataclass(frozen=True)
class RelatorioIntegridade:
    """Criterio 3: esperado contra obtido, lacunas e volume zero."""

    barras_esperadas: int
    barras_obtidas: int
    lacunas: list[Lacuna]
    barras_volume_zero: list[int]
    primeira_ms: int
    ultima_ms: int

    @property
    def completo(self) -> bool:
        return not self.lacunas and self.barras_obtidas == self.barras_esperadas

    def como_dict(self) -> dict:
        return {
            "completo": self.completo,
            "barras_esperadas": self.barras_esperadas,
            "barras_obtidas": self.barras_obtidas,
            "barras_faltando": self.barras_esperadas - self.barras_obtidas,
            "primeira_utc": _iso(self.primeira_ms),
            "ultima_utc": _iso(self.ultima_ms),
            "lacunas": {
                "total": len(self.lacunas),
                "barras_faltando": sum(l.barras_faltando for l in self.lacunas),
                "detalhe": [l.como_dict() for l in self.lacunas[:LIMITE_DETALHE]],
                "detalhe_truncado": len(self.lacunas) > LIMITE_DETALHE,
            },
            "volume_zero": {
                # Nao e erro: barra sem negocio existe. E sinal de que aquele
                # trecho nao sustenta conclusao, entao vai declarado.
                "total": len(self.barras_volume_zero),
                "detalhe_utc": [
                    _iso(ms) for ms in self.barras_volume_zero[:LIMITE_DETALHE]
                ],
                "detalhe_truncado": len(self.barras_volume_zero) > LIMITE_DETALHE,
            },
        }


@dataclass(frozen=True)
class ResultadoIngestao:
    dataset_id: int
    ja_existia: bool
    sha256: str
    barras: int
    start_ms: int
    end_ms: int
    reserved_from_ms: int
    relatorio: RelatorioIntegridade
    arquivos: list[ArquivoBaixado] = field(default_factory=list)

    def como_dict(self) -> dict:
        return {
            "dataset_id": self.dataset_id,
            "ja_existia": self.ja_existia,
            "sha256": self.sha256,
            "barras": self.barras,
            "janela_utc": [_iso(self.start_ms), _iso(self.end_ms)],
            "reservado_a_partir_de_utc": _iso(self.reserved_from_ms),
            "relatorio_integridade": self.relatorio.como_dict(),
            "arquivos": [
                {
                    "mes": a.mes,
                    "barras": a.barras,
                    "sha256": a.sha256,
                    "checksum_conferido": a.sha256_publicado is not None,
                }
                for a in self.arquivos
            ],
        }


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _ms_da_data(texto: str) -> int:
    d = date.fromisoformat(texto)
    return int(
        datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000
    )


def hash_dos_dados(barras: Sequence[Barra]) -> str:
    """SHA-256 da serie normalizada - dos DADOS, nao dos bytes baixados.

    Hashear o zip seria fragil: recompactacao na origem mudaria o hash sem
    mudar barra nenhuma, e a reingestao passaria a acusar divergencia falsa.
    Aqui, hash igual significa serie igual, que e o que a regra 13 quer dizer.
    """
    h = hashlib.sha256()
    for b in barras:
        h.update(
            f"{b.open_time_ms},{b.open},{b.high},{b.low},{b.close},"
            f"{b.volume},{b.quote_volume},{b.trades}\n".encode("ascii")
        )
    return h.hexdigest()


def analisar(barras: Sequence[Barra], intervalo: int) -> RelatorioIntegridade:
    """Confere as invariantes da serie e monta o relatorio.

    A invariante que importa e a diferenca entre barras consecutivas ser
    EXATAMENTE o intervalo. Contar linhas nao detectaria a troca de unidade de
    timestamp da Binance - as contagens estao certas nos dois formatos.
    """
    if not barras:
        raise DadosInconsistentes("nenhuma barra para analisar")

    lacunas: list[Lacuna] = []
    volume_zero: list[int] = []

    if barras[0].volume == 0:
        volume_zero.append(barras[0].open_time_ms)

    for anterior, atual in zip(barras, barras[1:]):
        delta = atual.open_time_ms - anterior.open_time_ms

        if delta == intervalo:
            pass
        elif delta <= 0:
            raise DadosInconsistentes(
                f"barras fora de ordem ou duplicadas em {_iso(atual.open_time_ms)}: "
                f"delta {delta} ms"
            )
        elif delta % intervalo != 0:
            # Desalinhamento nao e lacuna: e sinal de que a grade esta errada,
            # o que costuma significar unidade de timestamp trocada. Sempre
            # fatal - aceitar isto seria aceitar um dataset que nao e o que diz.
            raise DadosInconsistentes(
                f"barra desalinhada da grade de {intervalo} ms em "
                f"{_iso(atual.open_time_ms)}: delta {delta} ms nao e multiplo. "
                "Suspeita principal: unidade de timestamp interpretada errado."
            )
        else:
            lacunas.append(
                Lacuna(
                    apos_ms=anterior.open_time_ms,
                    antes_ms=atual.open_time_ms,
                    barras_faltando=delta // intervalo - 1,
                )
            )

        if atual.volume == 0:
            volume_zero.append(atual.open_time_ms)

    span = barras[-1].open_time_ms - barras[0].open_time_ms
    esperadas = span // intervalo + 1

    return RelatorioIntegridade(
        barras_esperadas=esperadas,
        barras_obtidas=len(barras),
        lacunas=lacunas,
        barras_volume_zero=volume_zero,
        primeira_ms=barras[0].open_time_ms,
        ultima_ms=barras[-1].open_time_ms,
    )


def ingerir(
    conn: sqlite3.Connection,
    config: ExperimentConfig,
    *,
    aceitar_lacunas: bool = False,
    conferir_checksum: bool = True,
    baixador: Callable[..., tuple[list[Barra], ArquivoBaixado]] | None = None,
) -> ResultadoIngestao:
    """Ingestao unica e idempotente da janela decidida.

    `baixador` e injetavel para que os testes nao dependam da rede. Resolvido
    aqui dentro, e nao como valor padrao do parametro: valor padrao e ligado
    na DEFINICAO da funcao, o que impediria substituir `baixar_mes` para
    exercitar a rota HTTP de ponta a ponta.
    """
    baixador = baixador or baixar_mes
    intervalo = intervalo_ms(config.timeframe)
    inicio_ms = _ms_da_data(config.data_start)
    fim_ms = _ms_da_data(config.data_end)  # exclusivo

    meses = meses_da_janela(
        date.fromisoformat(config.data_start), date.fromisoformat(config.data_end)
    )
    log.info(
        "dataset.ingestao_iniciada",
        extra={
            "symbol": config.market_symbol,
            "timeframe": config.timeframe,
            "meses": len(meses),
            "janela": [config.data_start, config.data_end],
        },
    )

    barras: list[Barra] = []
    arquivos: list[ArquivoBaixado] = []
    for mes in meses:
        do_mes, info = baixador(
            config.market_symbol,
            config.timeframe,
            mes,
            conferir_checksum=conferir_checksum,
        )
        barras.extend(do_mes)
        arquivos.append(info)

    # A janela manda, e nao o recorte dos arquivos: o mes de borda traz barras
    # fora dela.
    barras = sorted(
        (b for b in barras if inicio_ms <= b.open_time_ms < fim_ms),
        key=lambda b: b.open_time_ms,
    )
    if not barras:
        raise DadosInconsistentes(
            f"nenhuma barra dentro da janela {config.data_start} a {config.data_end}"
        )

    relatorio = analisar(barras, intervalo)
    if not relatorio.completo and not aceitar_lacunas:
        raise LacunasNaoAceitas(
            f"{len(relatorio.lacunas)} lacuna(s), "
            f"{relatorio.barras_esperadas - relatorio.barras_obtidas} barra(s) "
            "faltando. O criterio 3 nao permite ignorar: aceite explicitamente "
            "ou ajuste a janela.",
            relatorio,
        )

    sha = hash_dos_dados(barras)
    start_ms = barras[0].open_time_ms
    end_ms = barras[-1].open_time_ms

    # ------------------------------------------------------- reserva (D11)
    corte = int(len(barras) * (1 - float(config.reserved_fraction)))
    if not (0 < corte < len(barras)):
        raise DadosInconsistentes(
            f"reserved_fraction {config.reserved_fraction} nao deixa barra "
            f"utilizavel em {len(barras)} barras"
        )
    reserved_from_ms = barras[corte].open_time_ms

    # --------------------------------------------------- idempotencia (2)
    existente = conn.execute(
        "SELECT id, sha256, bars FROM dataset"
        " WHERE venue=? AND symbol=? AND timeframe=? AND start_ms=? AND end_ms=?",
        (
            config.market_venue,
            config.market_symbol,
            config.timeframe,
            start_ms,
            end_ms,
        ),
    ).fetchone()

    if existente is not None:
        if existente["sha256"] != sha:
            raise DivergenciaNaReingestao(
                f"dataset {existente['id']} ja existe para esta janela com "
                f"sha256 {existente['sha256']}, mas a reingestao produziu {sha}. "
                "Os dados da origem mudaram. Nada foi sobrescrito - decida "
                "explicitamente o que fazer."
            )
        log.info(
            "dataset.reingestao_identica",
            extra={"dataset_id": existente["id"], "sha256": sha},
        )
        return ResultadoIngestao(
            dataset_id=int(existente["id"]),
            ja_existia=True,
            sha256=sha,
            barras=len(barras),
            start_ms=start_ms,
            end_ms=end_ms,
            reserved_from_ms=reserved_from_ms,
            relatorio=relatorio,
            arquivos=arquivos,
        )

    # -------------------------------------------------------- persistencia
    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            """
            INSERT INTO dataset (
                venue, symbol, timeframe, interval_ms,
                start_ms, end_ms, reserved_from_ms, bars,
                sha256, source, source_files_json, fetched_at,
                fidelity_level, price_scale_exp, volume_scale_exp
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                config.market_venue,
                config.market_symbol,
                config.timeframe,
                intervalo,
                start_ms,
                end_ms,
                reserved_from_ms,
                len(barras),
                sha,
                BASE,
                json.dumps(
                    [
                        {
                            "mes": a.mes,
                            "url": a.url,
                            "bytes": a.bytes_baixados,
                            "sha256": a.sha256,
                            "sha256_publicado": a.sha256_publicado,
                            "barras": a.barras,
                        }
                        for a in arquivos
                    ],
                    ensure_ascii=False,
                ),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                config.fidelity_level,
                PRICE_SCALE_EXP,
                VOLUME_SCALE_EXP,
            ),
        )
        dataset_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO bar (dataset_id, open_time_ms, open, high, low, close,"
            " volume, quote_volume, trades) VALUES (?,?,?,?,?,?,?,?,?)",
            [(dataset_id, *b) for b in barras],
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        log.exception("dataset.ingestao_falhou")
        raise

    log.info(
        "dataset.ingerido",
        extra={
            "dataset_id": dataset_id,
            "barras": len(barras),
            "sha256": sha,
            "reserved_from": _iso(reserved_from_ms),
            "lacunas": len(relatorio.lacunas),
            "fidelity_level": config.fidelity_level,
        },
    )
    return ResultadoIngestao(
        dataset_id=dataset_id,
        ja_existia=False,
        sha256=sha,
        barras=len(barras),
        start_ms=start_ms,
        end_ms=end_ms,
        reserved_from_ms=reserved_from_ms,
        relatorio=relatorio,
        arquivos=arquivos,
    )
