# -*- coding: utf-8 -*-
"""A fronteira entre DIMENSIONAR, AJUSTAR e CONFIRMAR, imposta por forma.

O ADR 0027 par.5 e o ADR 0041 dizem que do piloto saem `sigma_e`, `tau` e a
forma da cauda, e que os dados dele ficam fora da calibracao E da revalidacao.
A separacao das SERIES ja era estrutural - `serie_e2_do_piloto`,
`_da_calibracao` e `_da_revalidacao`, com a finalidade literal no SQL.

**O que nao era estrutural era o NOME DO QUE SAI.** Ate 2026-10-16 o piloto
publicava `p10_mili_bps`, `limite_inferior_95_mili_bps` e `pessimista` - as
duas primeiras identicas, byte a byte, as que `ajuste` usa para o p10 que
AJUSTA e o LB95 que CONFIRMA; a terceira sendo o proprio criterio de
revalidacao avaliado sobre a janela errada.

Separar series e depois publicar os resultados sob o mesmo nome desfaz a
separacao no unico lugar onde um humano a le.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.calibracao import ajuste, dimensionamento

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

#: Os nomes que significam AJUSTE ou CONFIRMACAO. Quem publica um destes esta
#: respondendo a pergunta da calibracao ou a da revalidacao - nao a do piloto.
NOMES_RESERVADOS = (
    "p10_mili_bps",
    "limite_inferior_95_mili_bps",
    "lb95_mili_bps",
    "lb95",
    "p10",
    "pessimista",
)


#: O UNICO lugar onde `p10` e um nome legitimo no contrato do piloto: ali ele
#: e ROTULO DE QUANTIL, e a forma da cauda e saida declarada do piloto (ADR
#: 0027 par.5). Isentar por CAMINHO, e nao tirar `p10` da lista de reservados -
#: tirar da lista deixaria passar um `p10` solto em qualquer outro lugar.
CAMINHO_DOS_QUANTIS = ".forma_da_cauda.quantis_mili_bps"


def _chaves(o, caminho="") -> list[str]:
    """Todas as chaves do json, em qualquer profundidade, com o caminho."""
    saida = []
    if isinstance(o, dict):
        for k, v in o.items():
            saida.append(k)
            saida += _chaves(v, f"{caminho}.{k}")
    elif isinstance(o, list):
        for x in o:
            saida += _chaves(x, caminho)
    return saida


def _chaves_com_caminho(o, caminho="") -> list[tuple[str, str]]:
    saida = []
    if isinstance(o, dict):
        for k, v in o.items():
            saida.append((caminho, k))
            saida += _chaves_com_caminho(v, f"{caminho}.{k}")
    elif isinstance(o, list):
        for x in o:
            saida += _chaves_com_caminho(x, caminho)
    return saida


def _dimensionamento_de_exemplo() -> dimensionamento.Dimensionamento:
    """Um `Dimensionamento` montado a mao: o contrato e que esta sob teste.

    Nao e preciso piloto nem banco - o que se afirma aqui e o formato de
    `como_dict`, e monta-lo direto e o que mantem o teste focado nele.
    """
    return dimensionamento.Dimensionamento(
        de_ms=0, ate_ms_exclusive=900_000, janela_hash="h" * 64, lado="compra",
        n=100, n_efetivo=100.0, sigma_e_mili_bps=448.7, tau=1.0,
        tau_bruto=0.77, ar1_plausivel=True, bloco=10,
        forma_da_cauda={"quantis_mili_bps": {"p10": 3499.0}},
        p10_diagnostico_mili_bps=3499.0, lb95_diagnostico_mili_bps=3499.0,
        delta_mili_bps=500, n_exigido=10, n_bruto_exigido=10,
    )


# ===========================================================================
# 1. O CONTRATO DE SAIDA DO PILOTO
# ===========================================================================


def test_o_piloto_NAO_publica_nenhum_nome_reservado_ao_ajuste_ou_a_confirmacao():
    """O nome tem de viajar com o valor.

    Aninhar sob `diagnostico` nao basta: quem achata o json, procura a chave
    ou copia o campo le a grandeza errada, e nada acusa.
    """
    d = _dimensionamento_de_exemplo().como_dict()
    reservadas_presentes = [
        f"{caminho}.{k}"
        for caminho, k in _chaves_com_caminho(d)
        if k in NOMES_RESERVADOS and caminho != CAMINHO_DOS_QUANTIS
    ]
    assert reservadas_presentes == [], (
        f"o contrato do piloto publica {reservadas_presentes}, que sao os"
        f" nomes do p10 que AJUSTA e do LB95 que CONFIRMA. Quem ler isto le"
        f" uma resposta de calibracao sobre a serie do piloto"
    )
    # A isencao do caminho dos quantis NAO pode ser vacua: a forma da cauda
    # tem de estar la, com o rotulo `p10`, porque ela E saida do piloto.
    chaves = _chaves(d)
    assert any(
        caminho == CAMINHO_DOS_QUANTIS and k == "p10"
        for caminho, k in _chaves_com_caminho(d)
    ), ("a forma da cauda sumiu, ou o caminho dos quantis mudou. A isencao"
        " acima passou a nao isentar nada")
    # E a asserção de nao-vacuidade: o diagnostico CONTINUA sendo publicado,
    # so que com o sufixo. Retira-lo seria esconder o numero, que e o oposto
    # do que esta correcao quer.
    assert "p10_DIAGNOSTICO_mili_bps" in chaves
    assert "lb95_DIAGNOSTICO_mili_bps" in chaves


def test_pessimista_NAO_volta__ele_e_o_criterio_da_REVALIDACAO():
    """`lb95 >= 0` e `REGRAS_DA_REVALIDACAO['alvo']`, e nao um diagnostico.

    Um booleano nao se le como medicao: le-se como veredito. Publicá-lo a
    partir da serie do piloto responde a pergunta da revalidacao com o dado
    que o ADR 0027 par.5 mantem fora dela.
    """
    assert ajuste.REGRAS_DA_REVALIDACAO["alvo"] == (
        "lb95_p10_e2_maior_igual_zero"
    ), "se o alvo mudou de nome, esta guarda precisa acompanhar"

    d = _dimensionamento_de_exemplo().como_dict()
    assert "pessimista" not in _chaves(d)

    # E nenhum booleano do contrato pode ser `lb95 >= 0` disfarcado: o unico
    # booleano legitimo aqui e `ar1_plausivel`, que e sobre DEPENDENCIA.
    booleanos = _booleanos(d)
    assert set(booleanos) <= {"ar1_plausivel"}, (
        f"booleanos inesperados no contrato do piloto: {booleanos}."
        f" Um booleano novo aqui e um veredito novo, e o piloto nao emite"
        f" veredito"
    )


def _booleanos(o) -> list[str]:
    saida = []
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, bool):
                saida.append(k)
            else:
                saida += _booleanos(v)
    elif isinstance(o, list):
        for x in o:
            saida += _booleanos(x)
    return saida


# ===========================================================================
# 2. CADA FINALIDADE LE A PROPRIA SERIE — por AST, e nao por leitura
# ===========================================================================

LEITORES = {
    "serie_e2_do_piloto": "piloto",
    "serie_e2_da_calibracao": "calibracao",
    "serie_e2_da_revalidacao": "revalidacao",
}


def _chamadas(arquivo: pathlib.Path) -> set[str]:
    arvore = ast.parse(arquivo.read_text(encoding="utf-8"))
    nomes = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Call):
            f = no.func
            alvo = f.attr if isinstance(f, ast.Attribute) else (
                f.id if isinstance(f, ast.Name) else None
            )
            if alvo in LEITORES:
                nomes.add(alvo)
    return nomes


def test_dimensionamento_le_SO_a_serie_do_piloto():
    chamadas = _chamadas(APP / "calibracao" / "dimensionamento.py")
    assert chamadas == {"serie_e2_do_piloto"}, (
        f"`dimensionamento` chama {chamadas}. Ele dimensiona, e dimensionar le"
        f" o piloto - ler calibracao ou revalidacao aqui seria o caminho de"
        f" volta que o ADR 0041 fechou"
    )


def test_ajuste_NUNCA_le_a_serie_do_piloto():
    """A garantia central do ADR 0041, e a que ja foi violada uma vez.

    `ajuste.estimar` rodava na janela do piloto e `ajuste.derivar`
    transformava o `p10` dela em override de `spread_bps`.
    """
    chamadas = _chamadas(APP / "calibracao" / "ajuste.py")
    assert "serie_e2_do_piloto" not in chamadas, (
        "`ajuste` le a serie do PILOTO. E exatamente a CORRECAO BLOQUEANTE 2:"
        " o p10 do piloto virando override de parametro"
    )
    assert chamadas == {"serie_e2_da_calibracao", "serie_e2_da_revalidacao"}


def test_a_ROTA_nao_recalcula_a_estimativa_do_piloto_por_fora():
    """Duas implementacoes da mesma coisa divergem, e estas divergiram.

    A rota montava o bloco do piloto a mao, com `limite_inferior_do_quantil`
    direto, e publicava nomes que `dimensionamento` ja tinha resolvido.
    """
    rota = APP / "api" / "rotas" / "calibracao.py"
    chamadas = _chamadas(rota)
    assert "serie_e2_do_piloto" not in chamadas, (
        "a rota voltou a montar a serie do piloto por fora. Quem dimensiona e"
        " `dimensionamento.dimensionar`, e o contrato de saida e o dele"
    )

    # E nao basta nao ler a serie: ela tambem nao pode recalcular o quantil.
    texto = rota.read_text(encoding="utf-8")
    arvore = ast.parse(texto)
    quantis = [
        no for no in ast.walk(arvore)
        if isinstance(no, ast.Call)
        and isinstance(no.func, ast.Attribute)
        and no.func.attr == "limite_inferior_do_quantil"
    ]
    assert quantis == [], (
        "a rota chama `limite_inferior_do_quantil` de novo. O p10 e o LB95"
        " saem de UM lugar, e esse lugar carrega os nomes certos"
    )


@pytest.mark.parametrize("modulo,proibido", [
    ("dimensionamento.py", "perfil"),
])
def test_dimensionar_nao_importa_o_modulo_que_grava_perfil(modulo, proibido):
    """Ja afirmado no docstring do modulo; aqui vira guarda."""
    arvore = ast.parse((APP / "calibracao" / modulo).read_text(encoding="utf-8"))
    importados = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.ImportFrom):
            for a in no.names:
                importados.add(a.name)
        elif isinstance(no, ast.Import):
            for a in no.names:
                importados.add(a.name.split(".")[-1])
    assert proibido not in importados, (
        f"`{modulo}` importa `{proibido}`: dimensionar nao grava parametro"
    )
