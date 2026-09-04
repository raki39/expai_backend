"""Isolar CODIGO da PROSA, para guardas que varrem fonte.

Existe porque uma guarda que busca texto cru acusa a **explicacao** de por que
algo e proibido - e a correcao errada e apagar a explicacao. O incremento 11
registrou isso em `app/estatistica`, o 12 em `b4/braco.py`, e o incremento 16
de novo, quando `app/aovivo/fluxo.py` explicou por que o fluxo nao aparece em
`bar_por_finalidade` e a guarda do incremento 9 o acusou.

**Uma definicao, e nao duas.** `sql_sem_prosa` nasceu dentro de
`tests/test_b4.py` e passou a ter dois usuarios no incremento 16. Duas copias
identicas divergem - e este projeto conta essa historia em `baselines.condicoes`
contra `contrato.condicoes_da_config`, e em `agente_estado` contra
`/api/baselines/curva`.

## Sao DUAS funcoes, e a diferenca importa

`sql_sem_prosa` mantem literais de UMA LINHA, porque o SQL deste projeto vive
neles: remove-los deixaria a guarda cega para o que ela procura.

`codigo_sem_prosa` (em `tests/test_estatistica.py`) remove TODO literal, porque
lá o alvo e uma palavra que pode aparecer em texto qualquer. Sao objetivos
opostos, e trocar um pelo outro cega uma das duas guardas.
"""

from __future__ import annotations

import pathlib
import tokenize

TRIPLAS = ('"""', "'''", 'r"""', "r'''", 'f"""', "f'''")


def sql_sem_prosa(arquivo: pathlib.Path) -> str:
    """O codigo sem comentarios e sem DOCSTRINGS, com o SQL intacto.

    Docstring e reconhecida pelas aspas triplas: o SQL do projeto e escrito em
    literais de uma linha, concatenados, e eles ficam.

    O que se isola aqui e a CONSULTA, e nao a explicacao dela.
    """
    pedacos: list[str] = []
    with arquivo.open("rb") as f:
        for tok in tokenize.tokenize(f.readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.string.startswith(TRIPLAS):
                continue
            pedacos.append(tok.string)
    return " ".join(pedacos)
