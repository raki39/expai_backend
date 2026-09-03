"""Purga e embargo, derivados do catalogo (D28, secao 8.4).

> "Validacao cruzada em series temporais usa **purga e embargo** entre treino e
> teste, para impedir vazamento por sobreposicao de janelas." — secao 8.4

## O que cada um impede

**Purga** e o intervalo removido do fim do treino. Uma barra de teste calculada
com um indicador de 400 periodos usa as 400 barras anteriores a ela - e se
alguma delas for de treino, o resultado do teste ja viu treino. Nao e um
vazamento sutil: e o indicador funcionando como projetado, sobre dados do lado
errado da fronteira.

**Embargo** e o intervalo removido do INICIO do teste, depois da purga. Ele
cobre a dependencia serial residual que sobra quando a sobreposicao mecanica
ja foi removida: retornos proximos no tempo continuam correlacionados mesmo
sem indicador nenhum compartilhado. E o desenho de Lopez de Prado, e 1% e o
valor usual.

## Derivado, nunca constante - e a derivacao ja pegou um erro

A D28 recomendou "purga = 200 barras (50 h)", com o raciocinio de que "o maior
lookback do catalogo fechado e a media de 200 periodos".

**Estava errado.** `CruzamentoMedias.lenta` aceita ate **400**. Uma purga de
200 deixaria passar metade do alcance da familia mais usada do catalogo - a
mesma do B3 - e o vazamento nao apareceria em teste nenhum, porque o numero
pareceria deliberado.

Derivar do catalogo pegou isso na primeira execucao. E o motivo pelo qual a
propria D28 dizia "derivar do catalogo, nao fixar como constante": nao era
elegancia, era isto.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..regra.schema import BandaDesvio, BreakoutCanal, CruzamentoMedias

# Embargo em partes por dez mil da janela de teste. 1% e o valor usual de
# Lopez de Prado; em bps para nao haver ponto flutuante no caminho que decide
# fronteira de dado.
EMBARGO_BPS = 100


@dataclass(frozen=True)
class Separacao:
    purga_barras: int
    embargo_barras: int
    purga_origem: str

    @property
    def total_barras(self) -> int:
        return self.purga_barras + self.embargo_barras


def _teto_do_campo(modelo, campo: str) -> int:
    """O maior valor que o catalogo aceita naquele campo.

    Lido dos metadados do proprio pydantic, e nao de uma copia: uma copia
    envelheceria em silencio no dia em que a faixa mudasse, que e como este
    projeto ja se enganou oito vezes.
    """
    info = modelo.model_fields[campo]
    for restricao in info.metadata or ():
        teto = getattr(restricao, "le", None)
        if teto is not None:
            return int(teto)
    raise ValueError(
        f"{modelo.__name__}.{campo} nao declara teto; a purga nao pode ser"
        " derivada de uma faixa aberta"
    )


def maior_lookback() -> tuple[int, str]:
    """O maior alcance retrospectivo que uma regra do catalogo pode ter.

    Devolve `(barras, origem)`. A origem viaja junto porque um numero sozinho
    nao diz de onde veio - e e exatamente essa a diferenca entre uma purga
    derivada e uma purga digitada.
    """
    candidatos = [
        (_teto_do_campo(CruzamentoMedias, "lenta"), "CruzamentoMedias.lenta"),
        (_teto_do_campo(CruzamentoMedias, "rapida"), "CruzamentoMedias.rapida"),
        (_teto_do_campo(BandaDesvio, "periodo"), "BandaDesvio.periodo"),
        (_teto_do_campo(BreakoutCanal, "periodo"), "BreakoutCanal.periodo"),
    ]
    barras, origem = max(candidatos)
    return barras, f"maior lookback do catalogo: {origem} = {barras}"


def separacao(janela_de_teste_barras: int) -> Separacao:
    """Purga e embargo para uma janela de teste desse tamanho.

    Embargo arredondado para CIMA: um embargo menor que a conta manda deixa
    dependencia residual atravessar a fronteira, e o erro que se comete de
    graca e o do lado conservador.
    """
    if janela_de_teste_barras <= 0:
        raise ValueError("a janela de teste precisa ter barras")
    purga, origem = maior_lookback()
    embargo = -(-janela_de_teste_barras * EMBARGO_BPS // 10_000)
    return Separacao(
        purga_barras=purga, embargo_barras=embargo, purga_origem=origem
    )
