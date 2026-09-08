"""O relatorio da quarentena. Garantia 5 do ADR 0034.

Tres coisas, explicitas, e nenhuma delas e derivavel de silencio:

    nenhuma candidata admitida
    o motivo de cada exclusao
    que o B3 foi apenas controle negativo

**O modo de falha aqui e de RELATO, e nao de metodo.** Uma ausencia que ninguem
declara vira silencio, e silencio e lido como esquecimento - alguem abrindo o
painel em 2027 tem de conseguir ver que nao houve candidata *de proposito*, e
nao porque a etapa foi esquecida.

E o campo `sem_candidata` e derivado de CONSULTA, como todas as onze condicoes
do Portao A: se fosse uma frase, sobreviveria intacta ao dia em que uma
candidata entrasse.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..quarentena import abordagem as abordagem_mod
from ..quarentena import admissao, congelado
from ..quarentena.veredito import REQUISITOS, sem_candidata


def montar(
    conn: sqlite3.Connection, *, config_version_id: int
) -> dict[str, Any]:
    """O estado da quarentena, tudo derivado de consulta."""
    congelados = [
        dict(r)
        for r in conn.execute(
            "SELECT hypothesis_id, run_id, run_digest, content_hash,"
            "       abordagem_assinatura, abordagem_versao,"
            "       identidade_executavel, dataset_sha256, snapshot_sha256,"
            "       timeframe, metrica_primaria, metrica_valor_cents,"
            "       congelado_em FROM quarentena_congelado"
            " ORDER BY hypothesis_id"
        )
    ]

    # A CONTAGEM E A DECLARACAO. Zero admitidas nao e a ausencia de um numero:
    # e o numero zero, derivado de consulta.
    admitidas = conn.execute(
        "SELECT COUNT(*) AS n FROM quarentena_congelado"
    ).fetchone()["n"]

    veredito = sem_candidata() if admitidas == 0 else None

    limiares = admissao.ler_limiares(conn, config_version_id)

    return {
        # ------------------------------------------------ a declaracao
        "nenhuma_candidata_admitida": admitidas == 0,
        "candidatas_admitidas": int(admitidas),
        "decisao": "D38, ADR 0034 - alternativa A",
        "por_que": (
            "§14.2 previa a candidata retrospectiva como insumo da 0C, e o "
            "Portao B rejeitou a unica que existia. §14.4 e literal: a "
            "abordagem esta descartada, reprojetar - e reprojetar passa pela "
            "0B, nunca direto por aqui"
        ),

        # ------------------------------------------- o motivo de cada exclusao
        "abordagens_rejeitadas": [
            {
                "assinatura": a.assinatura,
                "familia": a.familia,
                "motivo": a.motivo,
                "bloqueia_variacao_parametrica": True,
                "bloqueia_variacao_textual": True,
                "bloqueia_troca_de_timeframe": True,
                "volta_legitima": (
                    "reprojeto pela 0B, com assinatura NOVA e pre-registro "
                    "proprio, mantendo a linhagem e a contabilizacao das "
                    "tentativas"
                ),
            }
            for a in abordagem_mod.rejeitadas(conn)
        ],

        # ------------------------------------------------- o B3, e o que ele e
        "b3": {
            "papel": "CONTROLE NEGATIVO do proprio encanamento",
            "tem_pre_registro": False,
            "consome_credito": False,
            "entra_em_familia": False,
            "conta_no_dsr": False,
            "pode_ser_promovido": False,
            "por_que": (
                "uma regra congelada que ninguem afirma ser edge, e que a 0A "
                "ja mediu perdendo (US$ 151,34 contra US$ 1.000 de semente). "
                "Se a maquinaria o promovesse, a maquinaria estaria errada - e "
                "e para isso que ele esta ali"
            ),
            "fronteira": (
                "estrutural, e nao uma checagem: `admissao.admitir` recusa "
                "baseline pelo `agent_id`, e nao ha caminho que lhe de "
                "pre-registro"
            ),
        },

        # -------------------------------------------------- os limiares
        "limiares_congelados": (
            None if limiares is None else {
                "periodo_minimo_barras": limiares.periodo_minimo_barras,
                "regimes_minimos": limiares.regimes_minimos,
                "magnitude_minima_ppm": limiares.magnitude_minima_ppm,
                "reserva_maxima_barras": limiares.reserva_maxima_barras,
                "podem_ser_reduzidos": False,
            }
        ),
        "limiares_estao_congelados": limiares is not None,

        # ---------------------------------------------- o in-sample congelado
        "in_sample_congelado": congelados,
        "conferencia_do_congelado": [
            _conferir(conn, int(c["hypothesis_id"])) for c in congelados
        ],

        # --------------------------------------------------------- o veredito
        "requisitos_de_saida": list(REQUISITOS),
        "veredito": (
            None if veredito is None else {
                "resultado": veredito.resultado,
                "motivo": veredito.motivo,
                "requisitos": veredito.requisitos,
            }
        ),
    }


def _conferir(conn: sqlite3.Connection, hypothesis_id: int) -> dict[str, Any]:
    """A conferencia do congelado, com o erro NO RELATORIO e nao no log.

    Uma divergencia que so aparece no log da plataforma e uma divergencia que
    ninguem ve - foi o defeito do run 27, e o incremento 11b existiu para
    corrigi-lo.
    """
    try:
        congelado.conferir(conn, hypothesis_id)
        return {"hypothesis_id": hypothesis_id, "confere": True, "erro": None}
    except congelado.DivergenciaDoCongelado as e:
        return {
            "hypothesis_id": hypothesis_id, "confere": False, "erro": str(e),
        }
