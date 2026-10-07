"""O regime de cada instante do fluxo. UMA definicao, e ela mora aqui.

A funcao vivia dentro de `ajuste.py`, e `dimensionamento.py` a importava de la
- um modulo de estimativa sendo a casa de um fato do MERCADO. Ela saiu porque
`observacao.py` passou a precisar dela para gravar `calibracao_observacao.regime`,
e importa-la de `ajuste` criaria ciclo (`ajuste` importa `observacao`).

**O regime e GRAVADO com a observacao desde o ADR 0041**, e nao mais derivado a
cada leitura. Esta funcao continua sendo a unica que o calcula; o que mudou e
que agora ela e chamada UMA vez, na materializacao, e o resto do sistema le a
coluna. Duas derivacoes do mesmo fato em modulos diferentes e o defeito que o
`n_efetivo` do run 30 ja custou.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from ..regime import deteccao

#: `indefinido` NAO e regime: e a declaracao de que ainda nao da para dizer.
#: Ele existe na observacao e nao existe em fechamento, override nem
#: revalidacao - e os `CHECK` das tres tabelas recusam.
INDEFINIDO = "indefinido"


def regimes_das_barras(
    conn: sqlite3.Connection, *, venue: str, symbol: str, timeframe: str,
) -> dict[int, str | None]:
    """O regime de cada instante do fluxo, pelo detector congelado do ADR 0026.

    **Classificar exige 672 barras ANTERIORES** (7 dias). Enquanto o fluxo nao
    tiver esse historico antes do piloto, todo instante sai `INDEFINIDO` - e
    `indefinido` NAO e um regime: e a declaracao de que ainda nao da para
    dizer. Trata-lo como quarto regime seria calibrar sobre uma classe que nao
    existe na taxonomia.
    """
    barras = [
        (int(r["open_time_ms"]), int(r["close"]), int(r["open"]))
        for r in conn.execute(
            "SELECT open_time_ms, open, close FROM stream_bar"
            " WHERE venue = ? AND symbol = ? AND timeframe = ?"
            " ORDER BY open_time_ms",
            (venue, symbol, timeframe),
        )
    ]
    if len(barras) < 2:
        return {}

    # Retorno que TERMINA em cada barra, em mili-bps, e `None` quando os
    # extremos nao sao temporalmente adjacentes - a subsecao 3b do ADR 0026.
    passo = barras[1][0] - barras[0][0]
    retornos: list[tuple[int, int | None]] = [(barras[0][0], None)]
    for anterior, atual in zip(barras, barras[1:]):
        if atual[0] - anterior[0] != passo:
            retornos.append((atual[0], None))
            continue
        r = (Decimal(atual[1] - anterior[1]) * 10_000 * 1_000
             / Decimal(anterior[1]))
        retornos.append((atual[0], int(r.to_integral_value())))

    return {
        c.open_time_ms: c.regime
        for c in deteccao.classificar_serie(retornos)
    }


def regime_do_instante(mapa: dict[int, str | None], t_grid_ms: int) -> str:
    """O nome que vai para a COLUNA, e nunca `None`.

    O detector devolve `None` para o instante que ainda nao da para
    classificar; a coluna e `NOT NULL` de proposito. `None` e `'indefinido'`
    dizem a mesma coisa - "ainda nao da para dizer" -, e deixar as duas
    representacoes conviverem faria a contagem por regime depender de qual
    caminho gravou a linha. `janela_incompleta` no detector e `indefinido` na
    calibracao ja sao o mesmo balde com dois nomes; a traducao acontece AQUI,
    num lugar so.
    """
    r = mapa.get(int(t_grid_ms))
    return INDEFINIDO if r is None else str(r)


def divergencias_do_gravado(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    timeframe: str,
) -> dict:
    """O regime GRAVADO contra o que o detector diria AGORA.

    ## Por que isto existe

    A classificacao e funcao do conteudo da janela, e a janela pode receber
    dado ATRASADO - o apagao de 19 a 21/09 foi preenchido por backfill, entao
    nao e hipotese. Barras FUTURAS nunca movem um instante (a janela e
    estritamente anterior, e ha teste); barras que chegam para DENTRO da janela
    movem.

    ## Qual dos dois vale

    O **gravado**. Ele e a classificacao no instante da materializacao, e e
    sobre ela que a calibracao daquele regime parou, fechou e eventualmente
    produziu override. Recalcular e trocar a serie que calibrou pela serie que
    existe hoje - o que a parada por `n_exigido` existe para impedir.

    O recalculo serve para **ver**, e nao para corrigir. Uma divergencia e um
    fato a declarar: ela diz que o dado mudou debaixo de uma medicao ja feita.
    Silenciar isso seria o padrao que este projeto conta trinta e duas vezes.
    """
    mapa = regimes_das_barras(
        conn, venue=venue, symbol=symbol, timeframe=timeframe
    )
    fora: list[dict] = []
    total = 0
    for linha in conn.execute(
        "SELECT DISTINCT t_grid_ms, finalidade, regime FROM calibracao_observacao"
        " WHERE contrato = ? AND venue = ? AND symbol = ?"
        " ORDER BY t_grid_ms",
        (contrato, venue, symbol),
    ):
        total += 1
        agora = regime_do_instante(mapa, int(linha["t_grid_ms"]))
        if agora != str(linha["regime"]):
            fora.append({
                "t_grid_ms": int(linha["t_grid_ms"]),
                "finalidade": str(linha["finalidade"]),
                "gravado": str(linha["regime"]),
                "recalculado_agora": agora,
            })
    return {
        "instantes_conferidos": total,
        "divergentes": len(fora),
        "amostra": fora[:20],
        "qual_vale": (
            "o GRAVADO. Ele e a classificacao no instante da materializacao, e"
            " e sobre ela que a calibracao daquele regime parou e fechou."
            " Recalcular trocaria a serie que calibrou pela serie que existe"
            " hoje"
        ),
        "o_que_uma_divergencia_significa": (
            "dado chegou ATRASADO para dentro da janela de 672 barras daquele"
            " instante - backfill. Barra FUTURA nunca move um instante: a"
            " janela e estritamente anterior, e ha teste"
        ),
    }
