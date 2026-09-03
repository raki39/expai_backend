"""O lote fechado: BY sobre a família, e o DSR sobre quem sobrevive.

> "Fase 0: família fechada e procedimento em lote. (...) Fechamento do lote: no
> fim da Fase 0." — §8.6

Este módulo é o que transforma pareceres individuais numa decisão de lote.
Cada parecer, sozinho, diz se aquela hipótese bateu o efeito mínimo dela. O
lote diz outra coisa: **quantas dessas podem ser promovidas sem estourar o
orçamento de falsas descobertas de todas juntas.**

## `m` é o TETO da família, não quantas foram testadas

§8.6: "número máximo de hipóteses: fixado antes de começar". A multiplicidade a
descontar é esse teto.

Usar quantas por acaso chegaram a ser testadas tornaria o limiar mais generoso
justamente nos lotes que testaram menos — e um agente que parasse de testar ao
ver dois resultados bons compraria, com isso, um limiar mais frouxo para eles.
É escolher a régua depois de ver a amostra, com passos extra.

## Os p-valores são RECALCULADOS, não lidos de onde foram gravados

Cada hipótese é rejulgada do ledger e das execuções no momento em que o lote
roda. Isso não é desperdício: garante que os `m` p-valores que BY ordena vêm
todos do mesmo código, sobre o mesmo dado. P-valores gravados em momentos
diferentes poderiam ter sido produzidos por versões diferentes da conta, e o
lote passaria a ordenar números que não são comparáveis entre si.

## Duas barreiras em série, e a ordem importa

BY decide **quantas** promover controlando FDR. O DSR pergunta outra coisa, por
hipótese: *"o Sharpe verdadeiro é maior que zero depois de descontada a
seleção?"* (§8.6). São independentes e §8.6 pede as duas — "aplica-se
**adicionalmente** o Deflated Sharpe Ratio".

Uma hipótese que passa em BY e falha no DSR não é promovida. O contrário
também. Substituir uma pela outra deixaria de fora exatamente o que a outra
pega: BY não sabe nada sobre assimetria e curtose, e o DSR não sabe nada sobre
as outras hipóteses do lote.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from pydantic import ValidationError

from ..estatistica import dsr as dsr_mod
from ..estatistica import fdr as fdr_mod
from . import contador, estados, promocao

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Membro:
    hypothesis_id: int
    run_id: int
    content_hash: str
    estado: str
    #: De qual braco. O lote e onde os dois sao comparados (§14.3), e sem isto
    #: quarenta e oito linhas misturadas nao respondem "quantas sobreviveram
    #: de cada lado" - que e a pergunta do Portao A.
    #:
    #: Sem valor padrao de proposito: um `Membro` construido sem dizer o braco
    #: apareceria no lote como "?" e a comparacao contaria errado em silencio.
    agente_origem: str
    p_valor_ppm: int | None
    por_que_sem_p: str | None = None
    dsr: dict | None = None
    detalhe: dict = field(default_factory=dict)

    def como_dict(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "run_id": self.run_id,
            "content_hash": self.content_hash[:12],
            "agente_origem": self.agente_origem,
            "estado": self.estado,
            "p_valor_ppm": self.p_valor_ppm,
            "por_que_sem_p": self.por_que_sem_p,
            "dsr": self.dsr,
        }


def membros(conn: sqlite3.Connection, config_version_id: int) -> list[Membro]:
    """As hipóteses do lote, com p-valor recalculado.

    O lote é o da `config_version`: uma mudança material abre família nova, e
    §10.2.3 já invalida toda comparação que a atravesse. Misturar duas configs
    aqui ordenaria p-valores de experimentos diferentes.
    """
    linhas = list(
        conn.execute(
            "SELECT h.id AS hid, h.run_id AS run_id,"
            "       h.content_hash AS hash, h.testavel AS testavel,"
            # De qual BRACO. O lote e onde os dois sao comparados, e um
            # membro que nao diz de onde veio torna a comparacao ilegivel:
            # com 48 linhas misturadas, "quantas sobreviveram de cada lado"
            # deixa de ser uma pergunta que a tabela responde.
            "       h.agente_origem AS agente_origem"
            "  FROM hypothesis h JOIN run r ON r.id = h.run_id"
            " WHERE r.config_version_id = ?"
            " ORDER BY h.id",
            (config_version_id,),
        )
    )
    saida: list[Membro] = []
    for l in linhas:
        hid, run_id = int(l["hid"]), int(l["run_id"])
        estado = estados.atual(conn, hid)
        nome = estado.estado if estado else "sem_estado"

        if not l["testavel"]:
            saida.append(
                Membro(
                    hypothesis_id=hid,
                    run_id=run_id,
                    content_hash=l["hash"],
                    agente_origem=l["agente_origem"],
                    estado=nome,
                    p_valor_ppm=None,
                    por_que_sem_p=(
                        "hipótese não testável (§8.3): a amostra nunca alcança"
                        " o n_minimo, e um p-valor aqui descreveria um teste"
                        " que não pode concluir"
                    ),
                )
            )
            continue
        try:
            _, detalhe = promocao._julgar(conn, hid, run_id)
        except (promocao.NaoAvaliavel, ValidationError) as erro:
            saida.append(
                Membro(
                    hypothesis_id=hid,
                    run_id=run_id,
                    content_hash=l["hash"],
                    agente_origem=l["agente_origem"],
                    estado=nome,
                    p_valor_ppm=None,
                    # `ValidationError` entra aqui de proposito. Uma linha
                    # de pre-registro malformada - possivel so por INSERT cru,
                    # porque o schema a impede - nao pode derrubar o lote
                    # inteiro: ela vira um membro sem p-valor, com o motivo
                    # escrito, e as outras 47 seguem sendo julgadas.
                    por_que_sem_p=f"{type(erro).__name__}: {erro}",
                )
            )
            continue
        est = detalhe.get("estatistica") or {}
        saida.append(
            Membro(
                hypothesis_id=hid,
                run_id=run_id,
                content_hash=l["hash"],
                agente_origem=l["agente_origem"],
                estado=nome,
                p_valor_ppm=est.get("p_valor_ppm"),
                por_que_sem_p=est.get("por_que"),
                detalhe=detalhe,
            )
        )
    return saida


@dataclass(frozen=True)
class Entrada:
    """Uma candidata do lote, reduzida ao que a DECISÃO precisa.

    Existe para que A1b — as execuções repetidas de §14.4 — passe pelo mesmo
    procedimento de decisão que o lote real, e não por uma cópia dele. Duas
    implementações do mesmo procedimento divergem, e aqui a divergência seria
    invisível do pior jeito: o calibre mediria um procedimento que não é o que
    promove.
    """

    chave: str
    p_valor_ppm: int | None
    #: `sharpe_por_observacao_milionesimos`, `n`, `assimetria_milesimos`,
    #: `curtose_milesimos` — o bloco de momentos, ou `None` quando a série é
    #: curta demais para ter quarto momento.
    momentos: dict | None


@dataclass(frozen=True)
class Decisao:
    fdr: dict
    #: As chaves que passaram em BY **e** no DSR.
    sobreviventes: list[str]
    #: Por chave, o bloco do DSR — inclusive as indisponíveis, com o motivo.
    dsr_por_chave: dict[str, dict]


def decidir(
    entradas: list[Entrada],
    *,
    procedimento: str,
    alfa_bps: int,
    m: int,
    dsr_minimo_milesimos: int,
    tentativas: int,
) -> Decisao:
    """BY e depois o DSR. **Uma definição, usada pelo lote real e por A1b.**

    Pura: não toca no banco e não move estado nenhum. O que ela decide é
    "quantas destas podem ser promovidas sem estourar o orçamento de falsas
    descobertas de todas juntas", que é a pergunta do lote — e é exatamente a
    pergunta que A1b calibra.
    """
    com_p = {
        e.chave: e.p_valor_ppm for e in entradas if e.p_valor_ppm is not None
    }
    resultado_fdr = fdr_mod.aplicar(
        com_p, procedimento=procedimento, alfa_bps=alfa_bps, m=m
    )
    rejeitadas = set(resultado_fdr.rejeitadas)

    sobreviventes: list[str] = []
    dsr_por_chave: dict[str, dict] = {}
    for e in entradas:
        if e.chave not in rejeitadas:
            continue
        est = e.momentos or {}
        n = est.get("n")
        if not n or n < 2:
            dsr_por_chave[e.chave] = {"disponivel": False}
            continue
        try:
            bloco = dsr_mod.calcular(
                sharpe_por_observacao=(
                    est["sharpe_por_observacao_milionesimos"] / 1_000_000
                ),
                n=n,
                tentativas=tentativas,
                assimetria=est["assimetria_milesimos"] / 1_000,
                curtose_bruta=est["curtose_milesimos"] / 1_000,
                limiar_milesimos=dsr_minimo_milesimos,
            ).como_dict()
        except dsr_mod.DSRImpossivel as erro:
            bloco = {"disponivel": False, "por_que": str(erro)}
        dsr_por_chave[e.chave] = bloco
        if bloco.get("aprovado"):
            sobreviventes.append(e.chave)

    return Decisao(
        fdr=resultado_fdr.como_dict(),
        sobreviventes=sobreviventes,
        dsr_por_chave=dsr_por_chave,
    )


@dataclass(frozen=True)
class Fechamento:
    config_version_id: int
    familia_max: int
    testadas: int
    sem_p_valor: int
    fdr: dict
    membros: list[dict]
    sobreviventes: list[int]
    tentativas_globais: int

    def como_dict(self) -> dict:
        return {
            "config_version_id": self.config_version_id,
            "familia_max": self.familia_max,
            "testadas": self.testadas,
            "sem_p_valor": self.sem_p_valor,
            "fdr": self.fdr,
            "membros": self.membros,
            "sobreviventes": self.sobreviventes,
            "tentativas_globais": self.tentativas_globais,
            "nota": (
                "sobrevivente passou em BY E no DSR. §8.6 pede as duas:"
                " 'aplica-se adicionalmente o Deflated Sharpe Ratio'"
            ),
        }


def fechar(
    conn: sqlite3.Connection,
    *,
    config_version_id: int,
    familia_max: int,
    procedimento: str,
    alfa_bps: int,
    dsr_minimo_milesimos: int,
) -> Fechamento:
    """Roda o procedimento de lote e o DSR. **Não promove nada.**

    Deliberado: promover a partir daqui exigiria mover estados em massa, e a
    máquina de §8.1 é por hipótese, com evidência por transição. O fechamento
    do lote é um **parecer sobre o conjunto**; quem move cada hipótese
    continua sendo `promocao`, uma a uma, com a evidência dela.

    Separar as duas coisas também mantém o fechamento **repetível**: dá para
    rodá-lo quantas vezes se quiser sem que nada mude de estado, o que é o que
    permite olhar o lote antes de decidir.
    """
    todos = membros(conn, config_version_id)
    com_p = {
        str(mb.hypothesis_id): mb.p_valor_ppm
        for mb in todos
        if mb.p_valor_ppm is not None
    }

    # O N do DSR é o contador GLOBAL, e não o tamanho deste lote.
    #
    # §8.6: "o contador global é registro histórico (...) alimenta o cálculo do
    # DSR". Usar o tamanho do lote esqueceria todas as tentativas anteriores -
    # e o DSR existe justamente porque "um Sharpe de 1,5 após 10 tentativas e
    # um Sharpe de 1,5 após 5.000 tentativas não são a mesma evidência".
    tentativas = max(1, contador.total(conn))

    # A DECISÃO sai de `decidir`, que A1b também usa. Antes ela estava escrita
    # aqui dentro, e o calibre teria de reimplementá-la - duas versões do
    # mesmo procedimento, com a divergência invisível justamente onde ela
    # importaria: o calibre mediria um procedimento que não é o que promove.
    decisao = decidir(
        [
            Entrada(
                chave=str(mb.hypothesis_id),
                p_valor_ppm=mb.p_valor_ppm,
                momentos=(mb.detalhe.get("estatistica") or {}).get("momentos"),
            )
            for mb in todos
        ],
        procedimento=procedimento,
        alfa_bps=alfa_bps,
        m=familia_max,
        dsr_minimo_milesimos=dsr_minimo_milesimos,
        tentativas=tentativas,
    )
    resultado_fdr = decisao.fdr
    rejeitadas = set(resultado_fdr["rejeitadas"])
    sobreviventes = [int(c) for c in decisao.sobreviventes]

    finais: list[Membro] = [
        mb
        if str(mb.hypothesis_id) not in rejeitadas
        else Membro(
            **{
                **mb.__dict__,
                "dsr": decisao.dsr_por_chave.get(str(mb.hypothesis_id), {}),
            }
        )
        for mb in todos
    ]

    log.info(
        "lote.fechado",
        extra={
            "config_version_id": config_version_id,
            "procedimento": procedimento,
            "k": resultado_fdr["k"],
            "sobreviventes": len(sobreviventes),
        },
    )
    return Fechamento(
        config_version_id=config_version_id,
        familia_max=familia_max,
        testadas=len(com_p),
        sem_p_valor=sum(1 for mb in todos if mb.p_valor_ppm is None),
        fdr=resultado_fdr,
        membros=[mb.como_dict() for mb in finais],
        sobreviventes=sobreviventes,
        tentativas_globais=tentativas,
    )
