"""Qualidade do relogio local, MEDIDA contra a Binance.

ADR 0028. Uma versao anterior desta decisao afirmava que a deriva de NTP seria
"~0,1 s", deixando 20x de folga contra a tolerancia de 2 s. Isso era suposicao
vestida de fato, e o projeto recusa esse tipo de afirmacao: o numero passa a ser
observado, com carimbo, e a tolerancia e avaliada contra o medido.

**A referencia e o relogio da propria Binance**, e nao um servidor NTP
generico. E deliberado: e contra o relogio DELA que as cotacoes DELA sao
comparadas. Um relogio local perfeitamente sincronizado com o UTC do mundo, mas
deslocado do da exchange, produziria o mesmo erro de alinhamento - e um NTP
generico nao o veria.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

URL_TEMPO = "https://api.binance.com/api/v3/time"
USER_AGENT = "ecossistema-agentes-economicos/0c-coletor"
TIMEOUT_S = 10.0

# A cada 5 minutos. Peso 1 de rate limit - desprezivel contra qualquer teto.
INTERVALO_S = 300.0


class FalhaNaSonda(Exception):
    """Nao foi possivel medir. Vira telemetria, nunca um valor inventado."""


class BloqueioPorJurisdicao(FalhaNaSonda):
    """HTTP 451. NAO e transitorio, e por isso nao entra em backoff.

    Mesma familia do 400 que o incremento 11b separou dos transitorios:
    reenviar o mesmo pedido do mesmo lugar devolve a mesma resposta. Repetir
    nao conserta jurisdicao - so enche o log e esconde a causa.

    O `app/dataset/binance.py` ja tinha esta excecao para a ingestao historica.
    Ela nao foi conferida para `api.binance.com` e `stream.binance.com`, que
    sao hosts DIFERENTES do `data.binance.vision` medido no ADR 0012 - e a
    diferenca apareceu no primeiro deploy.
    """


@dataclass(frozen=True)
class Medida:
    """Uma estimativa de ida e volta.

    `offset_ms` positivo = o relogio da exchange esta ADIANTE do nosso.
    """

    medido_em_ns: int
    rtt_ms: float
    offset_ms: float
    server_time_ms: int

    def como_linha(self) -> dict[str, Any]:
        return {
            "tipo": "relogio",
            "medido_em_ns": self.medido_em_ns,
            "rtt_ms": round(self.rtt_ms, 3),
            "offset_ms": round(self.offset_ms, 3),
            "incerteza_bruta_ms": round(self.incerteza_bruta_ms(), 3),
            "incerteza_residual_ms": round(self.incerteza_residual_ms(), 3),
            "server_time_ms": self.server_time_ms,
        }

    def incerteza_bruta_ms(self) -> float:
        """Erro de alinhamento de quem IGNORA o offset.

        `|offset| + rtt/2`. E o numero que interessa saber para descobrir o
        tamanho do engano que se estaria cometendo sem corrigir - e, medido em
        maquina real, ele pode passar da tolerancia inteira do ADR 0027.
        """
        return abs(self.offset_ms) + self.rtt_ms / 2

    def incerteza_residual_ms(self) -> float:
        """O que SOBRA depois de corrigir pelo offset, e e irredutivel.

        Metade do RTT: a assimetria maxima possivel da viagem de ida e volta,
        que a medicao de ponto medio nao consegue distinguir.

        **A diferenca entre os dois numeros e o valor da medicao.** O offset e
        grande e CORRIGIVEL; a assimetria e pequena e nao e. Quem nao mede
        carrega o primeiro como se fosse zero.
        """
        return self.rtt_ms / 2


def _buscar(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def medir(
    *,
    agora_s: Callable[[], float] = time.time,
    agora_ns: Callable[[], int] = time.time_ns,
    buscar: Callable[[str, float], bytes] = _buscar,
    timeout: float = TIMEOUT_S,
) -> Medida:
    """Estimativa classica de ida e volta, do mesmo formato do NTP.

        t0 = envio local    t1 = recebimento local    S = serverTime
        rtt    = t1 - t0
        offset = S - (t0 + t1)/2

    O ponto medio supoe simetria da viagem. Ela nao e garantida, e e por isso
    que `incerteza_ms` soma `rtt/2`: e o pior caso da assimetria.
    """
    t0 = agora_s()
    try:
        bruto = buscar(URL_TEMPO, timeout)
    except urllib.error.HTTPError as e:
        if e.code == 451:
            raise BloqueioPorJurisdicao(
                f"HTTP 451 em {URL_TEMPO}: a Binance recusa este ambiente por "
                f"jurisdicao. NAO adianta repetir. Acao: trocar a regiao do "
                f"servico na Railway, ou hospedar o coletor fora dela."
            ) from e
        raise FalhaNaSonda(f"HTTP {e.code} em {URL_TEMPO}: {e.reason}") from e
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise FalhaNaSonda(f"nao foi possivel alcancar {URL_TEMPO}: {e}") from e
    t1 = agora_s()

    try:
        s = int(json.loads(bruto)["serverTime"])
    except (ValueError, KeyError, TypeError) as e:
        raise FalhaNaSonda(f"resposta inesperada de {URL_TEMPO}: {bruto[:120]!r}") from e

    t0_ms, t1_ms = t0 * 1000.0, t1 * 1000.0
    return Medida(
        medido_em_ns=agora_ns(),
        rtt_ms=t1_ms - t0_ms,
        offset_ms=s - (t0_ms + t1_ms) / 2,
        server_time_ms=s,
    )
