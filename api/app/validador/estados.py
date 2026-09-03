"""A máquina de estados do conhecimento (§8.1, R32).

```
IDEIA
  ↓ (pré-registro)
HIPÓTESE REGISTRADA
  ↓ (teste in-sample)
CANDIDATA
  ↓ (teste out-of-sample)
EM QUARENTENA
  ↓ (observação em dados futuros, tempo mínimo)
CONHECIMENTO VALIDADO
  ↓ (monitoramento contínuo)
  ├→ REVALIDADO · CONDICIONADO · EM SUSPEITA · INVALIDADO
```

> "Nenhum estado pode ser pulado." — §8.1

## Log de transições, não coluna

`hypothesis` é imutável desde a migração 9 — não existe `UPDATE` nela. Um
estado que muda precisaria de um, então o estado corrente é **derivado** da
última transição, pela view `hypothesis_estado_atual`.

É o mesmo desenho do saldo, que sai do ledger e não de uma coluna (regra 16).
Duas fontes de verdade sobre o estado divergiriam no dia em que alguém
esquecesse de atualizar uma — e o dia em que isso acontece é justamente o dia
em que alguém consulta o estado para decidir promover.

## Este módulo não valida as transições

De propósito. Quem recusa é o banco, por três gatilhos: a entrada é sempre por
`hipotese_registrada`, a transição parte do estado **atual**, e o par
`(de, para)` precisa existir em `transicao_legal`.

Se a validação morasse aqui, um defeito neste arquivo mascararia a ausência da
regra no schema — o mesmo motivo pelo qual as partidas dobradas moram em
gatilho desde o incremento 2. Os testes inserem SQL cru e esperam a recusa.

## Duas leituras declaradas, e não resolvidas em silêncio

**IDEIA não é persistida.** §8.1 a lista como estado, e §8.2 diz que a hipótese
é gravada **no** pré-registro — antes dele não há hipótese a que atribuir
estado. Gravar `ideia` e `hipotese_registrada` no mesmo instante seria uma
transição que nunca falha e nada informa. A entrada na máquina é o
pré-registro.

**INVALIDADO também é alcançável antes de validar.** §8.1 desenha a seta dele
saindo só do monitoramento contínuo. Mas §14.4 exige o desfecho "rejeitado"
antes disso, e §8.6 exige que toda tentativa fique registrada "inclusive as que
falharam". Sem essa aresta, uma hipótese refutada no in-sample ficaria parada
em `hipotese_registrada`, indistinguível de uma que nunca foi testada.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Quem promove. Um valor só, num lugar só - e o banco tem o mesmo literal num
# CHECK, porque §8.1 diz que o agente não promove a própria hipótese.
PROMOTOR = "validador"

ENTRADA = "hipotese_registrada"

# Estados a partir dos quais nada sai. Não é lista de conveniência: é o que
# distingue "acabou" de "parou aqui e ninguém percebeu".
TERMINAIS: frozenset[str] = frozenset({"invalidado", "nao_testavel"})

# Alcançado pelo forward da 0C, e por nada da 0B. Existe como estado desde já
# porque a transição para ele precisa ser possível de gravar quando a 0C
# chegar - retrofitar a máquina depois exigiria reprocessar o histórico.
QUARENTENA = "em_quarentena"

# O que NÃO conta como promoção. A tolerância zero de §14.4 é sobre o
# complemento disto, e por isso ele mora num lugar só.
#
# Escrito como a **ausência**, e não como a lista dos estados promovidos, de
# propósito. Uma lista positiva precisaria nomear cada estado adiante no
# caminho de §8.1 — e um estado novo que ninguém lembrasse de acrescentar
# sairia calado da contagem, o que faria um controle promovido passar
# despercebido exatamente onde a tolerância é zero.
#
# Pela ausência, o erro cai na direção certa: um estado novo conta como
# promoção até que alguém decida o contrário, e o portão reprova em vez de
# aprovar por omissão.
NAO_PROMOVIDOS: frozenset[str] = frozenset({ENTRADA}) | TERMINAIS


def promovida(estado: str | None) -> bool:
    """A hipótese saiu da entrada e não terminou em recusa? Então foi promovida.

    Promover é MOVER a hipótese adiante no caminho principal de §8.1, e é a
    transição que fica gravada. Ler o veredito em texto em vez do estado
    deixaria de fora uma promoção que acontecesse por outro caminho — e é
    exatamente essa promoção que a tolerância zero de §14.4 existe para pegar.
    """
    return bool(estado) and estado not in NAO_PROMOVIDOS


class TransicaoRecusada(Exception):
    """O banco recusou. A mensagem dele é a explicação."""


@dataclass(frozen=True)
class Estado:
    hypothesis_id: int
    estado: str
    desde: str | None
    transicoes: int

    @property
    def terminal(self) -> bool:
        return self.estado in TERMINAIS

    def como_dict(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "estado": self.estado,
            "desde": self.desde,
            "transicoes": self.transicoes,
            "terminal": self.terminal,
        }


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atual(conn: sqlite3.Connection, hypothesis_id: int) -> Estado | None:
    """O estado corrente, lido da view derivada."""
    linha = conn.execute(
        "SELECT hypothesis_id, estado, desde, transicoes"
        "  FROM hypothesis_estado_atual WHERE hypothesis_id = ?",
        (hypothesis_id,),
    ).fetchone()
    if linha is None:
        return None
    return Estado(
        hypothesis_id=int(linha["hypothesis_id"]),
        estado=linha["estado"],
        desde=linha["desde"],
        transicoes=int(linha["transicoes"]),
    )


def registrar_entrada(
    conn: sqlite3.Connection, hypothesis_id: int, *, evidencia: dict
) -> int:
    """Põe a hipótese na máquina, em `hipotese_registrada`.

    Chamado logo depois do pré-registro. É a materialização da aresta
    IDEIA → HIPÓTESE REGISTRADA de §8.1: a hipótese passa a existir *porque*
    foi pré-registrada.
    """
    return _gravar(
        conn,
        hypothesis_id=hypothesis_id,
        de=None,
        para=ENTRADA,
        evidencia=evidencia,
    )


def transitar(
    conn: sqlite3.Connection,
    hypothesis_id: int,
    *,
    para: str,
    evidencia: dict,
) -> int:
    """Move a hipótese, partindo do estado atual.

    O `de` não é parâmetro: é lido do banco. Deixar o chamador declarar de
    onde parte permitiria promover uma hipótese já invalidada bastando
    informar o estado conveniente — e o gatilho recusaria, mas só depois de o
    chamador ter acreditado que podia.
    """
    agora = atual(conn, hypothesis_id)
    if agora is None:
        raise TransicaoRecusada(
            f"hipótese {hypothesis_id} não existe"
        )
    return _gravar(
        conn,
        hypothesis_id=hypothesis_id,
        de=agora.estado if agora.estado != "sem_estado" else None,
        para=para,
        evidencia=evidencia,
    )


def _gravar(
    conn: sqlite3.Connection,
    *,
    hypothesis_id: int,
    de: str | None,
    para: str,
    evidencia: dict,
) -> int:
    proximo = int(
        conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM hypothesis_state"
            " WHERE hypothesis_id = ?",
            (hypothesis_id,),
        ).fetchone()["n"]
    )
    try:
        cur = conn.execute(
            "INSERT INTO hypothesis_state (hypothesis_id, seq, from_state,"
            " state, occurred_at, promoted_by, evidence_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                hypothesis_id,
                proximo,
                de,
                para,
                _agora(),
                PROMOTOR,
                json.dumps(evidencia, ensure_ascii=False, sort_keys=True),
            ),
        )
    except sqlite3.IntegrityError as erro:
        raise TransicaoRecusada(
            f"transição {de!r} -> {para!r} recusada pelo banco: {erro}"
        ) from erro
    log.info(
        "estado.transicao",
        extra={
            "hypothesis_id": hypothesis_id,
            "de": de,
            "para": para,
            "seq": proximo,
        },
    )
    return int(cur.lastrowid)


def historico(conn: sqlite3.Connection, hypothesis_id: int) -> list[dict]:
    """A sequência inteira. É ela que prova que nenhum estado foi pulado."""
    return [
        {
            "seq": int(l["seq"]),
            "de": l["from_state"],
            "para": l["state"],
            "quando": l["occurred_at"],
            "por": l["promoted_by"],
            "evidencia": json.loads(l["evidence_json"]),
        }
        for l in conn.execute(
            "SELECT seq, from_state, state, occurred_at, promoted_by,"
            " evidence_json FROM hypothesis_state"
            " WHERE hypothesis_id = ? ORDER BY seq",
            (hypothesis_id,),
        )
    ]


def transicoes_legais(conn: sqlite3.Connection, de: str) -> list[str]:
    """Para onde se pode ir daqui. Lido da tabela, nunca de uma constante."""
    return [
        l["para"]
        for l in conn.execute(
            "SELECT para FROM transicao_legal WHERE de = ? ORDER BY para",
            (de,),
        )
    ]


def populacao(conn: sqlite3.Connection) -> dict[str, int]:
    """Quantas hipóteses em cada estado. Para o painel e para a auditoria."""
    return {
        l["estado"]: int(l["n"])
        for l in conn.execute(
            "SELECT estado, COUNT(*) AS n FROM hypothesis_estado_atual"
            " GROUP BY estado ORDER BY estado"
        )
    }
