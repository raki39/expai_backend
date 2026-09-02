"""Download e leitura dos dumps publicos da Binance.

Verificado por download real (ver `.aprendizado/binance-dados-notas.md`).
Esta camada NAO toca o banco e NAO decide nada do experimento: ela busca
bytes, confere integridade contra a origem e devolve barras normalizadas.

Tres fatos da fonte que o codigo aqui existe para tratar:

1. A unidade do timestamp muda de milissegundos para MICROSSEGUNDOS em
   2025-01, dentro da janela decidida (2024-08 a 2026-08). Normalizamos tudo
   para milissegundos.

2. Precos e volumes vem como STRING decimal ("93576.00000000"), nunca como
   numero. Isso permite ir direto para inteiro de precisao fixa sem passar
   por ponto flutuante em momento nenhum (regra 5).

3. O `.CHECKSUM` publicado pela Binance confere com o arquivo. Ele prova que
   baixamos o arquivo CERTO - garantia diferente da que o nosso proprio hash
   da, que e a de que o arquivo nao mudou depois que o pegamos.
"""

from __future__ import annotations

import hashlib
import io
import logging
import socket
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterator, NamedTuple

log = logging.getLogger(__name__)

BASE = "https://data.binance.vision/data/spot/monthly/klines"

# Expoente decimal dos inteiros gravados. Os dumps trazem 8 casas.
PRICE_SCALE_EXP = 8
VOLUME_SCALE_EXP = 8
PRICE_SCALE = 10**PRICE_SCALE_EXP
VOLUME_SCALE = 10**VOLUME_SCALE_EXP

TIMEOUT_S = 60.0
USER_AGENT = "fase0a-ingestao/1.0 (+fase-0a; uso academico)"

# Acima disto, o inteiro so pode estar em microssegundos: em milissegundos
# 1e14 corresponde ao ano 5138, e em microssegundos, a 1973.
LIMIAR_MICROSSEGUNDOS = 10**14

# Faixa de sanidade para o timestamp ja normalizado (2015 a 2100). Serve para
# transformar "unidade interpretada errado" em erro alto, e nao em barra
# silenciosamente datada de 1970.
MS_MIN = 1_420_070_400_000
MS_MAX = 4_102_444_800_000


class ErroDeFonte(Exception):
    """Qualquer falha ao obter dado da origem."""


class BloqueioPorJurisdicao(ErroDeFonte):
    """HTTP 451.

    Erro com NOME proprio, e nao falha generica de rede, porque a decisao
    (ADR 0012) depende de distinguir os dois: a medicao que autorizou ingerir
    na Railway vale para o dia em que foi feita, e bloqueio por jurisdicao e
    politica, que muda sem aviso. Se um dia acontecer, tem de ficar obvio.
    """


class ChecksumDivergente(ErroDeFonte):
    """O SHA-256 baixado nao bate com o publicado pela Binance."""


class DadosInconsistentes(ErroDeFonte):
    """O conteudo nao satisfaz uma invariante que deveria valer sempre."""


class Barra(NamedTuple):
    """Uma barra OHLCV ja normalizada. Todos os campos sao inteiros."""

    open_time_ms: int
    open: int
    high: int
    low: int
    close: int
    volume: int
    quote_volume: int
    trades: int


@dataclass(frozen=True)
class ArquivoBaixado:
    """Procedencia de um arquivo, para o registro do dataset."""

    mes: str
    url: str
    bytes_baixados: int
    sha256: str
    sha256_publicado: str | None
    barras: int


def intervalo_ms(timeframe: str) -> int:
    """"15m" -> 900000. Falha alto em timeframe que nao reconhece."""
    unidades = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    if len(timeframe) < 2 or timeframe[-1] not in unidades:
        raise ValueError(f"timeframe nao reconhecido: {timeframe!r}")
    try:
        quantidade = int(timeframe[:-1])
    except ValueError:
        raise ValueError(f"timeframe nao reconhecido: {timeframe!r}") from None
    if quantidade <= 0:
        raise ValueError(f"timeframe precisa ser positivo: {timeframe!r}")
    return quantidade * unidades[timeframe[-1]]


def meses_da_janela(inicio: date, fim: date) -> list[str]:
    """Meses "AAAA-MM" que cobrem [inicio, fim). O fim e exclusivo.

    A janela decidida termina em 2026-08-01, que significa "ate o fim de
    julho". Incluir agosto traria um mes que a decisao nao pediu - e que a
    origem ainda nem publicou (ADR 0013).
    """
    if inicio >= fim:
        raise ValueError("inicio precisa ser anterior a fim")
    meses: list[str] = []
    ano, mes = inicio.year, inicio.month
    # O ultimo mes necessario e o do instante imediatamente anterior a `fim`.
    ultimo = (fim.year, fim.month) if (fim.day > 1) else _mes_anterior(fim)
    while (ano, mes) <= ultimo:
        meses.append(f"{ano:04d}-{mes:02d}")
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return meses


def _mes_anterior(d: date) -> tuple[int, int]:
    return (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)


def url_mensal(symbol: str, timeframe: str, mes: str) -> str:
    return f"{BASE}/{symbol}/{timeframe}/{symbol}-{timeframe}-{mes}.zip"


def _buscar(url: str, *, timeout: float = TIMEOUT_S) -> bytes:
    requisicao = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(requisicao, timeout=timeout) as resposta:
            return resposta.read()
    except urllib.error.HTTPError as e:
        if e.code == 451:
            raise BloqueioPorJurisdicao(
                f"HTTP 451 em {url}. A origem esta bloqueando este ambiente "
                "por jurisdicao. O ADR 0012 previu esta possibilidade: a "
                "contingencia e ingerir localmente e subir o arquivo para o "
                "volume."
            ) from e
        raise ErroDeFonte(f"HTTP {e.code} em {url}: {e.reason}") from e
    except socket.timeout as e:
        raise ErroDeFonte(f"timeout de {timeout}s em {url}") from e
    except urllib.error.URLError as e:
        raise ErroDeFonte(f"falha de rede em {url}: {e.reason}") from e


def _sha256_publicado(texto: str) -> str:
    """Le o `.CHECKSUM`: "<sha256>  <nome do arquivo>"."""
    partes = texto.split()
    if not partes or len(partes[0]) != 64:
        raise ChecksumDivergente(f"CHECKSUM em formato inesperado: {texto[:80]!r}")
    return partes[0].lower()


def _decimal_para_inteiro(texto: str, escala: int, campo: str) -> int:
    """Converte a string decimal em inteiro de precisao fixa, SEM float.

    Recusa valor com mais casas do que a escala comporta, em vez de arredondar
    em silencio: perder precisao de preco caladamente e o tipo de defeito que
    so aparece no resultado do experimento, quando ja nao da para atribuir.
    """
    try:
        valor = Decimal(texto)
    except InvalidOperation:
        raise DadosInconsistentes(f"{campo}: {texto!r} nao e decimal") from None
    escalado = valor * escala
    if escalado != escalado.to_integral_value():
        raise DadosInconsistentes(
            f"{campo}: {texto!r} tem mais precisao do que a escala 1e-{escala} "
            "comporta; arredondar aqui seria perder preco em silencio"
        )
    return int(escalado)


def normalizar_timestamp(bruto: int) -> int:
    """Devolve milissegundos, venha o valor em ms ou em microssegundos.

    A Binance passou a publicar microssegundos a partir de 2025-01, e a janela
    decidida atravessa a mudanca. Interpretar a unidade errada joga a barra
    para 1970 ou para o ano ~57000 - e nao levanta excecao sozinha. Por isso o
    resultado passa por faixa de sanidade.
    """
    ms = bruto // 1000 if bruto >= LIMIAR_MICROSSEGUNDOS else bruto
    if not (MS_MIN <= ms <= MS_MAX):
        quando = "?"
        try:
            quando = datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            pass
        raise DadosInconsistentes(
            f"timestamp {bruto} normalizado para {ms} ms cai em {quando}, "
            "fora da faixa plausivel. Provavel unidade interpretada errado."
        )
    return ms


def ler_csv(texto: str, *, origem: str) -> Iterator[Barra]:
    """Converte o CSV do dump em barras normalizadas.

    Os arquivos verificados nao tem cabecalho, mas uma linha cujo primeiro
    campo nao seja inteiro e tratada como cabecalho e ignorada - a Binance ja
    mudou o formato uma vez.
    """
    for numero, linha in enumerate(texto.splitlines(), start=1):
        linha = linha.strip()
        if not linha:
            continue
        campos = linha.split(",")
        if len(campos) < 9:
            raise DadosInconsistentes(
                f"{origem}:{numero}: esperava >= 9 colunas, veio {len(campos)}"
            )
        try:
            bruto = int(campos[0])
        except ValueError:
            if numero == 1:
                log.info("dataset.cabecalho_ignorado", extra={"origem": origem})
                continue
            raise DadosInconsistentes(
                f"{origem}:{numero}: open time {campos[0]!r} nao e inteiro"
            ) from None

        yield Barra(
            open_time_ms=normalizar_timestamp(bruto),
            open=_decimal_para_inteiro(campos[1], PRICE_SCALE, f"{origem}:{numero} open"),
            high=_decimal_para_inteiro(campos[2], PRICE_SCALE, f"{origem}:{numero} high"),
            low=_decimal_para_inteiro(campos[3], PRICE_SCALE, f"{origem}:{numero} low"),
            close=_decimal_para_inteiro(campos[4], PRICE_SCALE, f"{origem}:{numero} close"),
            volume=_decimal_para_inteiro(
                campos[5], VOLUME_SCALE, f"{origem}:{numero} volume"
            ),
            quote_volume=_decimal_para_inteiro(
                campos[7], VOLUME_SCALE, f"{origem}:{numero} quote_volume"
            ),
            trades=int(campos[8]),
        )


def baixar_mes(
    symbol: str,
    timeframe: str,
    mes: str,
    *,
    conferir_checksum: bool = True,
) -> tuple[list[Barra], ArquivoBaixado]:
    """Baixa um mes, confere o checksum da origem e devolve as barras."""
    url = url_mensal(symbol, timeframe, mes)
    conteudo = _buscar(url)
    sha_local = hashlib.sha256(conteudo).hexdigest()

    sha_publicado: str | None = None
    if conferir_checksum:
        sha_publicado = _sha256_publicado(_buscar(url + ".CHECKSUM").decode("utf-8"))
        if sha_publicado != sha_local:
            raise ChecksumDivergente(
                f"{mes}: SHA-256 baixado {sha_local} difere do publicado "
                f"{sha_publicado}. Nao usar este arquivo."
            )

    try:
        with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
            nomes = z.namelist()
            if len(nomes) != 1:
                raise DadosInconsistentes(
                    f"{mes}: esperava 1 arquivo no zip, veio {len(nomes)}: {nomes}"
                )
            csv = z.read(nomes[0]).decode("utf-8")
    except zipfile.BadZipFile as e:
        raise DadosInconsistentes(f"{mes}: zip invalido") from e

    barras = list(ler_csv(csv, origem=f"{mes}"))
    if not barras:
        raise DadosInconsistentes(f"{mes}: nenhuma barra no arquivo")

    log.info(
        "dataset.mes_baixado",
        extra={
            "mes": mes,
            "bytes": len(conteudo),
            "barras": len(barras),
            "checksum_conferido": conferir_checksum,
        },
    )
    return barras, ArquivoBaixado(
        mes=mes,
        url=url,
        bytes_baixados=len(conteudo),
        sha256=sha_local,
        sha256_publicado=sha_publicado,
        barras=len(barras),
    )
