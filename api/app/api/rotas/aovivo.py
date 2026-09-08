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

from ...aovivo import assinatura, bbo, fluxo, snapshot
from ...calibracao import piloto
from ..comum import _conn

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
    conn = _conn(request)
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

    conn = _conn(request)
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

    conn = _conn(request)
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
    conn = _conn(request)
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


# ===========================================================================
# BBO alinhado a grade. ADR 0032, incremento 18.
#
# ROTA E TABELA PROPRIAS (requisito 1), e o protocolo e o MESMO do rele. O que
# nao se compartilha e o destino: `stream_bar` e a serie de DECISAO, e e dela
# que o snapshot do forward tira o hash; `bbo_amostra` e insumo de MEDICAO.
# Misturar faria o hash do snapshot depender de dado de calibracao, e um
# resultado do forward passaria a citar procedencia que nao e dele.
# ===========================================================================

MAX_AMOSTRAS = 500


class AmostraEntrada(BaseModel):
    model_config = {"extra": "forbid"}

    t_grid_ms: int = Field(ge=0)
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
    relogio_medido_em_ms: int | None = None


class LoteBBO(BaseModel):
    model_config = {"extra": "forbid"}

    venue: str
    symbol: str
    contrato: str
    price_scale_exp: int = Field(ge=0, le=18)
    volume_scale_exp: int = Field(ge=0, le=18)
    amostras: list[AmostraEntrada]

    def serie(self) -> bbo.Serie:
        return bbo.Serie(
            venue=self.venue, symbol=self.symbol,
            price_scale_exp=self.price_scale_exp,
            volume_scale_exp=self.volume_scale_exp,
        )


@router.get("/bbo/ponto")
def ponto_do_bbo(
    request: Request, venue: str, symbol: str, contrato: str
) -> dict[str, Any]:
    """De onde a extracao deve retomar.

    Mesma regra do fluxo: o estado de verdade e o desta ponta, e nao o que o
    coletor lembra. E `ultimo_t_grid` conta a INDISPONIVEL tambem - ela
    tambem e observacao, e retomar de antes dela reprocessaria o que a
    extracao ja concluiu.
    """
    conn = _conn(request)
    try:
        c = bbo.ler_contrato(conn, contrato)
    except bbo.ContratoDesconhecido as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e

    serie = bbo.Serie(venue=venue, symbol=symbol,
                      price_scale_exp=0, volume_scale_exp=0)
    ultimo = bbo.ultimo_t_grid(conn, serie, contrato)
    return {
        "venue": venue, "symbol": symbol, "contrato": contrato,
        "ultimo_t_grid_ms": ultimo,
        "retomar_de_ms": None if ultimo is None else ultimo + c.grade_ms,
        "grade_ms": c.grade_ms,
        "tolerancia_ms": c.tolerancia_ms,
        "max_amostras_por_lote": MAX_AMOSTRAS,
    }


@router.post("/bbo", status_code=status.HTTP_202_ACCEPTED)
async def receber_bbo(
    request: Request,
    x_rele_assinatura: str = Header(...),
    x_rele_carimbo: int = Header(...),
    x_rele_nonce: str = Header(...),
) -> dict[str, Any]:
    """Recebe um lote de amostras alinhadas a grade.

    O corpo assinado e o CRU, pelo mesmo motivo da rota de barras: assinar o
    JSON re-serializado faria a verificacao depender de espacamento, e a
    primeira divergencia de biblioteca quebraria tudo parecendo credencial
    errada.
    """
    from ...settings import get_settings

    conn = _conn(request)
    bruto = await request.body()

    try:
        assinatura.conferir(
            conn,
            assinatura.Pedido(
                carimbo_ms=x_rele_carimbo, nonce=x_rele_nonce, corpo=bruto
            ),
            x_rele_assinatura,
            get_settings().coletor_hmac_secret.get_secret_value(),
        )
    except assinatura.AssinaturaInvalida as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)
        ) from e

    try:
        lote = LoteBBO.model_validate_json(bruto)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"corpo invalido: {e}",
        ) from e

    if not lote.amostras:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="lote vazio: nada a receber",
        )
    if len(lote.amostras) > MAX_AMOSTRAS:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"{len(lote.amostras)} amostras excede o teto de {MAX_AMOSTRAS}",
        )

    amostras = [
        bbo.Amostra(
            t_grid_ms=a.t_grid_ms, disponivel=a.disponivel, motivo=a.motivo,
            bid=a.bid, bid_qty=a.bid_qty, ask=a.ask, ask_qty=a.ask_qty, u=a.u,
            received_at_ms=a.received_at_ms,
            received_at_corrigido_ms=a.received_at_corrigido_ms,
            sampled_at_ms=a.sampled_at_ms, defasagem_ms=a.defasagem_ms,
            offset_us=a.offset_us, rtt_us=a.rtt_us,
            incerteza_residual_us=a.incerteza_residual_us,
            relogio_medido_em_ms=a.relogio_medido_em_ms,
        )
        for a in lote.amostras
    ]

    try:
        r = bbo.receber(conn, lote.serie(), lote.contrato, amostras)
        assinatura.podar(conn)
    except bbo.ContratoDesconhecido as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    except bbo.DivergenciaDeAmostra as e:
        log.error("bbo.divergencia", extra={"erro": str(e)})
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except bbo.AmostraInvalida as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        ) from e

    log.info("bbo.lote_recebido", extra={
        "symbol": lote.symbol, "contrato": lote.contrato,
        "aceitas": r.aceitas, "repetidas": r.repetidas,
    })
    return {
        "aceitas": r.aceitas, "repetidas": r.repetidas,
        "primeira_ms": r.primeira_ms, "ultima_ms": r.ultima_ms,
        "ultimo_t_grid_ms": bbo.ultimo_t_grid(conn, lote.serie(), lote.contrato),
    }


@router.get("/bbo/estado")
def estado_do_bbo(
    request: Request,
    venue: str = "binance",
    symbol: str = "BTCUSDT",
    contrato: str = bbo.CONTRATO_PADRAO,
) -> dict[str, Any]:
    """Cobertura, e em que pe esta a janela do piloto.

    **Duas coberturas**, e a `observada` e a que vale: a `total` conta os
    instantes que a primeira extracao varreu antes de existir coletor.

    A janela e **lida**, e so derivada quando ainda nao foi fechada - e nunca
    fechada por esta rota. Uma consulta de estado que congelasse a fronteira
    do periodo de calibracao como efeito colateral seria exatamente o que a
    quinta pergunta do teste de escopo proibe.
    """
    conn = _conn(request)
    try:
        c = bbo.ler_contrato(conn, contrato)
    except bbo.ContratoDesconhecido as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e

    serie = bbo.Serie(venue=venue, symbol=symbol,
                      price_scale_exp=0, volume_scale_exp=0)

    janela = piloto.ler(conn, serie, contrato)
    if janela is None:
        try:
            previa = piloto.derivar(conn, serie, contrato)
            piloto_json: dict[str, Any] = {
                "estado": "pronta_para_fechar",
                "de_ms": previa.de_ms,
                "ate_ms_exclusive": previa.ate_ms_exclusive,
                "fechada_por": previa.fechada_por,
            }
        except piloto.PilotoNaoFechaAinda as e:
            piloto_json = {"estado": "acumulando", "falta": str(e)}
    else:
        piloto_json = {
            "estado": "fechada",
            "de_ms": janela.de_ms,
            "ate_ms_exclusive": janela.ate_ms_exclusive,
            "observacoes_validas": janela.observacoes_validas,
            "observacoes_totais": janela.observacoes_totais,
            "dias_corridos": janela.dias_corridos,
            "fechada_por": janela.fechada_por,
        }

    # DUAS coberturas, e a diferenca entre elas nao e detalhe.
    #
    # A global inclui os instantes que a primeira extracao varreu ANTES de o
    # coletor existir: sem ponto de retomada ela olha 7 dias para tras, e o
    # coletor liga em 2026-09-04. Medido na primeira entrega real: 301 dos 367
    # indisponiveis eram desse periodo - "nao havia coletor" contado como se
    # fosse "nao havia cotacao".
    #
    # A do PILOTO comeca na primeira observacao valida, e e a que descreve o
    # que foi de fato observado. **E ela que acompanha qualquer estimativa de
    # calibracao** - publicar a global ao lado de um numero de calibracao
    # afirmaria uma qualidade de dado que nao e a daquele periodo.
    de = ate = None
    if janela is not None:
        de, ate = janela.de_ms, janela.ate_ms_exclusive
    else:
        primeira = conn.execute(
            "SELECT MIN(t_grid_ms) AS t FROM bbo_amostra"
            " WHERE venue = ? AND symbol = ? AND contrato = ?"
            "   AND disponivel = 1",
            (venue, symbol, contrato),
        ).fetchone()
        if primeira is not None and primeira["t"] is not None:
            de = int(primeira["t"])

    return {
        "contrato": {
            "nome": c.contrato, "timeframe": c.timeframe,
            "execution_reference": c.execution_reference,
            "latency_bars": c.latency_bars, "grade_ms": c.grade_ms,
            "tolerancia_ms": c.tolerancia_ms,
        },
        "cobertura_total": bbo.cobertura(conn, serie, contrato),
        "cobertura_observada": (
            None if de is None
            else bbo.cobertura(conn, serie, contrato, de_ms=de,
                               ate_ms_exclusive=ate)
        ),
        "piloto": piloto_json,
    }
