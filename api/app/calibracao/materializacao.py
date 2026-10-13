"""O caminho OFICIAL de materializar observacao. ADR 0041.

Uma funcao por finalidade, e cada uma **deriva a propria janela do registro**.
Nenhuma aceita fronteira do chamador - a janela sai de `janela_piloto`,
`janela_calibracao` ou `janela_revalidacao`, que sao seladas antes do dado.

    do_piloto        -> janela_piloto, fechada, hash do MANIFESTO
    da_calibracao    -> janela_calibracao, selada, aberta no fim
    da_revalidacao   -> janela_revalidacao daquele REGIME, selada

## O `janela_hash` significa coisas diferentes, e isso vai publicado

| finalidade | o que o hash e | por que |
|---|---|---|
| `piloto` | o hash do **conteudo** do intervalo (`ee5a0413...`) | a janela esta fechada e a grade esta completa: o conteudo nao muda mais |
| `calibracao` | o hash do **selo** | a janela nao tem fim declarado - ela fecha por regime -, entao o conteudo ainda cresce |
| `revalidacao` | o hash do **selo** | idem: o periodo esta declarado e o dado ainda vai chegar |

Publicar a diferenca e o que impede um campo de significar duas coisas em
silencio. `o_que_o_janela_hash_e` sai em toda resposta.

## O que este modulo NAO faz

Nao ajusta parametro, nao grava perfil e nao cria `config_version`. Ele
materializa - e quem calibra le a janela de calibracao, que e outra.
"""

from __future__ import annotations

import hashlib
import sqlite3

from ..aovivo import bbo
from ..config.schema import ExperimentConfig
from . import observacao, piloto

#: A versao do formato do hash de SELO entra no proprio hash. Sem ela, mudar a
#: serializacao um dia produziria outro numero sobre o mesmo selo, e os dois
#: pareceriam divergencia do dado em vez de mudanca de regua.
FORMATO_SELO = "janela-selo@1"


class JanelaNaoSelada(Exception):
    """Nao ha janela desta finalidade. Materializar antes de selar seria
    escolher a fronteira depois de ver o dado."""


def _hash_do_selo(*campos: object) -> str:
    h = hashlib.sha256()
    h.update(FORMATO_SELO.encode("utf-8"))
    h.update(b"\x00")
    for c in campos:
        h.update(str(c).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _serie(venue: str, symbol: str) -> bbo.Serie:
    return bbo.Serie(venue=venue, symbol=symbol,
                     price_scale_exp=0, volume_scale_exp=0)


def do_piloto(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    config: ExperimentConfig, config_version_id: int,
    calibracao_perfil_hash: str, orcamento_cents: int,
) -> dict:
    """Materializa a janela FECHADA do piloto, e so ela.

    O `janela_hash` e o do **manifesto** - o mesmo `ee5a0413...` que o
    fechamento publica. Uma insercao tardia ou um manifesto que mudasse
    apareceriam como divergencia, porque o gatilho de congelamento recusa a
    segunda identidade.
    """
    serie = _serie(venue, symbol)
    janela = piloto.ler(conn, serie, contrato)
    if janela is None:
        raise JanelaNaoSelada(
            "a janela do piloto nao foi fechada. Materializar antes do"
            " fechamento deixaria a janela crescer ate o numero ficar agradavel"
        )

    from . import manifesto  # aqui, para nao criar ciclo no topo

    m = manifesto.do_registro(conn, serie=serie, contrato=contrato)
    janela_hash = m["manifesto"]["hash_do_intervalo"]

    r = observacao.registrar(
        conn, finalidade="piloto", contrato=contrato, venue=venue,
        symbol=symbol, config=config, config_version_id=config_version_id,
        calibracao_perfil_hash=calibracao_perfil_hash,
        janela_hash=janela_hash, de_ms=janela.de_ms,
        ate_ms_exclusive=janela.ate_ms_exclusive,
        orcamento_cents=orcamento_cents,
    )
    return {
        **r.como_dict(),
        "o_que_o_janela_hash_e": (
            "o hash do CONTEUDO do intervalo fechado, derivado do manifesto -"
            " a janela do piloto esta fechada e a grade esta completa, entao o"
            " conteudo nao muda mais"
        ),
        "o_que_sai_do_piloto": (
            "`sigma_e`, `tau` e a forma da cauda (ADR 0027 par.5). Estas"
            " observacoes NAO calibram e NAO produzem override de parametro:"
            " quem calibra le a janela de calibracao, posterior e disjunta"
        ),
    }


def da_calibracao(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    config: ExperimentConfig, config_version_id: int,
    calibracao_perfil_hash: str, orcamento_cents: int,
    ate_ms_exclusive: int,
) -> dict:
    """Materializa a janela de CALIBRACAO, do selo ate onde o dado alcanca.

    `ate_ms_exclusive` e o alcance do dado, e nao uma escolha: a janela nao tem
    fim declarado porque ela fecha **por regime**, quando cada um alcanca o
    proprio `n_exigido`. Fixar um fim aqui seria parar por calendario, que a
    decisao 4 do usuario proibe.
    """
    janela = ler_janela_de_calibracao(conn, contrato=contrato, venue=venue,
                                      symbol=symbol)
    if janela is None:
        raise JanelaNaoSelada(
            "nao ha janela de calibracao selada. Ela comeca numa fronteira de"
            " grade POSTERIOR ao proprio selamento (ADR 0041), e materializar"
            " antes de selar seria escolher o periodo depois de ver o dado"
        )
    janela_hash = _hash_do_selo(
        "calibracao", contrato, venue, symbol,
        janela["de_ms"], janela["selada_em_ms"], janela["grade_ms"],
    )
    # LIMITA PELO FIM DECLARADO. A janela ganhou fim (ADR 0041 par.9i), e
    # `ate_ms_exclusive` aqui e o alcance do DADO - que pode passar dele.
    # Sem o `min`, o gatilho `..._dentro_da_janela` abortaria a materializacao
    # inteira no primeiro instante posterior ao periodo.
    fim = min(int(ate_ms_exclusive), int(janela["ate_ms_exclusive"]))
    r = observacao.registrar(
        conn, finalidade="calibracao", contrato=contrato, venue=venue,
        symbol=symbol, config=config, config_version_id=config_version_id,
        calibracao_perfil_hash=calibracao_perfil_hash,
        janela_hash=janela_hash, de_ms=int(janela["de_ms"]),
        ate_ms_exclusive=fim, orcamento_cents=orcamento_cents,
    )
    return {
        **r.como_dict(),
        "o_que_o_janela_hash_e": (
            "o hash do SELO, e nao do conteudo: a janela de calibracao nao tem"
            " fim declarado - ela fecha por regime -, entao o conteudo ainda"
            " cresce e um hash de conteudo mudaria a cada lote"
        ),
    }


class IdentidadeDaCalibracaoAusente(Exception):
    """Nao ha calibracao APLICADA para este regime.

    A revalidacao confirma o que a calibracao produziu; sem override nao ha o
    que confirmar, e materializar sob a config vigente mediria outro simulador.
    """


def da_revalidacao(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
    regime: str, orcamento_cents: int,
) -> dict:
    """Materializa a janela de REVALIDACAO daquele regime.

    **A identidade executavel nao e recebida: ela e DERIVADA do fechamento a
    que a janela se liga.** A `config_version` e o perfil sao os que o ajuste
    daquele regime criou, e a simulacao roda sob ELES - nao sob a configuracao
    vigente, que e a que esta a mao e por isso a tentacao.

    Confirmar sob a config vigente mediria um simulador diferente daquele que o
    override descreve, e o `LB95` valeria sobre outra coisa. O gatilho
    `calibracao_observacao_identidade_da_revalidacao` recusa no BANCO; esta
    funcao simplesmente nao tem como errar, porque nao ha parametro de
    identidade para passar.
    """
    from ..config import service as config_service
    from . import ajuste as ajuste_mod

    janela = ajuste_mod.ler_revalidacao(
        conn, contrato=contrato, venue=venue, symbol=symbol, regime=regime
    )
    if janela is None:
        raise JanelaNaoSelada(
            f"nao ha janela de revalidacao selada para o regime {regime!r}."
            f" Ela nasce do fechamento da calibracao DELE, e materializar"
            f" antes de selar seria escolher o periodo depois de ver o dado"
        )

    identidade = conn.execute(
        # A identidade sai do CAMINHO INTEIRO: janela -> fechamento -> versao
        # de calibracao aplicada -> o regime que de fato ganhou override ->
        # a `config_version` nova e o perfil que ela referencia.
        "SELECT cv.id AS calibracao_versao_id,"
        "       cv.config_version_nova AS config_version_id,"
        "       nova.calibracao_perfil_hash AS perfil"
        "  FROM janela_revalidacao jr"
        "  JOIN calibracao_regime_fechamento f"
        "    ON f.id = jr.calibracao_fechamento_id"
        "  JOIN calibracao_versao cv"
        "    ON cv.janela_calibracao_id = f.janela_calibracao_id"
        "   AND cv.aplicado = 1"
        "  JOIN calibracao_regime cr"
        "    ON cr.calibracao_versao_id = cv.id AND cr.regime = jr.regime"
        "   AND cr.estado = 'calibrado'"
        "  JOIN config_version nova ON nova.id = cv.config_version_nova"
        " WHERE jr.id = ?",
        (int(janela["id"]),),
    ).fetchone()
    if identidade is None:
        raise IdentidadeDaCalibracaoAusente(
            f"o regime {regime!r} nao tem calibracao APLICADA: nao ha"
            f" `config_version` nem perfil produzidos por ela, e a revalidacao"
            f" confirma exatamente esses dois. A configuracao vigente NAO"
            f" ocupa o lugar deles"
        )

    versao = config_service.versao_por_id(
        conn, int(identidade["config_version_id"])
    )
    if versao is None:  # pragma: no cover - a FK acima ja garante
        raise IdentidadeDaCalibracaoAusente(
            "a config_version produzida pela calibracao nao existe mais"
        )

    janela_hash = _hash_do_selo(
        "revalidacao", contrato, venue, symbol, regime,
        janela["de_ms"], janela["ate_ms_exclusive"], janela["selada_em_ms"],
        janela["grade_ms"],
    )
    r = observacao.registrar(
        conn, finalidade="revalidacao", contrato=contrato, venue=venue,
        symbol=symbol, config=versao.config,
        config_version_id=int(identidade["config_version_id"]),
        calibracao_perfil_hash=str(identidade["perfil"]),
        janela_hash=janela_hash, de_ms=int(janela["de_ms"]),
        ate_ms_exclusive=int(janela["ate_ms_exclusive"]),
        orcamento_cents=orcamento_cents,
        # A janela e POR REGIME, e catorze dias atravessam varios. Sem este
        # filtro o primeiro instante de outro regime abortaria a
        # materializacao inteira, pelo gatilho `..._dentro_da_janela`.
        apenas_regime=regime,
    )
    return {
        **r.como_dict(),
        "regime": regime,
        "identidade_executavel": {
            "config_version_id": int(identidade["config_version_id"]),
            "calibracao_perfil_hash": str(identidade["perfil"]),
            "calibracao_versao_id": int(identidade["calibracao_versao_id"]),
            "de_onde_veio": (
                "do fechamento a que esta janela se liga, e nao da"
                " configuracao vigente. A revalidacao confirma o simulador que"
                " a calibracao DAQUELE regime produziu"
            ),
        },
        "o_que_o_janela_hash_e": (
            "o hash do SELO desta janela - periodo declarado, regime e"
            " instante do selamento -, e nao do conteudo: o dado ainda vai"
            " chegar"
        ),
    }


def ler_janela_de_calibracao(
    conn: sqlite3.Connection, *, contrato: str, venue: str, symbol: str,
) -> sqlite3.Row | None:
    # `SELECT *` de proposito: uma lista de colunas escrita a mao envelhece a
    # cada coluna nova, e as regras congeladas do ADR 0041 entraram depois.
    return conn.execute(
        "SELECT * FROM janela_calibracao"
        " WHERE contrato = ? AND venue = ? AND symbol = ?",
        (contrato, venue, symbol),
    ).fetchone()
