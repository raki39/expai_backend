"""O laco que extrai e entrega. ADR 0032, incremento 18.

Roda ao lado da amostragem, e **nunca dentro dela**: a coleta a 1 Hz e a coisa
irrecuperavel do projeto, e nada que possa bloquear pode morar no caminho
dela. Por isso a extracao roda em thread separada (`asyncio.to_thread`) e a
falha dela nunca derruba o laco.

## Ate onde extrair, e por que nao ate agora

A regra do ADR 0032 e "a ULTIMA amostra com corrigido <= t_grid". Para um
instante `t` estar RESOLVIDO, e preciso ja existir amostra depois dele - senao
a proxima que chegar poderia ser a candidata certa, e a extracao teria
concluido cedo demais.

Entao a fronteira e o inicio da barra corrente: extrai-se `[retomada, agora
truncado a grade)`. Isso deixa no maximo uma barra de atraso, e a calibracao
nao e tempo real - ela e uma conta sobre periodo fechado.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from . import envio, extracao
from .arquivo import selar_dias_fechados

log = logging.getLogger("coletor")

# De quanto em quanto tempo tentar entregar. Um terco da grade, pelo mesmo
# argumento do rele: a barra fecha uma vez por intervalo, e checar tres vezes
# a apanha logo depois sem depender de o relogio estar alinhado.
SEGUNDOS_ENTRE_VOLTAS = extracao.GRADE_MS / 1000 / 3

# Teto por lote, o mesmo da rota.
MAX_LOTE = 500

# Quanto olhar para tras quando NAO ha ponto de retomada (fluxo vazio). Sete
# dias cobrem o inicio da coleta em 2026-09-04 sem varrer o volume inteiro a
# cada boot.
DIAS_INICIAIS = 7
MS_POR_DIA = 86_400_000


def _dias_utc(de_ms: int, ate_ms: int) -> list[str]:
    dia = de_ms // MS_POR_DIA * MS_POR_DIA
    saida = []
    while dia <= ate_ms:
        saida.append(
            datetime.fromtimestamp(dia / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        )
        dia += MS_POR_DIA
    return saida


def arquivos_do_periodo(
    diretorio: Path, prefixo: str, de_ms: int, ate_ms: int
) -> list[Path]:
    """So os arquivos dos dias que o periodo toca.

    Ler o diretorio inteiro funcionaria e ficaria mais lento a cada mes de
    coleta - e um laco que fica mais lento para sempre e um defeito com data
    marcada.
    """
    caminhos = [
        diretorio / f"{prefixo}-{dia}.jsonl.gz" for dia in _dias_utc(de_ms, ate_ms)
    ]
    return [c for c in caminhos if c.exists()]


def _primeiro_dia_com_arquivo(diretorio: Path, prefixo: str) -> int | None:
    """Meia-noite UTC do dia mais antigo com arquivo, em ms.

    O dia, e nao a primeira linha: ler o arquivo inteiro so para descobrir o
    primeiro carimbo custaria uma descompressao a cada volta. O dia ja tira
    fora tudo o que e anterior ao coletor, e o que sobra e no maximo a rampa
    de um dia - que e genuinamente ambigua e fica registrada como tal.
    """
    dias = sorted(
        p.name[len(prefixo) + 1:-len(".jsonl.gz")]
        for p in diretorio.glob(f"{prefixo}-*.jsonl.gz")
    )
    if not dias:
        return None
    return int(
        datetime.strptime(dias[0], "%Y-%m-%d")
        .replace(tzinfo=timezone.utc).timestamp() * 1000
    )


def uma_volta(
    destino: envio.Destino,
    diretorio: Path,
    prefixo: str,
    *,
    venue: str,
    symbol: str,
    contrato: str = extracao.CONTRATO,
    agora_ms: int | None = None,
) -> dict:
    """Pergunta o ponto, extrai o que falta, entrega."""
    agora = int(time.time() * 1000) if agora_ms is None else agora_ms

    ponto = envio.ponto_de_retomada(
        destino, venue=venue, symbol=symbol, contrato=contrato
    )
    grade = int(ponto.get("grade_ms") or extracao.GRADE_MS)
    tolerancia = int(ponto.get("tolerancia_ms") or extracao.TOLERANCIA_MS)

    retomar = ponto.get("retomar_de_ms")
    de_ms = int(retomar) if retomar is not None else (
        (agora - DIAS_INICIAIS * MS_POR_DIA) // grade * grade
    )

    # NAO varrer para tras de onde o coletor comecou a existir.
    #
    # Medido na primeira entrega real: dos 367 instantes indisponiveis, **301
    # eram anteriores ao coletor**. A janela inicial de 7 dias alcancou
    # 2026-08-31 e a coleta liga em 2026-09-04 - e "nao havia coletor" foi
    # gravado com o mesmo motivo de "nao havia cotacao".
    #
    # As duas coisas sao diferentes: uma e ausencia de observacao, a outra e
    # observacao de ausencia. Contar as duas juntas faz a cobertura descrever
    # a nossa data de deploy em vez do mercado.
    if retomar is None:
        primeiro = _primeiro_dia_com_arquivo(diretorio, prefixo)
        if primeiro is not None:
            de_ms = max(de_ms, primeiro // grade * grade)

    # A fronteira: o inicio da barra CORRENTE, exclusivo.
    ate_ms = agora // grade * grade

    # ATRASO em instantes de grade: quantos ja fecharam e ainda nao foram
    # entregues. E o numero operacional, e vai em TODA volta - inclusive nas
    # que nao enviam nada.
    atraso = max(0, (ate_ms - de_ms) // grade)
    base = {"enviadas": 0, "atraso_instantes": atraso,
            "ultimo_entregue_ms": de_ms - grade}

    if ate_ms <= de_ms:
        return {**base, "estado": "em_dia"}

    arquivos = arquivos_do_periodo(diretorio, prefixo, de_ms, ate_ms)
    if not arquivos:
        # NAO e silencio normal. O coletor esta gravando agora; se nenhum
        # arquivo cobre o periodo, ou o prefixo mudou, ou o volume sumiu, ou o
        # relogio esta em outro ano. Sai como alerta.
        return {**base, "estado": "sem_arquivo", "de_ms": de_ms, "ate_ms": ate_ms}

    observacoes = extracao.extrair(
        arquivos, de_ms=de_ms, ate_ms_exclusive=ate_ms,
        grade_ms=grade, tolerancia_ms=tolerancia,
    )
    if not observacoes:
        return {**base, "estado": "nada_a_extrair",
                "de_ms": de_ms, "ate_ms": ate_ms}

    lote = observacoes[:MAX_LOTE]
    resposta = envio.enviar(
        destino, venue=venue, symbol=symbol, contrato=contrato,
        observacoes=lote,
    )
    validas = sum(1 for o in lote if o.disponivel)
    return {
        "estado": "entregue",
        "enviadas": len(lote),
        "validas": validas,
        "indisponiveis": len(lote) - validas,
        "aceitas": resposta.get("aceitas"),
        "repetidas": resposta.get("repetidas"),
        "de_ms": lote[0].t_grid_ms,
        "ate_ms": lote[-1].t_grid_ms,
        "restantes": len(observacoes) - len(lote),
        "atraso_instantes": atraso - len(lote),
        "ultimo_entregue_ms": lote[-1].t_grid_ms,
    }


async def entregar(
    destino: envio.Destino | None,
    diretorio: Path,
    prefixo: str,
    parar: asyncio.Event,
    *,
    venue: str,
    symbol: str,
    espera_s: float = SEGUNDOS_ENTRE_VOLTAS,
) -> None:
    """Laco de entrega. **Nunca derruba o coletor.**

    Se `destino` e `None` (sem credencial), reclama uma vez e nao roda: a
    coleta continua, porque ela e a parte irrecuperavel. A entrega e derivada
    do arquivo bruto e pode ser refeita a qualquer momento.
    """
    if destino is None:
        log.warning("coletor.entrega_desligada", extra={
            "motivo": "sem COLETOR_API_URL, API_SERVICE_TOKEN ou "
                      "COLETOR_HMAC_SECRET",
            "consequencia": "a COLETA continua; a calibracao nao recebe "
                            "amostra ate as variaveis existirem",
            "recuperavel": True,
        })
        return

    while not parar.is_set():
        try:
            r = await asyncio.to_thread(
                uma_volta, destino, diretorio, prefixo,
                venue=venue, symbol=symbol,
            )
            # TODA volta diz alguma coisa, e a razao e concreta: com grade de
            # 15 min e laco de 5, DUAS EM TRES voltas nao tem o que enviar.
            # Enquanto so a volta produtiva falava, "funcionando e ocioso"
            # ficava indistinguivel de "quebrado e calado" - e `sem_arquivo`,
            # que e problema de verdade, saia pelo mesmo silencio.
            #
            # E o que se procura no log passa a ser `atraso_instantes`: ele
            # separa atraso de lacuna, que e a distincao do ADR 0029.
            estado = r.get("estado")
            if estado == "sem_arquivo":
                log.warning("coletor.entrega_sem_arquivo", extra={
                    **r,
                    "acao": "conferir COLETOR_DIR, o prefixo do arquivo e o "
                            "volume montado - o coletor esta gravando agora",
                })
            elif estado == "entregue":
                log.info("coletor.entrega", extra=r)
            else:
                log.info("coletor.entrega_ociosa", extra=r)
        except envio.DivergenciaRecusada as e:
            # ERRO ALTO, e nao se resolve reenviando.
            log.error("coletor.divergencia", extra={"erro": str(e)})
        except envio.ErroDeEnvio as e:
            log.warning("coletor.entrega_falhou", extra={"erro": str(e)})
        except Exception as e:  # noqa: BLE001
            # A entrega NUNCA derruba a coleta. Um defeito na extracao custa
            # amostras nao entregues - que sao refazeis a partir do bruto -,
            # e derrubar o processo custaria amostras nao COLETADAS, que nao
            # sao.
            log.error("coletor.entrega_quebrou", extra={
                "erro": f"{type(e).__name__}: {e}",
                "consequencia": "a coleta segue; a entrega tenta de novo",
            })
        try:
            await asyncio.wait_for(parar.wait(), timeout=espera_s)
        except asyncio.TimeoutError:
            pass


def destino_do_ambiente(env: dict[str, str]) -> envio.Destino | None:
    """`None` quando falta credencial - e o coletor segue coletando.

    Diferente do rele, que falha FECHADO: um rele sem credencial nao faz nada,
    e um coletor sem credencial de envio continua fazendo a unica coisa que
    nao da para refazer depois.
    """
    faltando = [
        n for n in ("COLETOR_API_URL", "API_SERVICE_TOKEN", "COLETOR_HMAC_SECRET")
        if not env.get(n)
    ]
    if faltando:
        return None
    return envio.Destino(
        base_url=env["COLETOR_API_URL"].rstrip("/"),
        token=env["API_SERVICE_TOKEN"],
        segredo=env["COLETOR_HMAC_SECRET"],
    )


def selar_no_boot(diretorio: Path, prefixo: str) -> list[Path]:
    """Sela os dias fechados que ainda nao tem manifesto (ADR 0032, req. 6).

    A rotacao normal sela ao virar o dia; um processo que MORREU antes da
    virada nao selou nada. Sem esta varredura, um reinicio a meia-noite
    deixaria um dia inteiro sem identidade, e o defeito so apareceria quando
    alguem tentasse auditar a extracao.
    """
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return selar_dias_fechados(diretorio, prefixo, hoje)
