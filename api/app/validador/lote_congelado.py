"""Qual `config_version` os portoes certificam — e ela nao e a vigente.

> *"Os portoes devem ser vinculados ao lote congelado que certificam, e nao
> consultar silenciosamente a config_version vigente. A rota deve derivar a
> versao do proprio lote/pre-registro, sem permitir que o chamador escolha
> arbitrariamente uma versao conveniente."* — o usuario, 2026-09-09

## O defeito que isto conserta, medido em producao

Em 2026-09-09 `/api/relatorio/portao-a` respondeu **`pendente` com SETE das
onze condicoes em `None`** — a1a, a1b e a2 —, e `/api/relatorio/portao-b`
recusou calcular por R49. Nao havia dado perdido: a evidencia inteira da 0B
esta sob a `config_version` **6**, e as rotas liam a **7**, criada pela
reancoragem de 2026-09-08 para destravar o incremento 17.

**Os portoes descreviam a nossa data de deploy como se fosse o experimento.**
E a mesma forma de `cobertura_total` contra `cobertura_observada`, onde chamar
de perda os 366 instantes anteriores ao coletor teria descrito o deploy no
lugar do mercado.

## Por que DERIVAR, e nao receber por parametro

Um parametro deixaria o chamador escolher a versao que da a resposta melhor —
que e exatamente a pergunta 5 do teste de escopo (*"isso permitiria ajustar um
criterio depois de ver o resultado?"*). A familia vive onde as hipoteses estao,
e `hypothesis` e **append-only por gatilho**: a derivacao devolve a mesma
resposta para sempre, e muda sozinha no dia em que um lote novo nascer.

## E a equivalencia NAO e herdada em silencio

Se a vigente diverge do lote, o relatorio publica **`vigente_nao_certificada`**
com o diff campo a campo. Herdar calado faria a certificacao de um mundo valer
para outro sem que nada avisasse.
"""

from __future__ import annotations

import sqlite3

#: O que separa "a mesma coisa" de "outra coisa". Nao e o `config_hash` cru:
#: ele muda quando o SCHEMA cresce, mesmo sem nenhum valor mudar, e foi
#: exatamente isso que a reancoragem fez. O que decide e o par
#: (identidade executavel, campos materiais divergentes).
EQUIVALENTES = "equivalentes"
DIVERGENTES = "vigente_nao_certificada"


def do_lote(conn: sqlite3.Connection) -> int | None:
    """A `config_version` onde a familia de hipoteses vive. Derivada.

    A mais recente que tem hipotese: um lote novo desloca a certificacao para
    ele, e a do lote anterior deixa de responder pela fase corrente. `None`
    quando nunca se registrou hipotese nenhuma — e ai os portoes nao tem o que
    certificar, o que e resposta e nao falha.
    """
    linha = conn.execute(
        "SELECT r.config_version_id AS cv"
        "  FROM hypothesis h JOIN run r ON r.id = h.run_id"
        " ORDER BY h.id DESC LIMIT 1"
    ).fetchone()
    return int(linha["cv"]) if linha and linha["cv"] is not None else None


def quantas_no_lote(conn: sqlite3.Connection, config_version_id: int) -> int:
    linha = conn.execute(
        "SELECT COUNT(*) AS n FROM hypothesis h JOIN run r ON r.id = h.run_id"
        " WHERE r.config_version_id = ?",
        (config_version_id,),
    ).fetchone()
    return int(linha["n"]) if linha else 0


def _campos_materiais_divergentes(
    conn: sqlite3.Connection, *, de: int, ate: int
) -> list[dict]:
    """Os campos MATERIAIS que mudaram entre duas versoes, pelo registro.

    Do log de `config_change`, e nao de um diff que recalculassemos: o registro
    e o que a §10.2.3 exige que exista, e recalcular abriria a porta para as
    duas leituras discordarem.
    """
    return [
        {
            "campo": l["field"],
            "no_lote": l["old_value"],
            "na_vigente": l["new_value"],
        }
        for l in conn.execute(
            "SELECT c.field AS field, c.old_value_json AS old_value,"
            "       c.new_value_json AS new_value"
            "  FROM config_change c"
            " WHERE c.version_id > ? AND c.version_id <= ?"
            "   AND c.material = 1"
            " ORDER BY c.version_id, c.field",
            (de, ate),
        )
    ]


def equivalencia(
    conn: sqlite3.Connection, *, lote_id: int, vigente_id: int
) -> dict:
    """O lote certificado continua descrevendo a config vigente?

    **Sem heranca silenciosa.** Quando divergem, o campo `estado` diz
    `vigente_nao_certificada` e o diff vai junto, para que quem le julgue com
    o mesmo dado que a funcao usou.
    """
    from ..calibracao import identidade
    from ..config import service as config_service

    if lote_id == vigente_id:
        return {
            "estado": EQUIVALENTES,
            "lote_config_version_id": lote_id,
            "vigente_config_version_id": vigente_id,
            "por_que": "o lote esta sob a propria config vigente",
            "campos_materiais_divergentes": [],
        }

    do_lote_v = config_service.versao_por_id(conn, lote_id)
    vigente_v = config_service.versao_por_id(conn, vigente_id)
    perfil_lote = identidade.perfil_da_config_version(conn, lote_id)
    perfil_vig = identidade.perfil_da_config_version(conn, vigente_id)
    ident_lote = identidade.identidade_executavel(
        do_lote_v.config_hash if do_lote_v else "?", perfil_lote
    )
    ident_vig = identidade.identidade_executavel(
        vigente_v.config_hash if vigente_v else "?", perfil_vig
    )
    divergentes = _campos_materiais_divergentes(
        conn, de=lote_id, ate=vigente_id
    )
    igual = ident_lote == ident_vig and not divergentes
    return {
        "estado": EQUIVALENTES if igual else DIVERGENTES,
        "lote_config_version_id": lote_id,
        "vigente_config_version_id": vigente_id,
        "identidade_executavel_do_lote": ident_lote,
        "identidade_executavel_da_vigente": ident_vig,
        "identidades_iguais": ident_lote == ident_vig,
        "campos_materiais_divergentes": divergentes,
        "por_que": (
            "a config vigente executa o mesmo que a do lote: mesma identidade"
            " executavel e nenhum campo material divergente"
            if igual
            else (
                "a config vigente NAO esta certificada. O Portao A e o Portao B"
                " abaixo certificam o lote da"
                f" config_version {lote_id}, e a vigente ({vigente_id}) difere"
                f" em {len(divergentes)} campo(s) material(is). Estender a"
                " certificacao a ela seria heranca silenciosa - e o que vale"
                " para um mundo passaria a valer para outro sem que nada"
                " avisasse. Recertificar exige rodar o lote sob a vigente"
            )
        ),
        "o_que_isso_NAO_significa": (
            "nao significa que a evidencia do lote se perdeu, nem que ela"
            " esteja errada: ela segue integra sob a config em que nasceu, e e"
            " ela que estes portoes leem. O que ela nao faz e responder pela"
            " config vigente"
        ),
    }
