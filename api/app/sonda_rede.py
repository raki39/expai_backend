"""TEMPORARIO - sonda de rede do incremento 1. REMOVER apos a decisao D18.

Existe para responder UMA pergunta com fato, e nao com chute: a partir da
Railway, a Binance responde ou bloqueia?

A decisao D18 depende so disso:
  200/206 em data.binance.vision  ->  a ingestao roda NA Railway
  451                             ->  a ingestao roda LOCAL e o arquivo sobe
                                      para o volume

Por que a pergunta e real: a Binance responde 451 ("Unavailable For Legal
Reasons") para faixas de IP de certas jurisdicoes, e a Railway roda em nuvem
nos EUA. O comportamento a partir do Brasil (onde a Binance opera) NAO diz
nada sobre o comportamento a partir de um datacenter americano. Por isso a
sonda tem de rodar la, e nao aqui.

CRITERIO DE REMOCAO: este arquivo sai do repositorio no mesmo commit que
registra a decisao D18 com a evidencia colada no ADR. Nao e infraestrutura,
e um instrumento de medicao com prazo.

Escolhas deliberadas:

- urllib.request, da stdlib, e nao um cliente novo. Nao se adiciona
  dependencia de producao por causa de codigo que vai ser apagado. E a
  stdlib devolve o status cru, sem tratamento do cliente por cima.

- A sonda NUNCA levanta excecao. Uma sonda que estoura nao mede nada: um
  timeout, um DNS que falha e um 451 sao respostas DIFERENTES, e as tres
  precisam chegar distinguiveis ao painel.

- Le poucos bytes (Range), porque o objetivo e saber se alcanca, nao baixar.
"""

from __future__ import annotations

import logging
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends

from .security import exigir_token_de_servico

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/sonda",
    tags=["temporario"],
    dependencies=[Depends(exigir_token_de_servico)],
)

TIMEOUT_S = 15.0

# Cabecalhos que dizem algo sobre O CAMINHO ate o servidor: qual borda de CDN
# atendeu, se aceita Range, o que veio no corpo. O resto e ruido.
CABECALHOS_DE_INTERESSE = (
    "server",
    "date",
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "x-cache",
    "via",
    "x-amz-cf-pop",
    "x-amz-request-id",
    "cf-ray",
)

# Mes dentro da janela decidida (2024-09 a 2026-09) e fechado ha tempo, entao
# existe com certeza. A sonda nao pode falhar por escolha de data.
URL_DUMP = (
    "https://data.binance.vision/data/spot/monthly/klines"
    "/BTCUSDT/15m/BTCUSDT-15m-2025-01.zip"
)
URL_CHECKSUM = URL_DUMP + ".CHECKSUM"
URL_API_REST = (
    "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=15m&limit=1"
)


@dataclass(frozen=True)
class Alvo:
    """Um endereco a sondar e o motivo de ele importar."""

    nome: str
    url: str
    porque: str
    # Range mantem o trafego em bytes mesmo quando o alvo e um zip inteiro.
    # Um 206 de volta tambem confirma que da para retomar download interrompido.
    cabecalhos: dict[str, str] = field(default_factory=dict)
    ler_bytes: int = 512


ALVOS = (
    Alvo(
        nome="dump_zip",
        url=URL_DUMP,
        porque=(
            "fonte decidida do dataset (dumps publicos). E este alvo, e nao "
            "os outros, que decide a D18."
        ),
        cabecalhos={"Range": "bytes=0-511"},
    ),
    Alvo(
        nome="dump_checksum",
        url=URL_CHECKSUM,
        porque=(
            "SHA-256 publicado pela propria Binance para o mesmo arquivo; "
            "permite conferir o download contra a origem."
        ),
        ler_bytes=256,
    ),
    Alvo(
        nome="api_rest",
        url=URL_API_REST,
        porque=(
            "a API REST costuma ser bloqueada por jurisdicao antes do host de "
            "arquivos estaticos. Separa 'o IP esta bloqueado' de 'este host "
            "especifico esta bloqueado'."
        ),
        ler_bytes=256,
    ),
)


def _cabecalhos_uteis(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    return {
        chave: headers.get(chave)
        for chave in CABECALHOS_DE_INTERESSE
        if headers.get(chave) is not None
    }


def _amostra(corpo: bytes) -> str | None:
    """Primeiros bytes em forma legivel, para o corpo poder ser INSPECIONADO.

    Um 451 costuma trazer explicacao no corpo, e um zip comeca com 'PK'. Sem
    olhar o corpo, uma pagina de erro devolvida com 200 passaria por sucesso.
    """
    if not corpo:
        return None
    try:
        return corpo.decode("utf-8")[:400]
    except UnicodeDecodeError:
        return f"<binario> {corpo[:32].hex()}"


def sondar(alvo: Alvo, *, timeout: float = TIMEOUT_S) -> dict[str, Any]:
    """Faz uma requisicao e descreve o que voltou. Nunca levanta.

    O resultado distingue tres desfechos que sao facilmente confundidos:
      - `status` preenchido  -> o servidor RESPONDEU (inclusive 451)
      - `erro` = "timeout"   -> nao respondeu a tempo
      - `erro` = outro       -> nem chegou a falar HTTP (DNS, TLS, recusa)
    """
    requisicao = urllib.request.Request(
        alvo.url,
        method="GET",
        headers={
            # User-Agent explicito: alguns CDNs respondem diferente ao default
            # do urllib, e uma sonda que mente sobre si mesma mede a coisa
            # errada.
            "User-Agent": "fase0a-sonda/1.0 (+incremento-1; verificacao-de-acesso)",
            **alvo.cabecalhos,
        },
    )

    inicio = time.monotonic()
    resultado: dict[str, Any] = {
        "nome": alvo.nome,
        "url": alvo.url,
        "porque": alvo.porque,
        "status": None,
        "motivo": None,
        "bloqueado_por_jurisdicao": None,
        "cabecalhos": {},
        "amostra": None,
        "erro": None,
    }

    try:
        with urllib.request.urlopen(requisicao, timeout=timeout) as resposta:
            corpo = resposta.read(alvo.ler_bytes)
            resultado["status"] = resposta.status
            resultado["motivo"] = resposta.reason
            resultado["cabecalhos"] = _cabecalhos_uteis(resposta.headers)
            resultado["amostra"] = _amostra(corpo)
    except urllib.error.HTTPError as e:
        # HTTPError E a resposta. O 451 chega aqui, e e exatamente o que
        # queremos ler - por isso ele nao pode ser tratado como falha.
        # `except Exception` de proposito: um HTTPError sem corpo nem tem
        # metodo `read`, e um AttributeError aqui derrubaria a sonda
        # exatamente no caso que ela existe para medir - o 451.
        corpo = b""
        try:
            corpo = e.read(alvo.ler_bytes)
        except Exception:
            pass
        resultado["status"] = e.code
        resultado["motivo"] = e.reason
        resultado["cabecalhos"] = _cabecalhos_uteis(e.headers)
        resultado["amostra"] = _amostra(corpo)
    except socket.timeout:
        resultado["erro"] = "timeout"
    except urllib.error.URLError as e:
        # Nao houve HTTP: DNS, TLS ou recusa de conexao.
        resultado["erro"] = f"{type(e.reason).__name__}: {e.reason}"
    except Exception as e:  # a sonda nunca derruba a rota
        resultado["erro"] = f"{type(e).__name__}: {e}"

    resultado["duracao_ms"] = round((time.monotonic() - inicio) * 1000)
    if resultado["status"] is not None:
        resultado["bloqueado_por_jurisdicao"] = resultado["status"] == 451
    return resultado


def veredito(resultados: list[dict[str, Any]]) -> dict[str, Any]:
    """Traduz as sondas na decisao que elas suportam - e so nela."""
    por_nome = {r["nome"]: r for r in resultados}
    dump = por_nome.get("dump_zip", {})
    status = dump.get("status")

    if status in (200, 206):
        return {
            "acesso_ao_dump": "liberado",
            "decisao_d18": "ingestao roda NA Railway",
            "detalhe": (
                f"data.binance.vision respondeu {status} a partir deste "
                "ambiente."
            ),
        }
    if status == 451:
        return {
            "acesso_ao_dump": "bloqueado_por_jurisdicao",
            "decisao_d18": "ingestao roda LOCAL, arquivo sobe para o volume",
            "detalhe": (
                "data.binance.vision respondeu 451 (Unavailable For Legal "
                "Reasons) a partir deste ambiente."
            ),
        }
    if status is not None:
        return {
            "acesso_ao_dump": "resposta_inesperada",
            "decisao_d18": "indefinida - investigar antes de decidir",
            "detalhe": f"status {status} ({dump.get('motivo')}).",
        }
    return {
        "acesso_ao_dump": "sem_resposta",
        "decisao_d18": "indefinida - a sonda nao chegou a falar HTTP",
        "detalhe": str(dump.get("erro")),
    }


@router.get("/binance")
def sonda_binance() -> dict[str, Any]:
    """Sonda os tres alvos e devolve a evidencia crua mais o veredito.

    A evidencia crua vem junto de proposito: o veredito e uma leitura, e quem
    decide precisa poder conferir a leitura contra o que de fato voltou.
    """
    resultados = [sondar(alvo) for alvo in ALVOS]
    conclusao = veredito(resultados)

    log.info(
        "sonda.binance",
        extra={
            "veredito": conclusao["acesso_ao_dump"],
            "status": {r["nome"]: r["status"] for r in resultados},
            "erros": {r["nome"]: r["erro"] for r in resultados if r["erro"]},
        },
    )

    return {
        "temporario": (
            "Sonda do incremento 1. Removida quando a D18 for registrada."
        ),
        "veredito": conclusao,
        "sondas": resultados,
    }
