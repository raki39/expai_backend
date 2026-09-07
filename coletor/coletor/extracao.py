"""Extracao da amostra alinhada a grade. ADR 0032, contrato `bbo@1`.

O coletor grava 86.400 amostras por dia. A calibracao usa **96** - uma por
barra de 15 min -, e o numero nao e escolha de engenharia: o ADR 0027 conta em
BARRAS ("mediana de 1.114 barras em 90 dias", e 90 x 96 = 8.640).

O arquivo bruto **nunca sai de Singapura**. O que atravessa e este derivado,
0,11% do fluxo.

## A regra, e ela e uma so

    a ULTIMA amostra com `received_at_corrigido <= t_grid`,
    com `t_grid - corrigido <= tolerancia`

Tres palavras carregam o desenho inteiro:

**CORRIGIDO.** Comparar relogio local com hora de exchange sem corrigir o
offset erra por mais que a tolerancia inteira - o coletor mediu **-2.450 ms**
numa maquina real, contra 2.000 ms de tolerancia. A correcao usa a ultima
medida **anterior** a amostra; sem nenhuma, a linha e pulada e o instante cai
em `sem_amostra_na_janela`. Corrigir por zero seria assumir deriva zero, que e
o que o usuario recusou ao fechar o ADR 0028.

E a IDADE dessa medida vai gravada em cada observacao. Sem ela, um offset de
seis horas atras seria indistinguivel de um de trinta segundos - e nenhum
limiar de validade e escolhido aqui, porque a taxa de deriva ainda nao foi
medida. Grava-se o numero; o criterio e declarado quando houver medicao.

**`<=`.** Uma cotacao posterior ao instante e o futuro dele. A `api` recusa
defasagem negativa por CHECK, entao um erro aqui vira 422 e nao dado ruim.

**ULTIMA.** A mais proxima por baixo, e nao a primeira nem uma media - media
de duas cotacoes e uma cotacao que nunca existiu.

## Deterministica, e e isso que torna a chave idempotente honesta

Mesmo arquivo bruto e mesmo contrato produzem exatamente a mesma amostra. E
por isso que conteudo diferente para a mesma chave e ERRO GRAVE na `api`: sob
determinismo, divergir significa que o bruto mudou ou a regra mudou sem trocar
de contrato.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator

from .arquivo import ler

log = logging.getLogger("coletor")

# As MESMAS da ingestao historica e do rele (ADR 0030). Escala diferente faria
# o mesmo dado ter representacao diferente conforme o caminho que o trouxe.
PRICE_SCALE_EXP = 8
VOLUME_SCALE_EXP = 8

CONTRATO = "bbo@1"
GRADE_MS = 900_000
TOLERANCIA_MS = 2_000


def _inteiro(texto: str, expoente: int) -> int:
    """`Decimal`, nunca `float`. Regra 5, e a razao aparece aqui.

    A `api` compara estes inteiros por IGUALDADE contra o que ja gravou, e
    `float("0.1") * 10**8` nao da 10000000 exato em toda plataforma - duas
    extracoes da mesma linha discordariam, e a divergencia apareceria como
    corrupcao de dado.
    """
    return int((Decimal(texto) * (Decimal(10) ** expoente)).to_integral_value())


@dataclass(frozen=True)
class Observacao:
    """O que atravessa. `disponivel=False` NUNCA carrega preco."""

    t_grid_ms: int
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
    # QUANDO o relogio foi medido. Sem isto, um offset de 6 horas atras e
    # indistinguivel de um de 30 segundos - e o requisito 2 do ADR 0032
    # pede a incerteza do relogio, da qual a idade faz parte.
    relogio_medido_em_ms: int | None = None

    def como_corpo(self) -> dict[str, Any]:
        return {
            "t_grid_ms": self.t_grid_ms, "disponivel": self.disponivel,
            "motivo": self.motivo,
            "bid": self.bid, "bid_qty": self.bid_qty,
            "ask": self.ask, "ask_qty": self.ask_qty, "u": self.u,
            "received_at_ms": self.received_at_ms,
            "received_at_corrigido_ms": self.received_at_corrigido_ms,
            "sampled_at_ms": self.sampled_at_ms,
            "defasagem_ms": self.defasagem_ms,
            "offset_us": self.offset_us, "rtt_us": self.rtt_us,
            "incerteza_residual_us": self.incerteza_residual_us,
            "relogio_medido_em_ms": self.relogio_medido_em_ms,
        }


def corrigir(received_at_ms: int, offset_us: int) -> int:
    """A regra do horario corrigido - a MESMA que `app/aovivo/bbo.py` aplica.

    Inteira de proposito: a `api` recalcula e compara por IGUALDADE. Com float
    os dois lados discordariam por arredondamento, e a divergencia chegaria
    disfarcada de corrupcao de dado.

    Duas copias da mesma formula em repositorios diferentes e uma divida
    conhecida: ha teste do lado da `api` afirmando o valor para uma entrada
    fixa, e ele quebra se um dos lados mudar.
    """
    return received_at_ms + offset_us // 1000


@dataclass
class _Relogio:
    """A ultima medida de relogio ANTERIOR ao instante em questao."""

    offset_us: int
    rtt_us: int
    incerteza_residual_us: int
    medido_em_ms: int


def _linhas_ordenadas(arquivos: Iterable[Path]) -> Iterator[dict[str, Any]]:
    for caminho in sorted(arquivos):
        yield from ler(caminho)


def extrair(
    arquivos: Iterable[Path], *,
    de_ms: int,
    ate_ms_exclusive: int,
    grade_ms: int = GRADE_MS,
    tolerancia_ms: int = TOLERANCIA_MS,
) -> list[Observacao]:
    """Uma observacao por instante da grade em `[de_ms, ate)`.

    **Todo instante produz observacao**, inclusive os sem cotacao utilizavel -
    eles saem `disponivel=False` com motivo, e entram na cobertura. Pular o
    instante ruim faria a cobertura parecer perfeita justamente onde ela nao e.
    """
    if de_ms % grade_ms != 0:
        de_ms = (de_ms // grade_ms + 1) * grade_ms

    instantes = list(range(de_ms, ate_ms_exclusive, grade_ms))
    if not instantes:
        return []

    saida: list[Observacao] = []
    proximo = 0
    relogio: _Relogio | None = None
    # A melhor candidata para o instante corrente: a ultima vista com
    # `corrigido <= t_grid`.
    candidata: tuple[int, dict[str, Any], _Relogio] | None = None

    def fechar(t_grid: int) -> Observacao:
        if candidata is None:
            return Observacao(t_grid_ms=t_grid, disponivel=False,
                              motivo="sem_amostra_na_janela")
        corrigido, linha, rel = candidata
        defasagem = t_grid - corrigido
        if defasagem > tolerancia_ms:
            return Observacao(t_grid_ms=t_grid, disponivel=False,
                              motivo="defasada")
        if not linha.get("disponivel"):
            return Observacao(t_grid_ms=t_grid, disponivel=False,
                              motivo=linha.get("motivo") or "sem_mensagem")
        return Observacao(
            t_grid_ms=t_grid, disponivel=True,
            bid=_inteiro(linha["bid"], PRICE_SCALE_EXP),
            bid_qty=_inteiro(linha["bid_qty"], VOLUME_SCALE_EXP),
            ask=_inteiro(linha["ask"], PRICE_SCALE_EXP),
            ask_qty=_inteiro(linha["ask_qty"], VOLUME_SCALE_EXP),
            u=linha.get("u"),
            received_at_ms=linha["received_at_ns"] // 1_000_000,
            received_at_corrigido_ms=corrigido,
            sampled_at_ms=linha["sampled_at_ns"] // 1_000_000,
            defasagem_ms=defasagem,
            offset_us=rel.offset_us, rtt_us=rel.rtt_us,
            incerteza_residual_us=rel.incerteza_residual_us,
            relogio_medido_em_ms=rel.medido_em_ms,
        )

    for linha in _linhas_ordenadas(arquivos):
        if linha.get("tipo") == "relogio":
            # SONDA FALHADA nao traz medicao, e a linha existe assim de
            # proposito: `fluxo.sondar_relogio` grava
            # `{tipo, medido_em_ns, falha}` porque "um relogio nao medido e um
            # relogio nao medido" - ela nao inventa valor.
            #
            # Esta versao supunha que toda linha `tipo: relogio` trouxesse
            # medicao, e quebrou com `KeyError: 'offset_ms'` na PRIMEIRA volta
            # em producao. A extracao nao decide nada sobre o relogio: quem ja
            # tinha medida boa continua com ela.
            #
            # E manter a anterior e o certo, e nao so o conveniente: a sonda
            # falhar informa sobre a REDE ate a Binance, e nao sobre o relogio
            # local. O offset e propriedade que varia devagar; a falha da sonda
            # nao o torna desconhecido, so o deixa mais VELHO - e a idade dele
            # vai gravada em cada observacao, para que ninguem precise supor.
            if "offset_ms" not in linha:
                continue
            relogio = _Relogio(
                offset_us=round(float(linha["offset_ms"]) * 1000),
                rtt_us=round(float(linha["rtt_ms"]) * 1000),
                incerteza_residual_us=round(
                    float(linha["incerteza_residual_ms"]) * 1000
                ),
                medido_em_ms=int(linha["medido_em_ns"]) // 1_000_000,
            )
            continue

        recebido_ns = linha.get("received_at_ns")
        if recebido_ns is None:
            # Amostra sem cotacao nenhuma (boot, ou queda longa). Ela ainda
            # marca o tempo, mas nao pode ser corrigida nem alinhada.
            continue
        if relogio is None:
            # SEM MEDIDA DE RELOGIO ANTERIOR. Corrigir por zero seria assumir
            # deriva zero, que e o que o usuario recusou explicitamente ao
            # fechar o ADR 0028 - e o offset medido em maquina real passa da
            # tolerancia inteira.
            continue

        corrigido = corrigir(recebido_ns // 1_000_000, relogio.offset_us)

        while proximo < len(instantes) and corrigido > instantes[proximo]:
            saida.append(fechar(instantes[proximo]))
            proximo += 1
        if proximo >= len(instantes):
            break
        candidata = (corrigido, linha, relogio)

    while proximo < len(instantes):
        saida.append(fechar(instantes[proximo]))
        proximo += 1

    # A observacao `sem_relogio` nao sai do laco acima: sem medida anterior a
    # linha foi PULADA, e o instante cai em `sem_amostra_na_janela`. Isso e
    # deliberado - "nao havia cotacao utilizavel" e verdade nos dois casos, e
    # inventar um motivo que a `api` teria de aceitar so para distinguir a
    # causa acrescentaria vocabulario sem acrescentar informacao acionavel: a
    # acao e a mesma, e a causa esta no log do coletor.
    return saida
