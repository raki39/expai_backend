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
from dataclasses import dataclass
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
    return {
        c.open_time_ms: c.regime
        for c in deteccao.classificar_serie(
            _retornos(conn, venue=venue, symbol=symbol, timeframe=timeframe)
        )
    }


def _retornos(
    conn: sqlite3.Connection, *, venue: str, symbol: str, timeframe: str,
) -> list[tuple[int, int | None]]:
    """A serie de retornos do fluxo, UMA vez.

    Ela e o insumo tanto do regime por instante quanto da cobertura por janela,
    e duas construcoes dela divergiriam no primeiro ajuste - o defeito do
    `n_efetivo` do run 30.
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

    return retornos


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


# ===========================================================================
# A TRAVA CONTRA MATERIALIZAR ANTES DO BACKFILL
#
# Exigencia do usuario em 2026-10-07, e ela fecha o caso que as barras futuras
# nao fechavam:
#
#   > "Nao quero que uma ausencia temporaria recuperavel seja gravada como
#   > `indefinido` e passe a valer para sempre."
#
# Classificar e funcao do CONTEUDO da janela causal. Barra futura nunca a toca
# (ha teste nos tres niveis). Barra que chega ATRASADA para DENTRO dela toca -
# e o apagao de 19 a 21/09 foi preenchido por backfill, entao nao e hipotese.
#
# A distincao que decide e **recuperavel x definitivo**, e ela e DERIVADA do
# proprio fluxo, nunca de uma constante que envelhece:
#
#   | o instante `t` | a ausencia e | e entao |
#   |---|---|---|
#   | `watermark <= t` | recuperavel: o rele nao passou dele | ADIA |
#   | falta barra DENTRO de `[inicio, watermark]` | recuperavel: o backfill ainda pode trazer | ADIA |
#   | a janela alcanca ANTES do inicio do fluxo | DEFINITIVA: o fluxo nao existia | materializa `indefinido` |
#   | janela completa | — | materializa o regime |
#
# ## Por que NAO ha relogio de parede aqui
#
# A condicao "a barra do instante esta fechada" parece pedir o relogio. Ela nao
# pede, e derivar do FLUXO e mais forte:
#
# O rele so busca kline FECHADA - `rele/binance.py` descarta a em formacao,
# explicitamente, porque "ela muda a cada negocio". Mas isso e disciplina do
# PRODUTOR, e garantia que depende do bom comportamento do outro lado ja foi
# violada (secao 8.5.1). O que se exige aqui e localmente verificavel:
# **`watermark > t`**. Se uma barra em formacao algum dia escorregasse, ela
# seria a de MAIOR `open_time_ms` - a fronteira. Exigir que exista barra DEPOIS
# de `t` exclui exatamente esse caso, sem perguntar a hora a ninguem.
#
# E o efeito colateral e o que a regra 13 pede: o veredito e funcao do DADO, e
# nao do instante da leitura. Duas materializacoes sobre o mesmo fluxo tomam a
# mesma decisao - inclusive em teste, inclusive daqui a um ano.
#
# A quarta linha e a unica que grava `indefinido`, e ela e honesta: nenhum
# backfill traz barra de antes da primeira que o rele ja entregou. Se um dia
# alguem carregar historico mais antigo, o inicio do fluxo anda e aqueles
# instantes passam a ser classificaveis - e e exatamente por isso que a
# divergencia entre gravado e recalculado virou VIOLACAO DE INTEGRIDADE, e nao
# mais diagnostico.
# ===========================================================================

ADIAR = "adiar"
MATERIALIZAR = "materializar"

#: Os motivos, fechados. Cada um diz de qual linha da tabela acima ele veio.
MOTIVOS = (
    "watermark_do_rele_nao_passou_do_instante",
    "janela_causal_incompleta",
    "antes_do_inicio_do_fluxo",
    "janela_causal_completa",
)


@dataclass(frozen=True)
class CoberturaDoFluxo:
    """O que o rele JA entregou, em forma de pergunta por instante.

    `presente_ate` e uma contagem acumulada por indice de grade: a janela de
    `t` esta completa quando o acumulado dela vale exatamente `janela_barras`.
    Sem isso seriam 672 consultas por instante, e a materializacao do piloto
    faria 900 mil.
    """

    inicio_ms: int | None
    watermark_ms: int | None
    passo_ms: int
    janela_barras: int
    _base_ms: int
    _acumulado: tuple[int, ...]

    def _indice(self, t_grid_ms: int) -> int:
        return (t_grid_ms - self._base_ms) // self.passo_ms

    def _presentes_em(self, de_ms: int, ate_ms_exclusive: int) -> int:
        """Quantas barras existem em `[de, ate)`, pelos acumulados."""
        if not self._acumulado:
            return 0
        a = max(0, min(len(self._acumulado) - 1, self._indice(de_ms)))
        b = max(0, min(len(self._acumulado) - 1, self._indice(ate_ms_exclusive)))
        return self._acumulado[b] - self._acumulado[a]

    def veredito(self, *, t_grid_ms: int) -> tuple[str, str]:
        """`(acao, motivo)` para um instante. A ordem das perguntas importa.

        **Sem `agora_ms`, de proposito.** Ver a nota do topo: a condicao de
        barra fechada sai do fluxo (`watermark > t`), e nao do relogio - o que
        torna o veredito funcao do DADO e reproduzivel (regra 13).
        """
        if self.inicio_ms is None or self.watermark_ms is None:
            return ADIAR, "watermark_do_rele_nao_passou_do_instante"
        # 1. O RELE TEM DE TER PASSADO DO INSTANTE. Isto cobre duas coisas:
        #    que ele chegou la, e que a barra de `t` nao e a fronteira - a
        #    unica que poderia nao estar fechada.
        if self.watermark_ms <= t_grid_ms:
            return ADIAR, "watermark_do_rele_nao_passou_do_instante"
        # 2. A janela alcanca antes do inicio do fluxo: DEFINITIVO. Nenhum
        #    backfill traz barra de antes da primeira que o rele entregou.
        inicio_da_janela = t_grid_ms - self.janela_barras * self.passo_ms
        if inicio_da_janela < self.inicio_ms:
            return MATERIALIZAR, "antes_do_inicio_do_fluxo"
        # 3. Dentro do alcance do rele, falta barra: RECUPERAVEL, e adia.
        if self._presentes_em(inicio_da_janela, t_grid_ms) < self.janela_barras:
            return ADIAR, "janela_causal_incompleta"
        return MATERIALIZAR, "janela_causal_completa"


def cobertura_do_fluxo(
    conn: sqlite3.Connection, *, venue: str, symbol: str, timeframe: str,
    janela_barras: int = deteccao.JANELA_BARRAS,
) -> CoberturaDoFluxo:
    """O inicio, o watermark e a presenca por instante, numa leitura so."""
    linhas = [
        int(r["open_time_ms"]) for r in conn.execute(
            "SELECT open_time_ms FROM stream_bar"
            " WHERE venue = ? AND symbol = ? AND timeframe = ?"
            " ORDER BY open_time_ms",
            (venue, symbol, timeframe),
        )
    ]
    if len(linhas) < 2:
        return CoberturaDoFluxo(
            inicio_ms=linhas[0] if linhas else None,
            watermark_ms=linhas[-1] if linhas else None,
            passo_ms=1, janela_barras=janela_barras,
            _base_ms=linhas[0] if linhas else 0, _acumulado=(),
        )

    passo = linhas[1] - linhas[0]
    base = linhas[0]
    tamanho = (linhas[-1] - base) // passo + 2
    acumulado = [0] * (tamanho + 1)
    presentes = set(linhas)
    for i in range(tamanho):
        t = base + i * passo
        acumulado[i + 1] = acumulado[i] + (1 if t in presentes else 0)
    return CoberturaDoFluxo(
        inicio_ms=base, watermark_ms=linhas[-1], passo_ms=passo,
        janela_barras=janela_barras, _base_ms=base,
        _acumulado=tuple(acumulado),
    )


def cobertura_da_janela(
    conn: sqlite3.Connection, *, venue: str, symbol: str, timeframe: str,
    de_ms: int, ate_ms_exclusive: int,
) -> dict:
    """Cobertura e episodios **restritos a uma janela SELADA**.

    `deteccao.cobertura` sobre a serie inteira responde outra pergunta: quantos
    regimes o FLUXO cobriu ate agora. Para uma janela selada isso e errado em
    direcao perigosa - barras posteriores a `ate_ms_exclusive` continuariam
    crescendo a corrida e podendo QUALIFICAR um episodio que, dentro da janela,
    nunca alcancou a permanencia.

    Uma janela selada e um periodo fechado: o que acontece depois dela nao
    pertence a ela. O recorte e feito ANTES de `episodios`, e nao depois - uma
    corrida que atravessa a borda e truncada na borda, que e o que "dentro da
    janela" significa.
    """
    classificacoes = [
        c for c in deteccao.classificar_serie(
            _retornos(conn, venue=venue, symbol=symbol, timeframe=timeframe)
        )
        if de_ms <= c.open_time_ms < ate_ms_exclusive
    ]
    cob = deteccao.cobertura(classificacoes)
    return {
        **cob,
        "de_ms": de_ms,
        "ate_ms_exclusive": ate_ms_exclusive,
        "instantes_na_janela": len(classificacoes),
        "o_recorte_e_ANTES_dos_episodios": (
            "uma corrida que atravessa a borda e TRUNCADA nela. Classificar"
            " tudo e cortar depois deixaria barra posterior a janela qualificar"
            " um episodio que, dentro dela, nunca alcancou a permanencia"
        ),
    }
