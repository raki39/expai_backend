"""Os 69 pacotes do alvo estão CONGELADOS, e mudar qualquer um invalida.

> *"Congele os pacotes que participam do `alvo_de_certificacao_hash`; qualquer
> mudança posterior deve invalidar a certificação normalmente."* — o usuário,
> 2026-09-10

O congelamento é `tests/alvo_congelado.json`, com o sha256 de **cada** módulo
do fechamento transitivo. Ele não impede a mudança — impede que ela passe
**despercebida**: quem mexer num dos 69 vê a suíte quebrar nomeando o módulo, e
decide se recertifica.

## Onde este arquivo mora, e por que isso importa

Em `tests/`, e não em `app/`. Se o congelamento vivesse dentro do pacote, ele
entraria no próprio fechamento — e cada atualização dele mudaria o alvo que ele
existe para vigiar, num laço sem fundo.

## O hash é normalizado para LF, e isso corrige um defeito real

**Medido em 2026-09-10:** o mesmo commit produz alvos diferentes conforme o
sistema de arquivos. Local (Windows, 39 dos 69 com CRLF) o
`hash_da_implementacao` dá `56d4a443…`; em produção (Linux, LF) dá
`8bfbef7e…`. Normalizando CRLF→LF localmente o resultado bate **ao dígito** com
produção.

Ou seja: `alvo.hash_da_implementacao` mede o **checkout**, e não o conteúdo. Um
certificado emitido no Linux não confere no Windows sob o mesmo commit.

**Não foi corrigido agora**, e a razão é a mesma do A1a: `alvo.py` está dentro
do próprio fechamento, então tocá-lo invalidaria os cinco escopos recém
certificados. Está em `.docs/16-divida-tecnica.md` como bloqueante antes da
próxima recertificação.

Este teste normaliza, então ele vale nos dois sistemas — e é por isso que ele é
a guarda utilizável enquanto a dívida não é paga.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

CONGELADO = pathlib.Path(__file__).parent / "alvo_congelado.json"


def _hash_lf(caminho: pathlib.Path) -> str:
    """Sha256 do conteúdo com fim de linha normalizado.

    LF é o canônico porque é o que o container executa — e é o ambiente que
    produz o certificado que vale.
    """
    return hashlib.sha256(
        caminho.read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()[:16]


def _atual() -> dict[str, str]:
    from app.certificacao import alvo

    _, modulos = alvo.hash_da_implementacao()
    fora = {}
    for m in modulos:
        arquivo = alvo._arquivo_do_modulo(m)
        if arquivo is not None:
            fora[m] = _hash_lf(arquivo)
    return fora


def test_o_congelamento_existe_e_cobre_o_fechamento_INTEIRO():
    """Nenhum módulo do alvo fica fora do congelamento.

    Um módulo novo no caminho entra no fechamento sozinho — e teria de entrar
    aqui também, senão o congelamento passaria a cobrir menos do que o alvo,
    silenciosamente. É a mesma forma do `ESTRUTURA_ESPERADA` que ficou parado na
    migração 27 enquanto o schema chegava à 28.
    """
    gravado = json.loads(CONGELADO.read_text(encoding="utf-8"))["modulos"]
    atual = _atual()

    faltando = sorted(set(atual) - set(gravado))
    sobrando = sorted(set(gravado) - set(atual))
    assert not faltando, (
        f"modulos NOVOS no alvo e ausentes do congelamento: {faltando}."
        " Ou eles entram em `tests/alvo_congelado.json`, ou saem do caminho de"
        " certificacao - e entrar significa RECERTIFICAR"
    )
    assert not sobrando, (
        f"modulos no congelamento que sairam do alvo: {sobrando}. Se a saida"
        " foi deliberada, atualize o congelamento e recertifique"
    )
    assert len(atual) == 69, (
        f"o fechamento tem {len(atual)} modulos e o congelamento fixou 69:"
        " a contagem mudou, entao o alvo mudou"
    )


def test_NENHUM_dos_69_mudou_desde_a_certificacao_da_cv9():
    """A guarda que o usuário pediu, e ela nomeia quem mudou.

    Não é proibição: é aviso obrigatório. Mudar um destes módulos é legítimo —
    o que não pode é a certificação continuar de pé sem que ninguém tenha
    decidido recertificar.
    """
    gravado = json.loads(CONGELADO.read_text(encoding="utf-8"))["modulos"]
    atual = _atual()

    mudaram = sorted(
        m for m in gravado if m in atual and gravado[m] != atual[m]
    )
    assert not mudaram, (
        "modulos do alvo de certificacao MUDARAM:\n  "
        + "\n  ".join(
            f"{m}: congelado {gravado[m]} -> agora {atual[m]}" for m in mudaram
        )
        + "\n\nOs cinco escopos da cv9 (alvo c36ff72e...) deixaram de descrever"
        " este laboratorio. Recertifique os CINCO e atualize"
        " `tests/alvo_congelado.json` - ou reverta a mudanca."
    )


def test_o_hash_agregado_bate_com_o_de_PRODUCAO():
    """Normalizado, o hash local reproduz o do container. Ao dígito.

    É a prova de que o congelamento acima é comparável entre as duas máquinas —
    sem ela, ele seria uma guarda que só vale em quem a escreveu.
    """
    from app.certificacao import alvo

    gravado = json.loads(CONGELADO.read_text(encoding="utf-8"))
    _, modulos = alvo.hash_da_implementacao()

    h = hashlib.sha256()
    arquivos = [
        a for a in (alvo._arquivo_do_modulo(m) for m in modulos) if a is not None
    ]
    for caminho in sorted(arquivos, key=lambda p: str(p)):
        p = pathlib.Path(caminho)
        h.update(
            str(p.relative_to(alvo._RAIZ)).replace("\\", "/").encode()
        )
        h.update(b"\x00")
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
        h.update(b"\x00")
    assert h.hexdigest()[:16] == gravado["implementacao_hash_lf"], (
        "o hash normalizado divergiu do que producao reportou na certificacao"
        " da cv9"
    )


def test_a_DIVIDA_do_fim_de_linha_esta_registrada():
    """A guarda normaliza; o código de produção **não**. Isso é dívida.

    `alvo.hash_da_implementacao` lê `read_bytes()` cru, então o alvo depende do
    checkout. Corrigir mexe em `alvo.py`, que está dentro do próprio
    fechamento — e invalidaria os cinco escopos. Fica registrado, e a
    correção é bloqueante antes da próxima recertificação.
    """
    import inspect

    from app.certificacao import alvo

    fonte = inspect.getsource(alvo._hash_de_arquivos)
    assert "read_bytes()" in fonte
    assert "replace(b" not in fonte, (
        "`_hash_de_arquivos` passou a normalizar o fim de linha: a divida foi"
        " paga, e este teste precisa virar a asserção contrária - junto com a"
        " recertificacao dos cinco escopos"
    )

    divida = (
        pathlib.Path(__file__).parents[3] / ".docs" / "16-divida-tecnica.md"
    )
    if divida.is_file():
        texto = divida.read_text(encoding="utf-8")
        assert "fim de linha" in texto or "CRLF" in texto, (
            "a divida do fim de linha saiu de `16-divida-tecnica.md` sem ter"
            " sido paga"
        )
