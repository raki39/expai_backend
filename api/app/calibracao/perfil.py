"""Perfil de calibracao: override POR REGIME. ADR 0033.

A frase que corrigiu o desenho anterior, e ela e do usuario:

    "Usar o maior valor observado nao significa ser pessimista onde nada foi
     observado. A etiqueta de 'nao calibrado' nao corrigiria o preco usado."

A parte 2c aplicava, num campo unico, o valor do regime mais pessimista entre
os calibrados. Mas o maximo sobre uma amostra de regimes **nao e limite
superior** para os regimes fora dela - e `vol_alta` e, por construcao da
taxonomia, o tercil de MAIOR volatilidade, onde o custo de execucao tende a
ser pior. Aplicar ali um numero medido no tercil mais calmo e otimismo com
aparencia de conservadorismo.

## A resolucao, e o passo 3 e o ponto inteiro

    1. classificar o regime de `t`, CAUSALMENTE, com a janela [t-672, t-1]
    2. ha override para esse regime?   -> usa o override
    3. nao ha, ou o regime e INDEFINIDO? -> usa o valor-BASE, e marca
                                            `nao_calibrado`

O passo 3 usa a **base**, e nao o override de outro regime.

## A ausencia de linha diz UMA coisa, e ate 2026-10-14 dizia duas

A frase original era *"a ausencia de linha E a informacao"*, e ela estava certa
sobre o que decidia: regime sem override nao herda parametro de outro. Mas
colava dois fatos diferentes.

    nao_calibrado           nao alcancou `n_exigido`. NAO ha medicao
    calibrado_sem_override  alcancou, e `p10(E2) >= delta` ja na base

O segundo e uma CONCLUSAO de calibracao, e das boas: o simulador ja e
pessimista o bastante naquele regime. Nao aumentar o spread ali e o resultado
correto da medicao, e nao ausencia de medicao.

**Hoje todo regime CALIBRADO tem linha**, com `tem_override` dizendo se o valor
efetivo difere da base. A ausencia significa `nao_calibrado`, e so isso.

    | estado | linha | valor efetivo | revalidacao |
    |---|---|---|---|
    | `nao_calibrado`          | nao existe | a base, e o resultado fica inconclusivo quanto a fidelidade | nao |
    | `calibrado_sem_override` | existe, `tem_override = 0` | a base, e isso foi MEDIDO | sim |
    | `calibrado_com_override` | existe, `tem_override = 1` | o ajustado | sim |

Os dois calibrados entram no hash, sao certificados juntos e geram janela de
revalidacao. O nao calibrado nao gera, e nunca herda parametro de outro regime
- a garantia 4 do ADR 0027, intacta.

E ha `CHECK` proibindo linha para `indefinido`, que nao e regime da taxonomia.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from ..regime import deteccao

log = logging.getLogger(__name__)

REGIMES_DA_TAXONOMIA = ("vol_baixa", "vol_media", "vol_alta")


class RegimeInvalido(Exception):
    """`indefinido` nao e regime da taxonomia, e nao pode ter override."""


#: Os TRES resultados possiveis por regime. `nao_calibrado` e a AUSENCIA de
#: linha - ele nao e um valor gravado, e nao poderia ser: gravar uma linha
#: dizendo "nao medi" faria a ausencia e a declaracao competirem pelo mesmo
#: fato.
NAO_CALIBRADO = "nao_calibrado"
CALIBRADO_SEM_OVERRIDE = "calibrado_sem_override"
CALIBRADO_COM_OVERRIDE = "calibrado_com_override"
ESTADOS = (NAO_CALIBRADO, CALIBRADO_SEM_OVERRIDE, CALIBRADO_COM_OVERRIDE)


@dataclass(frozen=True)
class Decisao:
    """A decisao de calibracao de UM regime. Ela existe sempre que ele fechou.

    Chamava-se `Override`, e o nome descrevia enquanto so havia linha para
    quem subia. Mantê-lo agora faria um objeto chamado `Override` representar
    "nao houve override" - a forma exata do defeito que este projeto conta.

    `spread_bps_x1000` e o valor EFETIVO: igual a base quando
    `tem_override` e falso. Quem le o perfil nao precisa saber qual dos dois
    casos e para usar o numero.
    """

    regime: str
    spread_bps_x1000: int
    tem_override: bool
    n: int
    p10_mili_bps: int
    motivo: str

    @property
    def estado(self) -> str:
        return CALIBRADO_COM_OVERRIDE if self.tem_override else CALIBRADO_SEM_OVERRIDE


@dataclass(frozen=True)
class Perfil:
    hash: str
    spread_bps_base_x1000: int
    decisoes: dict[str, Decisao]
    taxonomia: dict[str, int]

    def spread_bps_x1000(self, regime: str | None) -> tuple[int, bool]:
        """O `spread_bps` efetivo do instante, e se ele foi CALIBRADO.

        `regime` vem do detector e pode ser `None` (indefinido). Sem decisao
        para ele, devolve a BASE - nunca o valor de outro regime.

        **O segundo elemento diz CALIBRADO, e nao "tem override".** Um regime
        `calibrado_sem_override` devolve `(base, True)`: o valor e o da base
        porque foi isso que a medicao concluiu, e dizer `False` ali
        publicaria como nao medido um regime que foi medido.
        """
        d = None if regime is None else self.decisoes.get(regime)
        if d is not None:
            return d.spread_bps_x1000, True
        return self.spread_bps_base_x1000, False

    def estado(self, regime: str | None) -> str:
        """Um dos TRES. A ausencia de decisao e `nao_calibrado`, e so isso."""
        d = None if regime is None else self.decisoes.get(regime)
        return NAO_CALIBRADO if d is None else d.estado


def _agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def calcular_hash(
    *, spread_bps_base_x1000: int, decisoes: list[Decisao],
) -> str:
    """`sha256` sobre a taxonomia declarada e as DECISOES ordenadas.

    A taxonomia entra porque, se os cortes da D40 mudassem, `vol_alta`
    passaria a nomear outra coisa e o override deixaria de descrever o que
    descrevia. Sem ela no hash, o perfil sobreviveria a uma mudanca que o
    invalida - e sobreviveria calado.

    Ordenado porque conjunto nao tem ordem e hash tem: sem `sort_keys`, dois
    perfis identicos teriam hashes diferentes conforme a ordem de insercao.
    """
    corpo = {
        "taxonomia": {
            "corte_inferior_mili_bps": deteccao.CORTE_INFERIOR_MILI_BPS,
            "corte_superior_mili_bps": deteccao.CORTE_SUPERIOR_MILI_BPS,
            "janela_barras": deteccao.JANELA_BARRAS,
            "permanencia_barras": deteccao.PERMANENCIA_BARRAS,
        },
        "spread_bps_base_x1000": spread_bps_base_x1000,
        # A chave mudou de `overrides` para `decisoes` em 2026-10-14, e isso
        # SOZINHO muda todo hash - o que e o certo: o perfil passou a
        # representar outra coisa, e um perfil novo com hash antigo afirmaria
        # equivalencia que nao existe.
        #
        # `motivo` fica FORA: ele e prosa, e reescrever a frase nao muda o
        # simulador. Dentro do hash, uma virgula exigiria recertificar.
        "decisoes": {
            d.regime: {
                "spread_bps_x1000": d.spread_bps_x1000,
                "tem_override": bool(d.tem_override),
                "n": d.n,
                "p10_mili_bps": d.p10_mili_bps,
            }
            for d in sorted(decisoes, key=lambda x: x.regime)
        },
    }
    canonico = json.dumps(corpo, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def gravar(
    conn: sqlite3.Connection, *, spread_bps_base_x1000: int,
    decisoes: list[Decisao],
) -> Perfil:
    """Grava o perfil. Idempotente pelo hash: mesmo conteudo, mesmo perfil.

    Um override para `indefinido` e RECUSADO aqui e no banco. Ele nao e um
    quarto regime - e a declaracao de que faltam 672 barras anteriores para
    classificar, e dar-lhe parametro seria calibrar uma classe que nao existe.
    """
    for d in decisoes:
        if d.regime not in REGIMES_DA_TAXONOMIA:
            raise RegimeInvalido(
                f"{d.regime!r} nao e regime da taxonomia {REGIMES_DA_TAXONOMIA}. "
                f"`indefinido` nunca pode herdar parametro de outro regime"
            )
        # A mesma regra do gatilho, do lado de ca. Ela e repetida de proposito:
        # o gatilho protege o BANCO de qualquer caminho, e esta levanta um erro
        # legivel antes de o `INSERT` sair - e ha teste exigindo que as duas
        # recusem o mesmo caso.
        if d.tem_override and d.spread_bps_x1000 <= spread_bps_base_x1000:
            raise RegimeInvalido(
                f"{d.regime!r} declara override com {d.spread_bps_x1000} sobre"
                f" base {spread_bps_base_x1000}: override que nao SOBE nao e"
                f" override, e reduzir custo tornaria o simulador otimista"
            )
        if not d.tem_override and d.spread_bps_x1000 != spread_bps_base_x1000:
            raise RegimeInvalido(
                f"{d.regime!r} declara `calibrado_sem_override` com valor"
                f" {d.spread_bps_x1000} diferente da base"
                f" {spread_bps_base_x1000}: sem override o valor efetivo E a"
                f" base"
            )

    h = calcular_hash(
        spread_bps_base_x1000=spread_bps_base_x1000, decisoes=decisoes
    )
    existente = ler(conn, h)
    if existente is not None:
        return existente

    conn.execute(
        "INSERT INTO calibracao_perfil (hash,"
        " taxonomia_corte_inferior_mili_bps, taxonomia_corte_superior_mili_bps,"
        " taxonomia_janela_barras, taxonomia_permanencia_barras,"
        " spread_bps_base_x1000, criado_em)"
        " VALUES (?,?,?,?,?,?,?)",
        (h, deteccao.CORTE_INFERIOR_MILI_BPS, deteccao.CORTE_SUPERIOR_MILI_BPS,
         deteccao.JANELA_BARRAS, deteccao.PERMANENCIA_BARRAS,
         spread_bps_base_x1000, _agora()),
    )
    for d in sorted(decisoes, key=lambda x: x.regime):
        conn.execute(
            "INSERT INTO calibracao_perfil_regime (perfil_hash, regime,"
            " spread_bps_x1000, n, p10_mili_bps, tem_override, motivo)"
            " VALUES (?,?,?,?,?,?,?)",
            (h, d.regime, d.spread_bps_x1000, d.n, d.p10_mili_bps,
             1 if d.tem_override else 0, d.motivo),
        )

    decididos = {d.regime for d in decisoes}
    log.info("calibracao.perfil_gravado", extra={
        "hash": h[:16], "base_x1000": spread_bps_base_x1000,
        "calibrado_com_override": [
            d.regime for d in decisoes if d.tem_override],
        "calibrado_sem_override": [
            d.regime for d in decisoes if not d.tem_override],
        "nao_calibrado": [
            r for r in REGIMES_DA_TAXONOMIA if r not in decididos],
    })
    return ler(conn, h)  # type: ignore[return-value]


def ler(conn: sqlite3.Connection, perfil_hash: str) -> Perfil | None:
    linha = conn.execute(
        "SELECT hash, spread_bps_base_x1000,"
        "       taxonomia_corte_inferior_mili_bps AS corte_inferior,"
        "       taxonomia_corte_superior_mili_bps AS corte_superior,"
        "       taxonomia_janela_barras AS janela,"
        "       taxonomia_permanencia_barras AS permanencia"
        "  FROM calibracao_perfil WHERE hash = ?",
        (perfil_hash,),
    ).fetchone()
    if linha is None:
        return None
    decisoes = {
        r["regime"]: Decisao(
            regime=r["regime"], spread_bps_x1000=int(r["spread_bps_x1000"]),
            tem_override=bool(r["tem_override"]),
            n=int(r["n"]), p10_mili_bps=int(r["p10_mili_bps"]),
            motivo=str(r["motivo"]),
        )
        for r in conn.execute(
            "SELECT regime, spread_bps_x1000, n, p10_mili_bps, tem_override,"
            "       motivo"
            "  FROM calibracao_perfil_regime WHERE perfil_hash = ?"
            " ORDER BY regime",
            (perfil_hash,),
        )
    }
    return Perfil(
        hash=str(linha["hash"]),
        spread_bps_base_x1000=int(linha["spread_bps_base_x1000"]),
        decisoes=decisoes,
        taxonomia={
            "corte_inferior_mili_bps": int(linha["corte_inferior"]),
            "corte_superior_mili_bps": int(linha["corte_superior"]),
            "janela_barras": int(linha["janela"]),
            "permanencia_barras": int(linha["permanencia"]),
        },
    )


def da_config_version(
    conn: sqlite3.Connection, config_version_id: int
) -> Perfil | None:
    """O perfil vinculado a uma `config_version`, ou `None`.

    `None` nas versoes antigas, e e assim de proposito: o vinculo e uma COLUNA
    da tabela e nao um campo do payload, entao o `config_hash` das versoes ja
    gravadas fica exatamente como sempre foi.

    Sem perfil, o simulador usa `config.spread_bps` como sempre usou - nenhum
    caminho historico muda de comportamento, e R12 continua valendo.
    """
    linha = conn.execute(
        "SELECT calibracao_perfil_hash FROM config_version WHERE id = ?",
        (config_version_id,),
    ).fetchone()
    if linha is None or linha["calibracao_perfil_hash"] is None:
        return None
    return ler(conn, str(linha["calibracao_perfil_hash"]))


# NAO HA `vincular`, e a ausencia foi descoberta implementando.
#
# Eu havia escrito uma funcao que fazia `UPDATE config_version SET
# calibracao_perfil_hash`, com uma checagem recusando a troca. Ela NUNCA
# poderia funcionar: `config_version` e imutavel por gatilho, e o `UPDATE` e
# recusado pelo banco antes de a checagem significar coisa alguma.
#
# Uma funcao que nao tem caminho e a forma do `BLOCOS`: declarada, com um
# comentario afirmando o que ela protege, e sem nada por tras. O vinculo e
# feito no INSERT, por `config_service.criar_versao`, e "nao se troca o perfil
# de uma versao" e verdade porque a LINHA nao muda - nao porque alguem confere.

# ===========================================================================
# O que um resultado pode afirmar
# ===========================================================================


@dataclass(frozen=True)
class Fidelidade:
    conclusiva: bool
    regimes_tocados: list[str]
    regimes_calibrados: list[str]
    regimes_nao_calibrados: list[str]
    motivo: str


def fidelidade(
    perfil: Perfil | None, regimes_tocados: list[str | None]
) -> Fidelidade:
    """Um resultado que atravessa regime nao calibrado fica INCONCLUSIVO.

    Nao reprovado, nao aprovado - a mesma forma que §8.4.1.1 da ao nivel de
    fidelidade: *"o simulador declara seu nivel, e o nivel vira condicao de
    validade do conhecimento"*.

    E o shadow **continua rodando** num regime nao calibrado: acumular
    evidencia ali e exatamente como ele deixa de ser nao calibrado, e parar
    garantiria que ele nunca saisse desse estado.
    """
    nomes = sorted({r if r is not None else "indefinido" for r in regimes_tocados})
    # CALIBRADO, e nao "tem override". Um regime `calibrado_sem_override` foi
    # medido: a medicao concluiu que a base ja basta ali. Conta-lo como nao
    # calibrado tornaria inconclusivo um resultado cuja fidelidade E conhecida
    # - e empurraria na direcao de preferir o override a conclusao correta.
    decididos = set(perfil.decisoes) if perfil is not None else set()
    calibrados = sorted(n for n in nomes if n in decididos)
    faltando = sorted(n for n in nomes if n not in decididos)

    if not nomes:
        return Fidelidade(
            conclusiva=False, regimes_tocados=[], regimes_calibrados=[],
            regimes_nao_calibrados=[],
            motivo="nenhum instante classificado: nao ha o que afirmar",
        )
    if not faltando:
        return Fidelidade(
            conclusiva=True, regimes_tocados=nomes,
            regimes_calibrados=calibrados, regimes_nao_calibrados=[],
            motivo=(
                "todo regime tocado foi CALIBRADO - com ou sem override. O"
                " valor efetivo de cada um saiu de medicao propria, e nenhum"
                " herdou parametro de outro"
            ),
        )
    return Fidelidade(
        conclusiva=False, regimes_tocados=nomes,
        regimes_calibrados=calibrados, regimes_nao_calibrados=faltando,
        motivo=(
            f"o periodo atravessa regime(s) NAO CALIBRADO(S): "
            f"{', '.join(faltando)}. Ali o simulador usou o valor-BASE, que e "
            f"hipotese e nao medicao - entao o resultado e INCONCLUSIVO quanto "
            f"a fidelidade. O shadow segue rodando para acumular evidencia"
        ),
    )
