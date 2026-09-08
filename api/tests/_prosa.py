r"""Isolar CODIGO da PROSA, para guardas que varrem fonte.

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

`codigo_sem_prosa` remove TODO literal, porque
lá o alvo e uma palavra que pode aparecer em texto qualquer. Sao objetivos
opostos, e trocar um pelo outro cega uma das duas guardas.

**E eu troquei os dois, no mesmo dia, escrevendo guardas para a D47 e a D48**
(2026-09-08). Fica registrado porque o aviso acima ja existia e nao bastou:

| guarda | o que eu usei | o que acontecia |
|---|---|---|
| a rota de viabilidade passa a potencia explicitamente | `codigo_sem_prosa`, procurando a substring | os tokens saem JUNTOS COM ESPACO: `dimensionamento . POTENCIA_ALVO_PPM`. A substring nunca casa, e a guarda **passa com o defeito presente** |
| o bloco do DSR publica `n_bruto` e `n_efetivo` | `codigo_sem_prosa`, procurando as chaves | ele apaga TODO literal, entao `dsr_bloco["n_bruto"]` sai `dsr_bloco [ ]` — a guarda procurava uma chave que ela mesma tinha removido |

As duas correcoes:

1. **regex com `\s*`**, e nunca substring, quando o alvo atravessa mais de um
   token. E a licao do incremento 17, onde `app.state.conn` saiu
   `app . state . conn` e a guarda ficou vazia;
2. **`sql_sem_prosa` quando o alvo esta DENTRO de um literal** — chave de
   dicionario, nome de tabela, nome de campo.

A regra curta: **o alvo esta num literal? `sql_sem_prosa`. E uma palavra do
codigo? `codigo_sem_prosa`, e com regex se ela atravessa tokens.**
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


def codigo_sem_prosa(arquivo: pathlib.Path) -> str:
    """O codigo do arquivo, sem comentarios e sem literais de texto NENHUM.

    Existe porque a primeira versao da guarda de separacao acusou as proprias
    docstrings deste projeto: elas mencionam "credito" exatamente para
    explicar por que credito nao entra na estatistica.

    Uma guarda que proibe a palavra proibe tambem a explicacao de por que a
    palavra e proibida - e ai a saida e apagar o comentario, que e a pior das
    correcoes possiveis. O que se quer proibir e o **codigo**.

    Morava em `tests/test_estatistica.py` e veio para ca no incremento 20:
    este arquivo ja dizia, na propria docstring, que as duas funcoes sao um
    par - e um par declarado com as metades em arquivos diferentes e a forma
    do defeito que ele mesmo descreve.
    """
    pedacos: list[str] = []
    with arquivo.open("rb") as f:
        for tok in tokenize.tokenize(f.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            pedacos.append(tok.string)
    return " ".join(pedacos)
