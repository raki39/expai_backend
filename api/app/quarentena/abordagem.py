"""A abordagem como objeto, com assinatura estrutural e linhagem. ADR 0034.

## O bloqueio que existia nao bastava

`content_hash` e o hash do conteudo do pre-registro, e ele pega a copia
**identica**. Nao pega a **maquiada**: trocar `50/200` por `50/210` produz hash
diferente e a **mesma abordagem**.

E foi exatamente esse o formato do controle de **duplicacao disfarcada** que o
Portao A barrou na 0B - o mecanismo de deteccao existia para os controles e nao
existia para o caso real.

## A assinatura e o MECANISMO, sem os numeros

    familia + forma da regra (quais campos estao em uso)

O que fica **fora**, de proposito:

| fora | por que |
|---|---|
| os **valores** dos parametros | 50/200 e 50/210 sao o mesmo mecanismo |
| o **enunciado** | reescrever a frase nao muda o que a regra faz |
| `content_hash` | ele e a identidade do PRE-REGISTRO, e nao da abordagem |

## Nao e proibicao eterna

Rejeitada uma vez, toda hipotese cuja assinatura casa herda o bloqueio - **para
entrar na 0C**. Uma mudanca realmente material de mecanismo produz assinatura
**diferente**, e volta como reprojeto pela **0B**, com pre-registro proprio.

Nunca entrando direto na 0C: ela nao tem pre-registro, nao tem familia e nao
tem orcamento de falsas descobertas para candidata nova, e usa-la como porta
seria pular o Portao B - o portao que existe para decidir se algo merece
forward.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Os campos que descrevem a FORMA da regra, e nao o valor dela. A presenca
# entra na assinatura; o conteudo, nunca.
CAMPOS_DE_FORMA = ("position_fraction_bps", "stop_loss_bps")


class AbordagemRejeitada(Exception):
    """A assinatura casa com uma abordagem ja descartada.

    §14.4 e literal sobre a hipotese 41: *"A abordagem esta descartada.
    Reprojetar."* Reprojetar e passar pela 0B de novo - nao e reentrar aqui
    com outro numero.
    """


@dataclass(frozen=True)
class Abordagem:
    assinatura: str
    familia: str
    forma: dict
    estado: str
    motivo: str | None = None


def _agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def forma_da_regra(params: dict, extras: dict | None = None) -> dict:
    """A FORMA: a familia e quais campos estao em uso. Nunca os valores.

    `params` e o `params_json` da regra, que funde os parametros da familia
    com `position_fraction_bps` e `stop_loss_bps` (o incremento 14 registrou
    essa fusao ao criar `registro.reconstruir`).

    O que sai daqui e ordenado e canonico, porque conjunto nao tem ordem e
    hash tem.
    """
    juntos = {**params, **(extras or {})}
    familia = str(juntos.get("familia") or juntos.get("family") or "")

    # Os parametros da FAMILIA entram por NOME, e nunca por valor: e o nome
    # que diz qual mecanismo esta em jogo (`rapida`/`lenta` e cruzamento de
    # medias; `janela`/`desvios` e banda), e o valor e so a calibragem dele.
    nomes_de_parametro = sorted(
        k for k in juntos
        if k not in ("familia", "family", "enunciado", "content_hash")
        and k not in CAMPOS_DE_FORMA
    )
    em_uso = sorted(
        k for k in CAMPOS_DE_FORMA if juntos.get(k) not in (None, 0)
    )
    return {
        "familia": familia,
        "parametros": nomes_de_parametro,
        "campos_em_uso": em_uso,
    }


def assinatura_de(params: dict, extras: dict | None = None) -> str:
    """A assinatura estrutural, canonica.

    NAO e sha256: e o proprio texto canonico, curto e LEGIVEL. Um humano
    olhando o registro tem de conseguir ver por que duas hipoteses sao a mesma
    abordagem - e um hash esconderia exatamente isso, que e a informacao que
    importa aqui.
    """
    return json.dumps(
        forma_da_regra(params, extras), sort_keys=True, separators=(",", ":")
    )


def registrar(
    conn: sqlite3.Connection, *, params: dict, extras: dict | None = None
) -> Abordagem:
    """Registra a abordagem como `aberta`, se ela ainda nao existe."""
    a = assinatura_de(params, extras)
    ja = ler(conn, a)
    if ja is not None:
        return ja
    forma = forma_da_regra(params, extras)
    conn.execute(
        "INSERT INTO abordagem (assinatura, familia, forma_json, estado,"
        " criado_em) VALUES (?,?,?,'aberta',?)",
        (a, forma["familia"], json.dumps(forma, sort_keys=True), _agora()),
    )
    return ler(conn, a)  # type: ignore[return-value]


def rejeitar(
    conn: sqlite3.Connection, *, params: dict, extras: dict | None = None,
    hypothesis_id: int, motivo: str,
) -> Abordagem:
    """Marca a abordagem como rejeitada, com de onde veio a rejeicao.

    `hypothesis_id` e `motivo` sao obrigatorios pelo `CHECK` da tabela: "esta
    abordagem esta descartada" tem de ter como ser conferido, e nao ser prosa.
    """
    registrar(conn, params=params, extras=extras)
    a = assinatura_de(params, extras)
    conn.execute(
        "UPDATE abordagem SET estado = 'rejeitada',"
        " rejeitada_por_hypothesis_id = ?, rejeitada_em = ?, motivo = ?"
        " WHERE assinatura = ?",
        (hypothesis_id, _agora(), motivo, a),
    )
    log.info("abordagem.rejeitada", extra={
        "assinatura": a, "hypothesis_id": hypothesis_id, "motivo": motivo,
    })
    return ler(conn, a)  # type: ignore[return-value]


def ler(conn: sqlite3.Connection, assinatura: str) -> Abordagem | None:
    linha = conn.execute(
        "SELECT assinatura, familia, forma_json, estado, motivo"
        "  FROM abordagem WHERE assinatura = ?",
        (assinatura,),
    ).fetchone()
    if linha is None:
        return None
    return Abordagem(
        assinatura=str(linha["assinatura"]), familia=str(linha["familia"]),
        forma=json.loads(linha["forma_json"]), estado=str(linha["estado"]),
        motivo=linha["motivo"],
    )


def exigir_nao_rejeitada(
    conn: sqlite3.Connection, *, params: dict, extras: dict | None = None
) -> Abordagem | None:
    """Levanta se a abordagem esta descartada. Devolve o registro, ou `None`.

    `None` significa "nunca vista", que e diferente de "aberta" - e as duas
    sao diferentes de "rejeitada". Colapsar as tres em booleano perderia a
    unica distincao que importa.
    """
    a = assinatura_de(params, extras)
    registro = ler(conn, a)
    if registro is not None and registro.estado == "rejeitada":
        raise AbordagemRejeitada(
            f"a abordagem {a} esta DESCARTADA: {registro.motivo}. "
            f"§14.4 e literal - reprojetar e passar pela 0B de novo, com "
            f"assinatura nova e pre-registro proprio, e nunca reentrar aqui "
            f"com outro numero. Variacao apenas parametrica ou textual da "
            f"mesma abordagem continua sendo ela"
        )
    return registro


def rejeitadas(conn: sqlite3.Connection) -> list[Abordagem]:
    """As descartadas, para o relatorio declarar cada exclusao (garantia 5)."""
    return [
        Abordagem(
            assinatura=str(r["assinatura"]), familia=str(r["familia"]),
            forma=json.loads(r["forma_json"]), estado=str(r["estado"]),
            motivo=r["motivo"],
        )
        for r in conn.execute(
            "SELECT assinatura, familia, forma_json, estado, motivo"
            "  FROM abordagem WHERE estado = 'rejeitada'"
            " ORDER BY assinatura"
        )
    ]
