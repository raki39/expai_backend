"""R82: nenhum caminho de leitura do coletor a partir do agente ou do validador.

O coletor (ADR 0028) e servico proprio, com volume proprio. A separacao nao e
convencao: e fronteira de importacao verificavel, como ja e a das maos rapidas
(regra 3) e a das quatro janelas (incremento 9).

**A R83 foi REESCRITA no ADR 0028**, e a diferenca importa para este arquivo:

    o coletor NAO participa de nenhuma DECISAO da Fase 0
    o coletor PARTICIPA da MEDICAO de calibracao (ADR 0027)

A frase da D32 - "o unico ganho e historico que so a Fase 1-2 consome" - deixou
de ser verdade quando a D45 fechou, porque o calibrador le o BBO gravado aqui.
Entao a guarda nao pode ser "ninguem toca no coletor": ela e "quem toca no
coletor esta nesta lista, e a lista e curta".

Hoje a lista esta VAZIA, porque o calibrador ainda nao existe. Quando ele
entrar, alguem tem de vir aqui e escrever o nome dele - que e exatamente o
efeito que se quer.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

# Modulos autorizados a alcancar o dado do coletor. Vazia de proposito: o
# calibrador do incremento 18 entra aqui, por decisao explicita, e nao por
# alguem ter escrito um import sem ninguem notar.
PODEM_LER_O_COLETOR: frozenset[str] = frozenset()

# Como o coletor e reconhecido de dentro do `api`. Ele nao e um pacote
# importavel daqui (servico separado, imagem separada), entao o que se procura
# e o nome do diretorio, da variavel de ambiente e do arquivo.
MARCAS = ("COLETOR_DIR", "bookticker-", "coletor.amostra", "coletor.arquivo")


def _codigo_sem_prosa(fonte: str) -> str:
    """Remove docstrings, mantendo o resto.

    Sem isto, a guarda acusaria a propria explicacao de por que o coletor e
    separado - foi o que aconteceu no incremento 11 com `app/estatistica`, e a
    licao foi que uma guarda que proibe explicar o que ela proibe empurra para
    apagar o comentario.
    """
    try:
        arvore = ast.parse(fonte)
    except SyntaxError:
        return fonte
    docs = set()
    for no in ast.walk(arvore):
        if isinstance(no, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            d = ast.get_docstring(no, clean=False)
            if d:
                docs.add(d)
    linhas = fonte.splitlines()
    for d in docs:
        for linha_doc in d.splitlines():
            linhas = [l for l in linhas if l.strip() != linha_doc.strip() or not linha_doc.strip()]
    return "\n".join(linhas)


def _modulos():
    for py in sorted(APP.rglob("*.py")):
        yield py, _codigo_sem_prosa(py.read_text(encoding="utf-8"))


def test_nenhum_modulo_de_app_alcanca_o_dado_do_coletor():
    infratores: list[str] = []
    for py, codigo in _modulos():
        nome = py.relative_to(APP).as_posix()
        if nome in PODEM_LER_O_COLETOR:
            continue
        for marca in MARCAS:
            if marca in codigo:
                infratores.append(f"{nome}: cita {marca!r}")
    assert not infratores, (
        "R82: modulo de `app/` alcancando o dado do coletor sem estar em "
        f"PODEM_LER_O_COLETOR. {infratores}"
    )


def test_o_cerebro_e_o_validador_nunca_entram_na_lista():
    """A lista pode crescer; estes dois nao podem entrar nela.

    R82 e literal: "sem nenhum caminho de leitura a partir do agente ou do
    validador". O calibrador pode ler; o agente que formula hipotese e o
    validador que emite parecer, nao. Se um dia alguem os acrescentar por
    conveniencia, este teste quebra antes do merge.
    """
    proibidos = [m for m in PODEM_LER_O_COLETOR
                 if m.startswith(("cerebro/", "validador/"))]
    assert not proibidos, (
        f"R82 proibe: {proibidos}. O coletor nao participa de DECISAO."
    )


def test_a_guarda_nao_e_vazia():
    """Ela varre modulos de verdade - senao passaria por nao encontrar nada."""
    assert sum(1 for _ in _modulos()) > 50
