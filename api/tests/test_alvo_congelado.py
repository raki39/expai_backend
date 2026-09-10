"""Os módulos do alvo estão CONGELADOS, e mudar qualquer um invalida.

> *"Congele os pacotes que participam do `alvo_de_certificacao_hash`; qualquer
> mudança posterior deve invalidar a certificação normalmente."* — o usuário,
> 2026-09-10

O congelamento é `tests/alvo_congelado.json`, com o hash de **cada** módulo do
fechamento transitivo. Ele não impede a mudança — impede que ela passe
**despercebida**: quem mexer num deles vê a suíte quebrar nomeando o módulo, e
decide se recertifica.

## Onde este arquivo mora, e por que isso importa

Em `tests/`, e não em `app/`. Se o congelamento vivesse dentro do pacote, ele
entraria no próprio fechamento — e cada atualização dele mudaria o alvo que ele
existe para vigiar, num laço sem fundo.

## A dívida do fim de linha foi PAGA — OP-2, 2026-09-10

Antes, o mesmo commit produzia alvos diferentes conforme o sistema de arquivos:
`56d4a443…` no Windows (39 dos 69 com CRLF) e `8bfbef7e…` no Linux. Este teste
normalizava por conta própria, com uma **segunda** implementação da
normalização, e era a única guarda que valia nas duas máquinas.

Hoje quem normaliza é `alvo.conteudo_canonico`, dentro do próprio fechamento, e
este teste usa **a mesma função**. Duas normalizações em dois lugares
divergiriam na primeira regra nova — o BOM, por exemplo, que a de cá não
tratava.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import pathlib

CONGELADO = pathlib.Path(__file__).parent / "alvo_congelado.json"


def _atual() -> dict[str, str]:
    """O hash canônico de cada módulo do fechamento — pela função do alvo."""
    from app.certificacao import alvo

    _, modulos = alvo.hash_da_implementacao()
    fora = {}
    for m in modulos:
        arquivo = alvo._arquivo_do_modulo(m)
        if arquivo is not None:
            fora[m] = hashlib.sha256(
                alvo.conteudo_canonico(pathlib.Path(arquivo))
            ).hexdigest()[:16]
    return fora


def _gravado() -> dict:
    return json.loads(CONGELADO.read_text(encoding="utf-8"))


def test_o_congelamento_existe_e_cobre_o_fechamento_INTEIRO():
    """Nenhum módulo do alvo fica fora do congelamento.

    Um módulo novo no caminho entra no fechamento sozinho — e teria de entrar
    aqui também, senão o congelamento passaria a cobrir menos do que o alvo,
    silenciosamente. É a mesma forma do `ESTRUTURA_ESPERADA` que ficou parado na
    migração 27 enquanto o schema chegava à 28.
    """
    gravado = _gravado()["modulos"]
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


def test_NENHUM_modulo_congelado_mudou():
    """A guarda que o usuário pediu, e ela nomeia quem mudou.

    Não é proibição: é aviso obrigatório. Mudar um destes módulos é legítimo —
    o que não pode é a certificação continuar de pé sem que ninguém tenha
    decidido recertificar.
    """
    gravado = _gravado()
    atual = _atual()

    mudaram = sorted(
        m for m in gravado["modulos"]
        if m in atual and gravado["modulos"][m] != atual[m]
    )
    assert not mudaram, (
        "modulos do alvo de certificacao MUDARAM:\n  "
        + "\n  ".join(
            f"{m}: congelado {gravado['modulos'][m]} -> agora {atual[m]}"
            for m in mudaram
        )
        + "\n\nOs cinco escopos certificados deixaram de descrever este"
        " laboratorio. Recertifique os CINCO e atualize"
        " `tests/alvo_congelado.json` - ou reverta a mudanca."
    )


def test_o_hash_da_implementacao_e_o_CONGELADO():
    """O número que o alvo publica é o mesmo que o congelamento fixou.

    Depois da OP-2 ele não depende mais do sistema de arquivos: o valor daqui é
    o que produção reporta, e é por isso que o congelamento feito numa máquina
    vale na outra.
    """
    from app.certificacao import alvo

    h, _ = alvo.hash_da_implementacao()
    assert h[:16] == _gravado()["implementacao_hash"], (
        "o hash canonico da implementacao divergiu do congelado"
    )


def test_a_DIVIDA_do_fim_de_linha_foi_PAGA():
    """A asserção contrária da guarda antiga — trocada, e não apagada.

    A guarda antiga afirmava que `_hash_de_arquivos` lia `read_bytes()` cru e
    pedia para ser invertida no dia em que ele passasse a normalizar. Esse dia
    foi 2026-09-10.
    """
    from app.certificacao import alvo

    fonte = inspect.getsource(alvo._hash_de_arquivos)
    assert "conteudo_canonico(" in fonte, (
        "`_hash_de_arquivos` voltou a ler bytes crus: o alvo passaria a"
        " depender do checkout outra vez"
    )
    assert "POLITICA_CANONICA" in fonte

    divida = (
        pathlib.Path(__file__).parents[3] / ".docs" / "16-divida-tecnica.md"
    )
    if divida.is_file():
        texto = divida.read_text(encoding="utf-8")
        assert "OP-2" in texto and "PAGA" in texto, (
            "a OP-2 continua registrada como aberta em `16-divida-tecnica.md`"
            " depois de paga"
        )
