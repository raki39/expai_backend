"""Previsto contra observado, instante a instante. Incremento 18.

§8.4.1.2, passo 2: cada ordem hipotetica registrada no instante real, com o
preco que o simulador previu - e depois comparada com o mercado que apareceu.

## Os dois erros, e por que sao dois

    E1 = ask - abertura              DIAGNOSTICO
    E2 = p_exec_previsto - ask       ALVO da calibracao

**E1 ja contem meio spread.** Calibrar `spread_bps` a partir dele e continuar
somando `spread_bps` no simulador contaria o spread DUAS VEZES, e nada
acusaria - o resultado seria um simulador exageradamente pessimista com
aparencia de calibrado. Foi o usuario quem separou os dois ao fechar a D45.

**A direcao do pessimismo e `p10(E2) >= 0`.** Na compra pagamos o **ask**;
prever menos que o ask e otimismo, e um simulador otimista transforma
estrategia ruim em estrategia aprovada.

Na venda, a simetria: recebemos o **bid**, entao pessimismo e prever receber
no maximo ele.

    E1_venda = bid - abertura
    E2_venda = bid - p_exec_previsto

## O que este modulo NAO faz

Nao ajusta parametro nenhum. Ele mede. Quem ajusta e a estimativa, e ela roda
sobre estas linhas depois de a janela do piloto FECHAR - nunca durante.

E a referencia e SEMPRE mercado observado: `bbo_amostra`, que veio do coletor.
Ha guarda de codigo conferindo que nada aqui le `execution`, `baseline_result`
ou qualquer tabela de resultado simulado (R63). Comparar simulador com
simulador daria erro zero por construcao, e o numero pareceria excelente.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from ..aovivo import bbo
from ..config.schema import ExperimentConfig
from ..dataset.loader import BarraCarregada
from ..simulador.execucao import preco_executado, preco_referencia
from ..store import bloco_atomico
from . import regimes

log = logging.getLogger(__name__)

# bps x 1000, como `app/regime` ja usa. Um decimo de milesimo de bps e
# resolucao de sobra para um delta de 0,5 bps, e mantem tudo inteiro.
ESCALA_MILI_BPS = 1_000
BPS = Decimal(10_000)

LADOS = ("compra", "venda")

#: As tres finalidades do ADR 0041. Uma observacao pertence a EXATAMENTE uma,
#: e quem confere e o gatilho `calibracao_observacao_dentro_da_janela` - a
#: lista aqui existe para recusar o erro de digitacao antes do banco, e nao
#: para ser a garantia.
FINALIDADES = ("piloto", "calibracao", "revalidacao")


class ContratoIncompativel(Exception):
    """A config vigente nao e a que o contrato assume. NAO calibrar."""


@dataclass(frozen=True)
class Observacao:
    t_grid_ms: int
    lado: str
    abertura: int
    bbo_bid: int
    bbo_bid_qty: int
    bbo_ask: int
    bbo_ask_qty: int
    p_exec_previsto: int
    e1_mili_bps: int
    e2_mili_bps: int
    nocional_cents: int
    dentro_do_l1: bool


def _agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mili_bps(delta: int, base: int) -> int:
    """`delta / base` em bps x 1000, arredondado ao inteiro mais proximo.

    `Decimal`, nunca `float`: estes numeros entram numa estatistica de quantil
    cujo limite inferior decide se a calibracao passa, e erro de ponto
    flutuante acumulado numa cauda e o tipo de coisa que ninguem percebe.
    """
    return int(
        (Decimal(delta) * BPS * ESCALA_MILI_BPS / Decimal(base))
        .to_integral_value()
    )


def prever(
    abertura: int, lado: str, config: ExperimentConfig,
    *, spread_bps: Decimal | None = None,
) -> int:
    """O preco que o simulador preve para uma ordem neste instante.

    **Usa o nucleo compartilhado**, e nao uma copia da formula. Se este modulo
    reimplementasse `preco_executado`, a calibracao mediria a distancia entre
    duas implementacoes nossas em vez do erro do simulador - e as duas
    divergiriam no primeiro ajuste de parametro.

    `preco_referencia` recebe uma barra porque o modelo `limite_adverso`
    precisa de `high`/`low`. Sob `abertura` - que o contrato `bbo@1` exige - so
    `open` e lido, e os outros campos sao inertes.
    """
    barra = BarraCarregada(
        open_time_ms=0, close_time_ms=0,
        open=abertura, high=abertura, low=abertura, close=abertura,
        volume=0, quote_volume=0, trades=0,
    )
    ref = preco_referencia(barra, lado, config)  # type: ignore[arg-type]
    return preco_executado(  # type: ignore[arg-type]
        ref, lado, config, spread_bps=spread_bps
    )


def calcular(
    *, t_grid_ms: int, lado: str, abertura: int, amostra: sqlite3.Row,
    config: ExperimentConfig, orcamento_cents: int,
) -> Observacao:
    """Uma observacao, a partir da barra e da amostra de BBO do mesmo instante."""
    bid = int(amostra["bid"])
    ask = int(amostra["ask"])
    bid_qty = int(amostra["bid_qty"])
    ask_qty = int(amostra["ask_qty"])

    previsto = prever(abertura, lado, config)

    if lado == "compra":
        # Pagamos o ask. Pessimismo = prever pagar pelo menos ele.
        e1 = _mili_bps(ask - abertura, abertura)
        e2 = _mili_bps(previsto - ask, abertura)
        qty_cotada = ask_qty
        preco_do_lado = ask
    else:
        # Recebemos o bid. Pessimismo = prever receber no maximo ele.
        e1 = _mili_bps(bid - abertura, abertura)
        e2 = _mili_bps(bid - previsto, abertura)
        qty_cotada = bid_qty
        preco_do_lado = bid

    # CONDICAO DE VALIDADE POR TAMANHO (ADR 0027). O preco do topo de livro so
    # vale para nocional <= o tamanho cotado naquele nivel. Conferido
    # observacao a observacao, e nunca assumido - a hipotese 41 girava ~US$ 714
    # por ordem e o topo de BTC/USDT carrega muito mais, mas "muito mais"
    # costuma virar "menos" exatamente nos instantes que interessam.
    #
    # As escalas: preco e quantidade vem em 10^8, e `nocional_cents` e o
    # produto convertido a centavos. `DIVISOR_NOCIONAL` do simulador e 10^14
    # pela mesma conta (10^8 x 10^8 / 100).
    nocional_cotado_cents = qty_cotada * preco_do_lado // 10**14
    dentro = orcamento_cents <= nocional_cotado_cents

    return Observacao(
        t_grid_ms=t_grid_ms, lado=lado, abertura=abertura,
        bbo_bid=bid, bbo_bid_qty=bid_qty, bbo_ask=ask, bbo_ask_qty=ask_qty,
        p_exec_previsto=previsto, e1_mili_bps=e1, e2_mili_bps=e2,
        nocional_cents=orcamento_cents, dentro_do_l1=dentro,
    )


@dataclass(frozen=True)
class Materializacao:
    """O que a materializacao publica, e os seis numeros sao SEPARADOS.

    Requisito 8 do usuario (ADR 0041 par.7). Instante e observacao sao
    grandezas diferentes, e troca-las sob o mesmo rotulo e o padrao que este
    projeto conta trinta e uma vezes - a ultima foi "33 lancamentos" no lugar
    de "65 creditos", em 2026-09-10.

        instantes_validos -> candidatos_de_lado -> comparaveis -> na_estatistica
                              (x 2 lados)          (- sem barra)  (- fora do L1)
    """

    finalidade: str
    de_ms: int
    ate_ms_exclusive: int
    janela_hash: str
    instantes_validos_na_janela: int
    candidatos_de_lado: int
    comparaveis: int
    sem_barra_pareada: int
    excluidos_por_l1: int
    na_estatistica: int
    gravadas: int
    ja_existiam: int
    lados: tuple[str, ...]
    por_regime: dict[str, int]
    fora_do_regime_da_janela: int

    def como_dict(self) -> dict:
        return {
            "finalidade": self.finalidade,
            "janela": {
                "de_ms": self.de_ms,
                "ate_ms_exclusive": self.ate_ms_exclusive,
                "hash": self.janela_hash,
            },
            "instantes_validos_na_janela": self.instantes_validos_na_janela,
            "candidatos_de_lado": self.candidatos_de_lado,
            "comparaveis": self.comparaveis,
            "sem_barra_pareada": self.sem_barra_pareada,
            "excluidos_por_l1": self.excluidos_por_l1,
            "na_estatistica": self.na_estatistica,
            "gravadas": self.gravadas,
            "ja_existiam": self.ja_existiam,
            "lados": list(self.lados),
            "por_regime": dict(sorted(self.por_regime.items())),
            "fora_do_regime_da_janela": self.fora_do_regime_da_janela,
            "o_que_cada_um_e": {
                "instantes_validos_na_janela":
                    "instantes de GRADE com BBO valida dentro da janela",
                "candidatos_de_lado":
                    "instantes validos x lados. NAO e o mesmo numero: cada"
                    " instante rende uma observacao de compra e uma de venda",
                "comparaveis":
                    "candidatos que acharam barra do MESMO instante. Sem barra"
                    " nao ha `abertura`, e sem ela nao ha previsao a comparar",
                "excluidos_por_l1":
                    "comparaveis cujo nocional passa do tamanho cotado no topo."
                    " Ficam GRAVADOS e saem da estatistica (ADR 0027)",
                "na_estatistica":
                    "comparaveis - excluidos. E o UNICO que alimenta o"
                    " bootstrap",
                "por_regime":
                    "candidatos de lado por regime do instante, pelo detector"
                    " congelado do ADR 0026. `indefinido` NAO e regime: e a"
                    " declaracao de que ainda nao ha 672 barras anteriores"
                    " para classificar",
                "fora_do_regime_da_janela":
                    "candidatos PULADOS porque o regime do instante nao e o da"
                    " janela. Só a revalidacao tem janela por regime; na"
                    " calibracao e no piloto este numero e zero por"
                    " construcao, porque a janela nao tem regime",
            },
        }


def registrar(
    conn: sqlite3.Connection,
    *,
    finalidade: str,
    contrato: str,
    venue: str,
    symbol: str,
    config: ExperimentConfig,
    config_version_id: int,
    calibracao_perfil_hash: str,
    janela_hash: str,
    de_ms: int,
    ate_ms_exclusive: int,
    orcamento_cents: int,
    lados: tuple[str, ...] = LADOS,
    apenas_regime: str | None = None,
) -> Materializacao:
    """Pareia BBO com barra fechada e grava a observacao de cada instante.

    **A FINALIDADE e obrigatoria e nao tem default.** Ela e da JANELA, e quem
    confere que o instante cai dentro dela e o gatilho
    `calibracao_observacao_dentro_da_janela` - nao este codigo. Um default
    aqui faria a barreira depender de quem chama (ADR 0041 par.3).

    **Confere o contrato ANTES de olhar qualquer amostra.** Se a semantica de
    execucao vigente nao for a que o contrato assume, nada e medido - as
    amostras foram alinhadas na abertura da barra porque a D20 fixou a
    referencia ali, e usa-las sob outra semantica mediria o instante errado.

    Idempotente: a chave primaria e `(contrato, venue, symbol, t_grid_ms,
    lado)` - **sem** `config_version_id`. Reprocessar sob a MESMA identidade
    nao duplica nada; sob outra, o gatilho de congelamento **recusa** em vez de
    acrescentar (ADR 0041 par.8).

    ## `apenas_regime`, e por que ele e obrigatorio na revalidacao

    A janela de revalidacao e POR REGIME; a observacao e por INSTANTE. Uma
    janela de catorze dias atravessa varios regimes - e o gatilho
    `calibracao_observacao_dentro_da_janela` recusa um instante cujo regime nao
    seja o da janela.

    Recusar e o comportamento certo do gatilho e seria o comportamento ERRADO
    aqui: sem o filtro, o primeiro instante de outro regime abortaria a
    materializacao INTEIRA, e a revalidacao nunca sairia do chao. Quem nao
    pertence e PULADO e CONTADO em `fora_do_regime_da_janela` - pulado em
    silencio seria outro defeito.
    """
    if finalidade not in FINALIDADES:
        raise ValueError(
            f"finalidade {finalidade!r} nao existe; sao {FINALIDADES}"
        )
    try:
        c = bbo.conferir_contrato(
            conn, contrato,
            timeframe=config.timeframe,
            execution_reference=config.execution_reference,
            latency_bars=config.latency_bars,
        )
    except bbo.ContratoNaoDescreveMais as e:
        raise ContratoIncompativel(str(e)) from e

    # Os instantes VALIDOS da janela, independentemente de haver barra. E o
    # denominador honesto: perder barra e perda nossa, e tem de aparecer.
    instantes_validos = conn.execute(
        "SELECT COUNT(*) AS n FROM bbo_amostra"
        " WHERE venue = ? AND symbol = ? AND contrato = ? AND disponivel = 1"
        "   AND t_grid_ms >= ? AND t_grid_ms < ?",
        (venue, symbol, contrato, de_ms, ate_ms_exclusive),
    ).fetchone()["n"]

    # O JOIN e o que torna a comparacao honesta: so ha observacao quando
    # EXISTEM a cotacao e a barra do MESMO instante. `stream_bar` e mercado
    # observado, como a amostra - nenhum dos dois e resultado simulado.
    linhas = conn.execute(
        "SELECT a.t_grid_ms, a.bid, a.ask, a.bid_qty, a.ask_qty,"
        "       b.open AS abertura"
        "  FROM bbo_amostra a"
        "  JOIN stream_bar b"
        "    ON b.venue = a.venue AND b.symbol = a.symbol"
        "   AND b.timeframe = ? AND b.open_time_ms = a.t_grid_ms"
        " WHERE a.venue = ? AND a.symbol = ? AND a.contrato = ?"
        "   AND a.disponivel = 1"
        "   AND a.t_grid_ms >= ? AND a.t_grid_ms < ?"
        " ORDER BY a.t_grid_ms",
        (c.timeframe, venue, symbol, contrato, de_ms, ate_ms_exclusive),
    ).fetchall()

    # O REGIME DE CADA INSTANTE, calculado UMA vez e gravado com a linha.
    #
    # Ele era derivado a cada leitura. Passou a ser coluna porque a parada e
    # POR REGIME e o gatilho que congela a serie de um regime fechado precisa
    # saber, DENTRO DO BANCO, de que regime e o instante que esta entrando.
    mapa = regimes.regimes_das_barras(
        conn, venue=venue, symbol=symbol, timeframe=c.timeframe
    )

    gravadas = ja_existiam = excluidos = fora_do_regime = 0
    por_regime: dict[str, int] = {}
    agora = _agora()
    with bloco_atomico(conn, "calibracao_registrar"):
        for linha in linhas:
            regime = regimes.regime_do_instante(
                mapa, int(linha["t_grid_ms"])
            )
            por_regime[regime] = por_regime.get(regime, 0) + len(lados)
            if apenas_regime is not None and regime != apenas_regime:
                fora_do_regime += len(lados)
                continue
            for lado in lados:
                o = calcular(
                    t_grid_ms=int(linha["t_grid_ms"]), lado=lado,
                    abertura=int(linha["abertura"]), amostra=linha,
                    config=config, orcamento_cents=orcamento_cents,
                )
                if not o.dentro_do_l1:
                    excluidos += 1
                cur = conn.execute(
                    "INSERT OR IGNORE INTO calibracao_observacao ("
                    " contrato, venue, symbol, t_grid_ms, lado, finalidade,"
                    " regime, config_version_id, calibracao_perfil_hash,"
                    " janela_hash,"
                    " abertura, bbo_bid, bbo_bid_qty, bbo_ask, bbo_ask_qty,"
                    " p_exec_previsto, e1_mili_bps, e2_mili_bps,"
                    " nocional_cents, dentro_do_l1, criado_em)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (contrato, venue, symbol, o.t_grid_ms, o.lado, finalidade,
                     regime,
                     config_version_id, calibracao_perfil_hash, janela_hash,
                     o.abertura, o.bbo_bid, o.bbo_bid_qty, o.bbo_ask,
                     o.bbo_ask_qty, o.p_exec_previsto, o.e1_mili_bps,
                     o.e2_mili_bps, o.nocional_cents,
                     1 if o.dentro_do_l1 else 0, agora),
                )
                if cur.rowcount:
                    gravadas += 1
                else:
                    ja_existiam += 1

    candidatos = instantes_validos * len(lados)
    # Os que nao pertencem a janela saem do numerador E do denominador: eles
    # nao sao perda nossa, sao instantes de outro regime.
    comparaveis = len(linhas) * len(lados) - fora_do_regime
    r = Materializacao(
        finalidade=finalidade, de_ms=de_ms,
        ate_ms_exclusive=ate_ms_exclusive, janela_hash=janela_hash,
        instantes_validos_na_janela=instantes_validos,
        candidatos_de_lado=candidatos,
        comparaveis=comparaveis,
        sem_barra_pareada=candidatos - comparaveis,
        excluidos_por_l1=excluidos,
        na_estatistica=comparaveis - excluidos,
        gravadas=gravadas, ja_existiam=ja_existiam, lados=tuple(lados),
        por_regime=por_regime, fora_do_regime_da_janela=fora_do_regime,
    )
    log.info("calibracao.observacoes", extra={
        "contrato": contrato, "symbol": symbol, "finalidade": finalidade,
        "config_version_id": config_version_id,
        "comparaveis": comparaveis, "gravadas": gravadas,
        "ja_existiam": ja_existiam,
    })
    return r


# ===========================================================================
# A LEITURA E POR FUNCAO, E NAO POR PARAMETRO
#
# Uma funcao por finalidade, cada uma com o valor como LITERAL no SQL. E o
# mesmo desenho de `acesso = 'agente'` do incremento 9: nenhum argumento faz o
# caminho da calibracao devolver dado do piloto.
#
# `ajuste.py` importa SO `serie_e2_da_calibracao` e `serie_e2_da_revalidacao`,
# e ha guarda estrutural por AST conferindo que ele nao alcanca a do piloto
# (ADR 0041 par.4).
# ===========================================================================


def _serie(
    conn: sqlite3.Connection, *, sql_finalidade: str, contrato: str,
    venue: str, symbol: str, lado: str,
    de_ms: int | None, ate_ms_exclusive: int | None,
    apenas_dentro_do_l1: bool, regime: str | None,
) -> list[int]:
    onde = ["contrato = ?", "venue = ?", "symbol = ?", "lado = ?"]
    args: list[object] = [contrato, venue, symbol, lado]
    if apenas_dentro_do_l1:
        onde.append("dentro_do_l1 = 1")
    if regime is not None:
        # A COLUNA, e nao o detector rodado de novo. O regime foi decidido na
        # materializacao e gravado com a linha; recalcula-lo aqui criaria uma
        # segunda derivacao do mesmo fato, que e como o `n_efetivo` do run 30
        # passou a ter dois valores.
        onde.append("regime = ?")
        args.append(regime)
    if de_ms is not None:
        onde.append("t_grid_ms >= ?")
        args.append(de_ms)
    if ate_ms_exclusive is not None:
        onde.append("t_grid_ms < ?")
        args.append(ate_ms_exclusive)
    return [
        int(r["e2_mili_bps"])
        for r in conn.execute(
            "SELECT e2_mili_bps FROM calibracao_observacao"
            f" WHERE {sql_finalidade} AND {' AND '.join(onde)}"
            " ORDER BY t_grid_ms",
            args,
        )
    ]


def serie_e2_do_piloto(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    lado: str, de_ms: int | None = None, ate_ms_exclusive: int | None = None,
    apenas_dentro_do_l1: bool = True, regime: str | None = None,
) -> list[int]:
    """`E2` do PILOTO. **So dimensionamento e diagnostico.**

    O ADR 0027 par.5 e literal: do piloto saem `sigma_e`, `tau` e a forma da
    cauda, e nada mais. O `p10` desta serie NAO produz override de parametro -
    quem calibra le `serie_e2_da_calibracao`, sobre janela propria.

    **Ordenada, e nao um conjunto.** O block bootstrap precisa da ordem: ele
    reamostra BLOCOS contiguos justamente para preservar a dependencia que o
    embaralhamento destruiria.
    """
    return _serie(
        conn, sql_finalidade="finalidade = 'piloto'", contrato=contrato,
        venue=venue, symbol=symbol, lado=lado, de_ms=de_ms,
        ate_ms_exclusive=ate_ms_exclusive,
        apenas_dentro_do_l1=apenas_dentro_do_l1, regime=regime,
    )


def serie_e2_da_calibracao(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    lado: str, de_ms: int | None = None, ate_ms_exclusive: int | None = None,
    apenas_dentro_do_l1: bool = True, regime: str | None = None,
) -> list[int]:
    """`E2` da CALIBRACAO. A unica serie que vira override de `spread_bps`.

    `apenas_dentro_do_l1` e o default porque o ADR 0027 exclui da estatistica a
    observacao cujo nocional passa do tamanho cotado - ali o preco do topo de
    livro nao descreve o que a ordem encontraria. Ela continua GRAVADA.
    """
    return _serie(
        conn, sql_finalidade="finalidade = 'calibracao'", contrato=contrato,
        venue=venue, symbol=symbol, lado=lado, de_ms=de_ms,
        ate_ms_exclusive=ate_ms_exclusive,
        apenas_dentro_do_l1=apenas_dentro_do_l1, regime=regime,
    )


def serie_e2_da_revalidacao(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    lado: str, de_ms: int | None = None, ate_ms_exclusive: int | None = None,
    apenas_dentro_do_l1: bool = True, regime: str | None = None,
) -> list[int]:
    """`E2` da REVALIDACAO. Confirma, e nunca reajusta.

    **O `regime` e obrigatorio na pratica**, e quem o exige e `ajuste.revalidar`:
    a janela de revalidacao e POR REGIME desde o ADR 0041, e ler a janela
    inteira sem filtrar faria instantes de `vol_alta` confirmarem o override de
    `vol_baixa`. Ele fica opcional na assinatura porque `_serie` e comum as
    tres finalidades; a obrigatoriedade mora no chamador, que nao tem default.
    """
    return _serie(
        conn, sql_finalidade="finalidade = 'revalidacao'", contrato=contrato,
        venue=venue, symbol=symbol, lado=lado, de_ms=de_ms,
        ate_ms_exclusive=ate_ms_exclusive,
        apenas_dentro_do_l1=apenas_dentro_do_l1, regime=regime,
    )


def quantas(
    conn: sqlite3.Connection, *, finalidade: str, contrato: str, venue: str,
    symbol: str,
) -> dict:
    """Quantas observacoes existem, por finalidade. **Fato, nao data.**

    Usado pelo criterio 5 do incremento 18, que antes era um proxy de "a janela
    fechou" - e fechar a janela nao mede nada (ADR 0041 par.6).
    """
    if finalidade not in FINALIDADES:
        raise ValueError(f"finalidade {finalidade!r} nao existe")
    por_regime = {
        str(r["regime"]): int(r["n"])
        for r in conn.execute(
            "SELECT regime, COUNT(*) AS n FROM calibracao_observacao"
            " WHERE finalidade = ? AND contrato = ? AND venue = ?"
            "   AND symbol = ? GROUP BY regime ORDER BY regime",
            (finalidade, contrato, venue, symbol),
        )
    }
    linha = conn.execute(
        "SELECT COUNT(*) AS total,"
        "       SUM(dentro_do_l1) AS na_estatistica,"
        "       COUNT(DISTINCT t_grid_ms) AS instantes,"
        "       MIN(t_grid_ms) AS de_ms, MAX(t_grid_ms) AS ate_ms"
        "  FROM calibracao_observacao"
        " WHERE finalidade = ? AND contrato = ? AND venue = ? AND symbol = ?",
        (finalidade, contrato, venue, symbol),
    ).fetchone()
    identidades = conn.execute(
        "SELECT DISTINCT config_version_id, calibracao_perfil_hash,"
        "       janela_hash"
        "  FROM calibracao_observacao"
        " WHERE finalidade = ? AND contrato = ? AND venue = ? AND symbol = ?",
        (finalidade, contrato, venue, symbol),
    ).fetchall()
    return {
        "finalidade": finalidade,
        "observacoes": int(linha["total"] or 0),
        "na_estatistica": int(linha["na_estatistica"] or 0),
        "instantes_distintos": int(linha["instantes"] or 0),
        "de_ms": linha["de_ms"],
        "ate_ms": linha["ate_ms"],
        "identidades": [
            {
                "config_version_id": int(r["config_version_id"]),
                "calibracao_perfil_hash": r["calibracao_perfil_hash"],
                "janela_hash": r["janela_hash"],
            }
            for r in identidades
        ],
        "identidade_unica": len(identidades) <= 1,
        "por_regime": por_regime,
    }
