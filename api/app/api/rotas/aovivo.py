"""Rota do fluxo ao vivo. ADR 0029, incremento 16.

**A primeira rota do projeto que RECEBE dado que vai para o banco.** Todas as
outras leem, ou disparam trabalho sobre dado que a propria `api` buscou. Essa
e a superficie nova que a alternativa A do transporte trouxe, e ela e nomeada
em vez de minimizada.

Tres camadas, e nenhuma substitui a outra:

| camada | o que ela responde |
|---|---|
| `API_SERVICE_TOKEN` | quem esta chamando? |
| **HMAC + carimbo + nonce** | este pedido exato veio de quem tem o segredo, agora, e nao e repeticao? |
| **validacao integral** | o conteudo faz sentido como barra? |

A terceira existe porque o rele e codigo nosso mas roda noutro lugar e fala
pela rede. Confiar na validacao dele moveria a fronteira de confianca para
fora do processo que grava.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from ...aovivo import assinatura, fluxo, snapshot

log = logging.getLogger(__name__)

# A tag e o NOME DO MODULO, e nao um rotulo bonito: ha guarda conferindo, e a
# razao e que a secao do Swagger tem de ser rastreavel ao arquivo.
#
# E SEM `dependencies` aqui. A do token vive no router raiz, e uma so - guarda
# do incremento 6 impede a repeticao, porque repetida por modulo um
# esquecimento abriria uma secao inteira, e a ausencia de uma linha e o
# defeito mais dificil de ver numa revisao.
router = APIRouter(prefix="/api/aovivo", tags=["aovivo"])

# Teto de barras por lote. O backfill de um dia inteiro sao 96 barras a 15 min;
# 500 cobre cinco dias de queda e ainda cabe num pedido pequeno.
MAX_BARRAS = 500


class BarraEntrada(BaseModel):
    """Uma barra fechada, em inteiros de precisao fixa (regra 5)."""

    open_time_ms: int = Field(gt=0)
    open: int = Field(gt=0)
    high: int = Field(gt=0)
    low: int = Field(gt=0)
    close: int = Field(gt=0)
    volume: int = Field(ge=0)
    quote_volume: int = Field(ge=0)
    trades: int = Field(ge=0)


class LoteEntrada(BaseModel):
    """O corpo do POST.

    A serie vem no corpo, e nao na URL: ela entra no hash canonico do snapshot,
    e um parametro de rota seria mais facil de trocar por engano do que um
    campo que o HMAC cobre.
    """

    venue: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    interval_ms: int = Field(gt=0)
    price_scale_exp: int = Field(ge=0)
    volume_scale_exp: int = Field(ge=0)
    origem: str = Field(pattern="^(ao_vivo|backfill)$")
    # O limite vive na descricao e no validador, e nao em `max_length`: as
    # notas de API registram que `maxItems` e RECUSADO pelo provedor, e a
    # licao de nao confiar num limite declarado vale aqui tambem.
    barras: list[BarraEntrada]

    def serie(self) -> fluxo.Serie:
        return fluxo.Serie(
            venue=self.venue, symbol=self.symbol, timeframe=self.timeframe,
            interval_ms=self.interval_ms,
            price_scale_exp=self.price_scale_exp,
            volume_scale_exp=self.volume_scale_exp,
        )


@router.get("/ponto")
def ponto_de_retomada(
    request: Request,
    venue: str,
    symbol: str,
    timeframe: str,
    interval_ms: int,
) -> dict[str, Any]:
    """De onde o rele deve retomar.

    Existe para que o backfill parta da **ultima barra confirmada por este
    lado**, em vez de reenviar tudo ou de o rele supor onde paramos. Queda do
    rele passa a ser atraso recuperavel, e nao lacuna.
    """
    conn = request.app.state.conn
    serie = fluxo.Serie(
        venue=venue, symbol=symbol, timeframe=timeframe,
        interval_ms=interval_ms, price_scale_exp=0, volume_scale_exp=0,
    )
    ultima = fluxo.ultima_confirmada(conn, serie)
    return {
        "venue": venue, "symbol": symbol, "timeframe": timeframe,
        "ultima_confirmada_ms": ultima,
        "retomar_de_ms": None if ultima is None else ultima + interval_ms,
        "max_barras_por_lote": MAX_BARRAS,
    }


@router.post("/barras", status_code=status.HTTP_202_ACCEPTED)
async def receber_barras(
    request: Request,
    x_rele_assinatura: str = Header(...),
    x_rele_carimbo: int = Header(...),
    x_rele_nonce: str = Header(...),
) -> dict[str, Any]:
    """Recebe um lote de barras fechadas do rele.

    **O corpo e lido CRU antes de ser interpretado**, porque e sobre ele que a
    assinatura e conferida. Assinar o JSON re-serializado faria a verificacao
    depender de como cada lado ordena chaves e espaca virgulas - e a primeira
    divergencia de biblioteca quebraria tudo, parecendo credencial errada.

    `202` e nao `201`: o lote pode ser inteiramente de repetidas, e nesse caso
    nada foi criado. Dizer `201` ali afirmaria criacao que nao houve.
    """
    from ...settings import get_settings

    conn = request.app.state.conn
    bruto = await request.body()

    # ------------------------------------------------------------- HMAC
    try:
        assinatura.conferir(
            conn,
            assinatura.Pedido(
                carimbo_ms=x_rele_carimbo, nonce=x_rele_nonce, corpo=bruto
            ),
            x_rele_assinatura,
            get_settings().rele_hmac_secret.get_secret_value(),
        )
    except assinatura.AssinaturaInvalida as e:
        # 401, e nao 400: o pedido pode estar perfeito e a credencial nao.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)
        ) from e

    # ------------------------------------------------ o corpo, agora sim
    try:
        lote = LoteEntrada.model_validate_json(bruto)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"corpo invalido: {e}",
        ) from e

    if not lote.barras:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="lote vazio: nada a receber",
        )
    if len(lote.barras) > MAX_BARRAS:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"{len(lote.barras)} barras excede o teto de {MAX_BARRAS}",
        )

    barras = [
        fluxo.Barra(
            open_time_ms=b.open_time_ms, open=b.open, high=b.high, low=b.low,
            close=b.close, volume=b.volume, quote_volume=b.quote_volume,
            trades=b.trades,
        )
        for b in lote.barras
    ]

    # -------------------------------------------------------- a gravacao
    try:
        with conn:
            recebimento = fluxo.receber(
                conn, lote.serie(), barras, origem=lote.origem  # type: ignore[arg-type]
            )
            assinatura.podar(conn)
    except fluxo.DivergenciaDeConteudo as e:
        # 409, e ERRO ALTO. Nao e "aceito com aviso": ou a origem revisou o
        # passado, ou algo corrompeu o dado, ou dois remetentes discordam - e
        # nenhuma das tres se resolve escolhendo uma das versoes.
        log.error("aovivo.divergencia", extra={"erro": str(e)})
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except fluxo.BarraInvalida as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        ) from e

    log.info("aovivo.lote_recebido", extra={
        "symbol": lote.symbol, "timeframe": lote.timeframe,
        "origem": lote.origem, "aceitas": recebimento.aceitas,
        "repetidas": recebimento.repetidas,
    })
    return {
        "aceitas": recebimento.aceitas,
        "repetidas": recebimento.repetidas,
        "primeira_ms": recebimento.primeira_ms,
        "ultima_ms": recebimento.ultima_ms,
        "ultima_confirmada_ms": fluxo.ultima_confirmada(conn, lote.serie()),
    }


@router.get("/estado")
def estado(
    request: Request,
    venue: str = "binance",
    symbol: str = "BTCUSDT",
    timeframe: str = "15m",
    interval_ms: int = 900_000,
) -> dict[str, Any]:
    """Atraso e contagem. **Atraso NAO e lacuna** (ADR 0029).

    Kline e recuperavel: enquanto o backfill nao correr, o que existe e
    atraso. Publicar isso como lacuna declararia perdido um dado que a Binance
    ainda tem.
    """
    import time

    conn = request.app.state.conn
    serie = fluxo.Serie(
        venue=venue, symbol=symbol, timeframe=timeframe,
        interval_ms=interval_ms, price_scale_exp=0, volume_scale_exp=0,
    )
    agora_ms = int(time.time() * 1000)
    total = conn.execute(
        "SELECT COUNT(*) AS n, MIN(open_time_ms) AS a, MAX(open_time_ms) AS b"
        "  FROM stream_bar WHERE venue = ? AND symbol = ? AND timeframe = ?",
        (venue, symbol, timeframe),
    ).fetchone()
    por_origem = {
        r["origem"]: r["n"]
        for r in conn.execute(
            "SELECT origem, COUNT(*) AS n FROM stream_bar"
            " WHERE venue = ? AND symbol = ? AND timeframe = ?"
            " GROUP BY origem",
            (venue, symbol, timeframe),
        )
    }
    atraso = fluxo.atraso_ms(conn, serie, agora_ms)
    return {
        "serie": {"venue": venue, "symbol": symbol, "timeframe": timeframe},
        "barras": int(total["n"]),
        "primeira_ms": total["a"],
        "ultima_ms": total["b"],
        "por_origem": por_origem,
        "atraso_ms": atraso,
        "atraso_barras": None if atraso is None else atraso // interval_ms,
        "nota": (
            "atraso NAO e lacuna: kline e recuperavel, e o backfill fecha o "
            "atraso sem que nada tenha sido perdido"
        ),
    }


@router.get("/snapshots")
def listar_snapshots(request: Request) -> dict[str, Any]:
    """Os intervalos fechados, com hash e completude.

    Todo resultado do forward cita um destes. O fluxo, nunca - ele nao tem
    hash, e nao ter e o desenho.
    """
    conn = request.app.state.conn
    linhas = [
        dict(r)
        for r in conn.execute(
            "SELECT id, venue, symbol, timeframe, from_ms, to_ms_exclusive,"
            "       barras_esperadas, barras_presentes, lacunas,"
            "       maior_lacuna_barras, lacuna_aceita_por, sha256,"
            "       finalidade, calibration_version, hypothesis_id, criado_em"
            "  FROM snapshot ORDER BY id DESC LIMIT 100"
        )
    ]
    for l in linhas:
        l["completo"] = l["barras_presentes"] == l["barras_esperadas"]
        l["hash_conferido"] = snapshot.reconferir(conn, int(l["id"]))
    return {"snapshots": linhas, "quantidade": len(linhas)}
