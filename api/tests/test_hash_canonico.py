"""O alvo mede CONTEÚDO LÓGICO, e não o checkout. OP-2, paga em 2026-09-10.

> *"O hash dos módulos deve representar conteúdo lógico independente do
> checkout: arquivos-texto normalizar CRLF e CR para LF; caminhos relativos em
> formato POSIX; arquivos ordenados deterministicamente; política explícita
> para UTF-8/BOM; arquivos binários permanecem em bytes crus; incluir no
> fechamento a própria implementação da normalização."* — o usuário, 2026-09-10

## O defeito, medido antes de corrigir

```
local (Windows, 39 dos 69 arquivos com CRLF)   56d4a443ee51398b
producao (Linux, LF)                           8bfbef7e1914003a
local, normalizando CRLF -> LF                 8bfbef7e1914003a
```

Um certificado emitido no Linux **não conferia** no Windows sob o mesmo commit,
e quem tentasse auditar de outra máquina não saberia se a divergência era o
código ou o `git config core.autocrlf`.

## A normalização está DENTRO do que ela mede

`app.certificacao.alvo` está no fechamento transitivo (por `suite`), então
mudar a política muda o alvo — como tem de ser. E `POLITICA_CANONICA` entra no
próprio hash, para que a mudança de régua seja visível **no número** em vez de
produzir dois certificados incomparáveis que parecem descrever laboratórios
diferentes.
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from app.certificacao import alvo


# ---------------------------------------------------------------------------
# A exigência central: LF e CRLF dão o MESMO hash
# ---------------------------------------------------------------------------


def _escrever(pasta: pathlib.Path, nome: str, texto: str, fim: bytes) -> pathlib.Path:
    p = pasta / nome
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(texto.replace("\n", fim.decode()).encode("utf-8"))
    return p


def test_o_MESMO_modulo_em_LF_e_CRLF_da_hash_identico(tmp_path, monkeypatch):
    """A exigência que paga a OP-2, e ela é sobre o conteúdo lógico."""
    monkeypatch.setattr(alvo, "_RAIZ", tmp_path)
    fonte = "def f():\n    return 1\n\n\nX = 2\n"

    lf = _escrever(tmp_path / "lf", "m.py", fonte, b"\n")
    crlf = _escrever(tmp_path / "crlf", "m.py", fonte, b"\r\n")

    # Os BYTES sao diferentes - e essa e a nao-vacuidade do teste.
    assert lf.read_bytes() != crlf.read_bytes()
    assert b"\r\n" in crlf.read_bytes()

    # E o conteudo CANONICO e o mesmo.
    assert alvo.conteudo_canonico(lf) == alvo.conteudo_canonico(crlf)
    assert alvo.conteudo_canonico(crlf) == fonte.encode("utf-8")


def test_o_CR_solto_tambem_normaliza_e_a_ORDEM_das_substituicoes_importa():
    """`\\r\\n` primeiro, `\\r` depois — o contrário mudaria o conteúdo.

    Substituir `\\r` por `\\n` antes de tratar `\\r\\n` transformaria cada
    `\\r\\n` em `\\n\\n`: uma linha viraria duas, e o hash mediria um arquivo que
    não existe.
    """
    import tempfile

    pasta = pathlib.Path(tempfile.mkdtemp())
    fonte = "a\nb\nc\n"
    lf = _escrever(pasta, "lf.py", fonte, b"\n")
    crlf = _escrever(pasta, "crlf.py", fonte, b"\r\n")
    cr = _escrever(pasta, "cr.py", fonte, b"\r")

    canonico = fonte.encode("utf-8")
    assert alvo.conteudo_canonico(lf) == canonico
    assert alvo.conteudo_canonico(crlf) == canonico
    assert alvo.conteudo_canonico(cr) == canonico
    # E o resultado NAO tem linha em branco extra - a prova da ordem.
    assert b"\n\n" not in alvo.conteudo_canonico(crlf)


def test_o_BOM_utf8_e_removido_e_a_politica_diz_isso():
    """Abrir o arquivo no editor errado não pode virar recertificação.

    Python ignora o BOM ao compilar, então ele não muda o programa. Deixá-lo no
    hash faria um byte invisível pedir os cinco escopos de novo.
    """
    import tempfile

    pasta = pathlib.Path(tempfile.mkdtemp())
    sem = pasta / "sem.py"
    com = pasta / "com.py"
    sem.write_bytes(b"X = 1\n")
    com.write_bytes(b"\xef\xbb\xbfX = 1\n")

    assert sem.read_bytes() != com.read_bytes()
    assert alvo.conteudo_canonico(sem) == alvo.conteudo_canonico(com)


def test_BINARIO_fica_em_bytes_CRUS():
    """`0x0D0A` dentro de um PNG é dado, e não fim de linha.

    Hoje o fechamento é 100% `.py`. A regra existe para o dia em que não for —
    e normalizar bytes de imagem corromperia o hash de um arquivo que ninguém
    edita em editor de texto.
    """
    import tempfile

    pasta = pathlib.Path(tempfile.mkdtemp())
    p = pasta / "imagem.png"
    bruto = b"\x89PNG\r\n\x1a\n\x00\r\x00"
    p.write_bytes(bruto)

    assert alvo.conteudo_canonico(p) == bruto, (
        "os bytes do PNG foram normalizados: `\\r\\n` ali e dado"
    )
    assert ".png" not in alvo.EXTENSOES_DE_TEXTO


# ---------------------------------------------------------------------------
# NÃO-VACUIDADE: conteúdo diferente tem de dar hash diferente
# ---------------------------------------------------------------------------


def test_alteracao_REAL_de_conteudo_muda_o_hash(tmp_path, monkeypatch):
    """Um hash que nunca muda não vigia nada.

    A normalização não pode ter apagado a sensibilidade: uma linha a mais, um
    número trocado, um espaço no meio de um identificador — tudo isso é
    conteúdo lógico e tem de mover o alvo.
    """
    monkeypatch.setattr(alvo, "_RAIZ", tmp_path)
    base = _escrever(tmp_path, "m.py", "X = 1\n", b"\n")
    antes = alvo._hash_de_arquivos([base])

    for mudanca in ("X = 2\n", "X = 1\nY = 2\n", "X  = 1\n", "X = 1"):
        base.write_bytes(mudanca.encode("utf-8"))
        assert alvo._hash_de_arquivos([base]) != antes, (
            f"o conteudo mudou para {mudanca!r} e o hash nao se moveu"
        )


def test_RENOMEAR_o_modulo_muda_o_hash(tmp_path, monkeypatch):
    """Dois arquivos com o mesmo conteúdo em lugares diferentes são
    laboratórios diferentes — e é por isso que o caminho entra no hash."""
    monkeypatch.setattr(alvo, "_RAIZ", tmp_path)
    a = _escrever(tmp_path / "pac", "um.py", "X = 1\n", b"\n")
    b = _escrever(tmp_path / "pac", "dois.py", "X = 1\n", b"\n")
    assert alvo._hash_de_arquivos([a]) != alvo._hash_de_arquivos([b])


def test_mudar_a_POLITICA_muda_o_hash(tmp_path, monkeypatch):
    """A régua entra no número, e não fica implícita.

    Sem isso, trocar a canonicalização produziria dois certificados
    incomparáveis que parecem descrever laboratórios diferentes — quando o que
    mudou foi como se mede.
    """
    monkeypatch.setattr(alvo, "_RAIZ", tmp_path)
    p = _escrever(tmp_path, "m.py", "X = 1\n", b"\n")
    antes = alvo._hash_de_arquivos([p])
    monkeypatch.setattr(alvo, "POLITICA_CANONICA", "canonico@2")
    assert alvo._hash_de_arquivos([p]) != antes


# ---------------------------------------------------------------------------
# Caminho POSIX, e ordem pela MESMA string que entra no hash
# ---------------------------------------------------------------------------


def test_o_caminho_e_POSIX_nos_dois_sistemas(tmp_path, monkeypatch):
    """`str(Path)` dá `\\` no Windows e `/` no Linux."""
    monkeypatch.setattr(alvo, "_RAIZ", tmp_path)
    p = _escrever(tmp_path / "a" / "b", "c.py", "X = 1\n", b"\n")
    assert alvo.caminho_canonico(p) == "a/b/c.py"
    assert "\\" not in alvo.caminho_canonico(p)


def test_a_ORDEM_usa_a_mesma_string_que_entra_no_hash():
    """A `key` de ordenação é o caminho POSIX, e não `str(Path)`.

    `\\` é 0x5C e `/` é 0x2F, os dois em relação a `.` (0x2E): um conjunto com
    `app/x.py` **e** `app/x/algo.py` sairia em ordens diferentes nos dois
    sistemas.

    **Medido no fechamento de hoje: as duas ordens coincidem** — mas por acaso
    do conjunto, porque um pacote não coexiste com um módulo de mesmo nome.
    Ordenar pela string que entra no hash tira a sorte da conta.
    """
    import inspect

    fonte = inspect.getsource(alvo._hash_de_arquivos)
    assert "key=lambda par: par[0]" in fonte
    assert "key=lambda p: str(p)" not in fonte, (
        "a ordenacao voltou a usar `str(Path)`, que difere entre sistemas"
    )


def test_a_implementacao_da_NORMALIZACAO_esta_dentro_do_fechamento():
    """Mudar a política muda o alvo — como tem de ser.

    Se `alvo.py` estivesse fora do próprio fechamento, alguém poderia trocar a
    régua de canonicalização sem que o alvo se movesse, e os certificados
    continuariam de pé descrevendo uma medição que não existe mais.
    """
    _, modulos = alvo.hash_da_implementacao()
    assert "app.certificacao.alvo" in modulos


# ---------------------------------------------------------------------------
# E o hash NÃO depende do Git
# ---------------------------------------------------------------------------


def test_o_hash_NAO_depende_do_git(tmp_path, monkeypatch):
    """Produção não contém `.git`, e a correção não pode depender dele.

    O `.gitattributes` com `eol=lf` ajuda o checkout a nascer normalizado, mas
    ele é conveniência: quem garante o hash é a canonicalização, que roda sobre
    os bytes que estão no disco.
    """
    import ast
    import inspect

    for fn in (alvo.conteudo_canonico, alvo.caminho_canonico,
               alvo._hash_de_arquivos):
        arvore = ast.parse(inspect.getsource(fn))
        chamadas = {
            ast.unparse(n.func) for n in ast.walk(arvore)
            if isinstance(n, ast.Call)
        }
        assert not any("git" in c.lower() for c in chamadas), (
            f"{fn.__name__} chama git: a canonicalizacao tem de valer sem ele"
        )
        assert not any("subprocess" in c for c in chamadas)


# ---------------------------------------------------------------------------
# O que ficou para tras: o AMBIENTE tambem le arquivo de texto
# ---------------------------------------------------------------------------


def test_o_AMBIENTE_nao_depende_do_fim_de_linha_do_requirements(tmp_path, monkeypatch):
    """O campo por arquivo do ambiente entra no alvo inteiro.

    Ele lia bytes crus depois da OP-2 - achado ao conferir por que o componente
    de ambiente mudou na janela. Um requirements.txt com CRLF mudaria o alvo
    sem mudar dependencia nenhuma.
    """
    monkeypatch.setattr(alvo, "_RAIZ", tmp_path)
    fim = chr(10)
    texto = "fastapi==1.0" + fim + "pydantic==2.0" + fim
    (tmp_path / "requirements.txt").write_bytes(texto.encode())
    lf = alvo.hash_do_ambiente()
    (tmp_path / "requirements.txt").write_bytes(texto.replace(fim, chr(13) + chr(10)).encode())
    crlf = alvo.hash_do_ambiente()
    assert bytes([13, 10]) in (tmp_path / "requirements.txt").read_bytes()
    assert lf == crlf


def test_TODO_arquivo_que_o_alvo_le_passa_pela_canonicalizacao():
    """A guarda estrutural: read_bytes so dentro de conteudo_canonico.

    A OP-2 corrigiu _hash_de_arquivos e deixou um read_bytes cru em
    hash_do_ambiente. A guarda por funcao nao pegaria o proximo; esta varre o
    modulo inteiro pela forma.
    """
    import ast
    import inspect

    arvore = ast.parse(inspect.getsource(alvo))
    fora = []
    for funcao in ast.walk(arvore):
        if not isinstance(funcao, ast.FunctionDef):
            continue
        for no in ast.walk(funcao):
            if (
                isinstance(no, ast.Call)
                and isinstance(no.func, ast.Attribute)
                and no.func.attr == "read_bytes"
                and funcao.name != "conteudo_canonico"
            ):
                fora.append(funcao.name)
    assert not fora, (
        f"read_bytes cru fora de conteudo_canonico em {sorted(set(fora))}: o"
        " alvo voltaria a depender do checkout"
    )

