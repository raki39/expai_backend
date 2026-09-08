"""O relatorio do monitoramento continuo. ADR 0035.

Quatro coisas que o painel tem de dizer, e nenhuma pode ser prosa:

    o estado do monitor, DERIVADO
    os limiares congelados, com a procedencia inteira
    a insensibilidade do teste neste horizonte, com o numero
    o limite: CUSUM detecta NIVEL, e nao variancia

O terceiro e o quarto sao os que este projeto ja errou por escrever em texto.
*"O poder nao aparecia na tela"* foi a decima sexta ocorrencia do padrao, e
§14.4 tinha mandado registrar FDR e poder **juntos** exatamente para que
"nao rejeitou nada" nunca fosse lido como "esta calibrado".

Aqui e a mesma familia com outro nome: **nao alarmar e o comportamento de um
teste insensivel e tambem o de uma estrategia que funciona**. O relatorio poe
os dois lado a lado, sempre.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..monitoramento import cusum, limiar as limiar_mod, monitor
from ..monitoramento import veredito as veredito_mod

# A reserva da 0C, em barras de 15 minutos: 84 dias x 96. Usada SO para a
# tabela de diagnostico - o limiar de verdade e calibrado sobre este mesmo
# horizonte, mas por bootstrap, e nao por esta aritmetica.
RESERVA_BARRAS = 8_064

# O limite MEDIDO do bootstrap, e ele e diferente do limite do metodo.
#
# H0 da calibracao e "o forward se comporta como o in-sample CONGELADO", e nao
# "o forward vem da mesma lei desconhecida". O bootstrap condiciona na amostra
# observada, e a cauda de uma amostra finita e truncada - entao contra
# realizacoes novas da mesma lei o limiar cruza um pouco mais que o orcamento
# nominal.
#
# Medido em serie sintetica, com alvo 2.379, folga 1.189 e horizonte 1.000:
#
#     2.000 barras calibrando  ->  61,3% cruzam
#     8.000                    ->  13,3%
#     21.024 (o in-sample real)->  13,3%
#     60.000                   ->  10,8%
#
# O caso ruim e a amostra curta, e ele nao e o nosso. O numero fica AQUI e num
# teste, e nao numa frase: e a diferenca entre um limite conhecido e um limite
# que alguem descobre depois.
CRUZAMENTO_MEDIDO_SOB_H0_VERDADEIRA = 0.20

# O limite declarado do metodo. Campo, e nao frase: uma frase sobrevive a
# qualquer regressao sem mudar uma letra, e este e justamente o texto que
# alguem citaria para dizer que o monitor cobre mais do que cobre.
LIMITE_DECLARADO = (
    "CUSUM detecta desvio de NIVEL, e nao mudanca de variancia. Uma candidata"
    " que mantenha a media e exploda a volatilidade NAO dispara. Registrado e"
    " nao corrigido (ADR 0035): a alternativa e uma segunda estatistica com um"
    " segundo orcamento de falso alarme, na fase que existe para esperar"
)


def montar(
    conn: sqlite3.Connection, *, duracao_barra_ms: int
) -> dict[str, Any]:
    """O estado do monitoramento, tudo derivado de consulta."""
    em_uso = monitor.conhecimento_em_uso(conn)
    v = monitor.monitorar(conn, duracao_barra_ms=duracao_barra_ms)

    assuntos = [
        _do_assunto(conn, str(r["assunto"]), duracao_barra_ms=duracao_barra_ms)
        for r in conn.execute(
            "SELECT assunto FROM monitor_limiar ORDER BY assunto"
        )
    ]

    return {
        # ------------------------------------------------------ a resposta
        "estado": v.estado,
        "motivo": v.motivo,
        "mediu": v.mediu,
        # As duas que NUNCA sao verdadeiras, e por isso aparecem sempre.
        "ausencia_de_alarme_comprova_edge": v.comprova_edge,
        "ausencia_de_alarme_promove_candidata": v.promove_candidata,

        # ----------------------------------------------- quem esta em uso
        # §8.8 monitora "conhecimento em uso". A lista vazia e a resposta da
        # 0C, e ela e uma CONSULTA - nao um `if` sobre a fase.
        "conhecimento_em_uso": em_uso,
        "estados_que_contam_como_em_uso": list(monitor.ESTADOS_EM_USO),
        "por_que_vazio": (
            "o Portao B rejeitou a unica candidata que existia e a D38 (ADR"
            " 0034) decidiu que nenhuma entra no forward da 0C. O B3 nao ocupa"
            " o lugar dela: R79 manda o esperado vir do pre-registro imutavel,"
            " e o B3 nao tem pre-registro por decisao - inventar um alvo para"
            " ele seria escolher a regua depois de a fase existir"
            if not em_uso else None
        ),

        # ------------------------------------------------- o que foi medido
        "assuntos": assuntos,

        # ------------------------------------- a insensibilidade, com numero
        "diagnostico": {
            "o_que_e": (
                "ARL_0 e DIAGNOSTICO, e nao o criterio (ADR 0035, ajuste 1)."
                " O limiar e calibrado direto para a reserva finita, pela"
                " distribuicao do maximo do CUSUM ao longo de todo o"
                " horizonte - ARL medio nao garante sozinho a probabilidade de"
                " alarme dentro de 8.064 passos"
            ),
            "reserva_barras": RESERVA_BARRAS,
            "arl0_pela_aproximacao_de_poisson": limiar_mod.tabela_arl0(
                RESERVA_BARRAS
            ),
        },

        # ---------------------------------------------- o limite do metodo
        "limite_declarado": LIMITE_DECLARADO,
        "limite_do_bootstrap": {
            "h0": (
                "o forward se comporta como o in-sample CONGELADO - e nao"
                " 'vem da mesma lei desconhecida'. O bootstrap condiciona na"
                " amostra observada"
            ),
            "cruzamento_sob_h0_verdadeira_medido": (
                CRUZAMENTO_MEDIDO_SOB_H0_VERDADEIRA
            ),
            "por_que": (
                "a cauda de uma amostra finita e truncada, entao o limiar"
                " calibrado sobre 21.024 barras cruza ~13% contra realizacoes"
                " novas da mesma lei, e nao os 10% nominais. Com 2.000 barras"
                " calibrando seriam 61%: o tamanho da serie congelada e o que"
                " torna o numero utilizavel"
            ),
        },

        # ---------------------------------------------------- a estatistica
        "estatistica": {
            "tipo": "CUSUM unilateral inferior",
            "recorrencia": "S_t = max(0, S_{t-1} + alvo - observado_t - k)",
            "unidade": "milicents por barra",
            "alvo_vem_de": "efeito_minimo / horizonte_barras, os dois"
                           " pre-registrados e imutaveis (secao 8.2)",
            "n_minimo_entra_na_taxa": False,
            "por_que_n_minimo_fica_fora": (
                "n_minimo esta em observacoes EFETIVAS (secao 8.3), e o CUSUM"
                " atualiza por BARRA. Sobre a hipotese 41 a diferenca e de"
                " +9,27% no alvo, e o modo de falha nao e falso alarme: a"
                " calibracao usa o mesmo alvo, entao ela absorve o erro e o"
                " converte em SURDEZ"
            ),
            "barra_ausente": "S_t = S_{t-1}; nunca observacao de valor zero",
            "reset": (
                "nao existe. O piso em zero e parte da definicao da"
                " estatistica: o piso esta dentro do estimador, um reset"
                " estaria fora dele"
            ),
        },
    }


def _do_assunto(
    conn: sqlite3.Connection, assunto: str, *, duracao_barra_ms: int
) -> dict[str, Any]:
    lim = monitor.exigir(conn, assunto)
    serie = monitor.serie(conn, assunto)
    v = veredito_mod.avaliar(
        serie, lim.como_limiares(), duracao_barra_ms=duracao_barra_ms
    )
    return {
        "assunto": assunto,
        "limiar": lim.como_dict(),
        "veredito": v.como_dict(),
        "alarmes": monitor.alarmes(conn, assunto),
        "retestes": monitor.retestes(conn, assunto),
        "cobertura_minima_ppm": veredito_mod.COBERTURA_MINIMA_PPM,
        "lacuna_maxima_barras": veredito_mod.lacuna_maxima_em_barras(
            duracao_barra_ms
        ),
        "niveis": list(cusum.NIVEIS),
    }
