"""O caminho de PRODUCAO do monitoramento. ADR 0035.

## Na 0C ele nao tem sujeito, e isso e a resposta

§8.8 monitora *"conhecimento em uso"*. Na 0C nao ha nenhum: o Portao B rejeitou
a unica candidata que existia e a D38 decidiu que **nenhuma entra no forward**
(ADR 0034). Entao `monitorar` devolve `sem_conhecimento_em_uso`, **derivado** da
ausencia de hipotese em `conhecimento_validado`, `revalidado` ou `condicionado`
- nunca digitado, e nunca um `if` sobre a fase.

E o B3 **nao vira sujeito no lugar dela.** R79 e literal: *"o esperado vem do
pre-registro imutavel, nunca de uma media movel do proprio observado"*, e o B3
nao tem pre-registro por decisao. Inventar um alvo para ele seria escolher a
regua depois de a fase existir - a quinta pergunta do teste de escopo responde
"sim" a isso. A divergencia esta levantada no ADR 0035, com as duas leituras, e
a leitura funcional NAO esta pre-construida.

## O caminho de alarme roda na SUITE, pelo precedente do ADR 0034

    avaliar()    PURO, em `veredito.py` - recebe medicoes e limiares
    monitorar()  producao - na 0C responde `sem_conhecimento_em_uso`

O controle sintetico da suite alimenta `avaliar` diretamente, com degradacao
implantada. Ele **alarma quando a degradacao existe e nao alarma quando nao
existe**, que e o mesmo argumento do "1 promocao em 200 lotes" do Portao A: a
prova de que o monitor nao e surdo e ele **disparar quando tem de disparar**.

## Quatro invariantes que moram no BANCO, e nao aqui

| | onde |
|---|---|
| o limiar e congelado antes do primeiro tick | `UNIQUE` + dois gatilhos em `monitor_limiar` |
| o alarme e latched e sobrevive a reinicio | `UNIQUE (assunto, nivel)`, sem `UPDATE`, sem `DELETE` |
| cruzar o critico implica ter cruzado o alerta | `monitor_critico_exige_alerta` |
| o reteste usa so barras posteriores | `monitor_reteste_e_posterior` |

Este modulo nao reimplementa nenhuma delas. Reimplementar seria protecao que
vale enquanto ninguem a remove, e §8.5.1 ja disse o que acontece com essas.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from ..store import bloco_atomico
from ..validador import estados
from . import cusum, limiar as limiar_mod, veredito as veredito_mod

log = logging.getLogger(__name__)

# Os estados de §8.1 em que uma hipotese conta como CONHECIMENTO EM USO. Sao
# exatamente aqueles de que `transicao_legal` permite sair para `em_suspeita` -
# lidos de la, e nao redigitados, porque duas listas fechadas iguais em
# arquivos diferentes divergem (a licao de `paradas.py`).
ESTADOS_EM_USO: tuple[str, ...] = (
    "conhecimento_validado", "revalidado", "condicionado",
)


class JaCongelado(Exception):
    """Ja existe limiar congelado para este assunto. NAO se recalibra."""


class LimiarNaoCongelado(Exception):
    """Nao ha limiar congelado. O monitor NAO roda com limiar improvisado."""


@dataclass(frozen=True)
class LimiarCongelado:
    assunto: str
    efeito_minimo_cents: int
    horizonte_barras: int
    alvo_milicents_por_barra: int
    folga_milicents_por_barra: int
    alerta_milicents: int
    critico_milicents: int
    prob_alerta_ppm: int
    prob_critico_ppm: int
    semente: int
    algoritmo: str
    bloco: int
    repeticoes: int
    serie_hash: str
    serie_n: int
    hypothesis_id: int | None
    run_id: int | None
    run_digest: str
    congelado_em: str

    def como_limiares(self) -> veredito_mod.Limiares:
        return veredito_mod.Limiares(
            alerta_milicents=self.alerta_milicents,
            critico_milicents=self.critico_milicents,
            prob_alerta_ppm=self.prob_alerta_ppm,
            prob_critico_ppm=self.prob_critico_ppm,
        )

    def como_dict(self) -> dict:
        return {
            "assunto": self.assunto,
            "efeito_minimo_cents": self.efeito_minimo_cents,
            "horizonte_barras": self.horizonte_barras,
            "alvo_milicents_por_barra": self.alvo_milicents_por_barra,
            "folga_milicents_por_barra": self.folga_milicents_por_barra,
            "alerta_milicents": self.alerta_milicents,
            "critico_milicents": self.critico_milicents,
            "prob_alerta_ppm": self.prob_alerta_ppm,
            "prob_critico_ppm": self.prob_critico_ppm,
            "semente": self.semente,
            "algoritmo": self.algoritmo,
            "bloco": self.bloco,
            "repeticoes": self.repeticoes,
            "serie_hash": self.serie_hash,
            "serie_n": self.serie_n,
            "hypothesis_id": self.hypothesis_id,
            "run_id": self.run_id,
            "run_digest": self.run_digest,
            "congelado_em": self.congelado_em,
        }


def _agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ===========================================================================
# Congelar os limiares
# ===========================================================================


def congelar(
    conn: sqlite3.Connection,
    *,
    assunto: str,
    efeito_minimo_cents: int,
    horizonte_barras: int,
    serie_milicents: list[int],
    run_digest: str,
    hypothesis_id: int | None = None,
    run_id: int | None = None,
    horizonte_da_reserva: int | None = None,
    repeticoes: int = limiar_mod.REPETICOES,
    semente: int = limiar_mod.SEMENTE,
) -> LimiarCongelado:
    """Calibra e congela. Uma vez, e para sempre.

    `horizonte_barras` e o do **pre-registro** e alimenta o ALVO;
    `horizonte_da_reserva` e o do forward e alimenta a distribuicao do MAXIMO.
    Sao dois numeros diferentes de proposito - foi confundi-los que produziria
    o erro de unidade que o ADR 0035 documenta -, e quando o segundo nao vem,
    ele e o primeiro.
    """
    if ler(conn, assunto) is not None:
        raise JaCongelado(
            f"limiar de {assunto!r} ja esta congelado: recalibrar depois de o"
            " forward existir seria escolher o limiar olhando o numero"
        )

    alvo = cusum.alvo_por_barra(
        efeito_minimo_cents=efeito_minimo_cents,
        horizonte_barras=horizonte_barras,
    )
    k = cusum.folga(alvo)
    cal = limiar_mod.calibrar(
        serie_milicents,
        alvo_milicents_por_barra=alvo,
        folga_milicents_por_barra=k,
        horizonte_barras=horizonte_da_reserva or horizonte_barras,
        repeticoes=repeticoes,
        semente=semente,
    )

    agora = _agora()
    with bloco_atomico(conn, "monitor_congelar"):
        conn.execute(
            "INSERT INTO monitor_limiar ("
            " assunto, efeito_minimo_cents, horizonte_barras,"
            " alvo_milicents_por_barra, folga_milicents_por_barra,"
            " alerta_milicents, critico_milicents,"
            " prob_alerta_ppm, prob_critico_ppm,"
            " semente, algoritmo, bloco, repeticoes,"
            " serie_hash, serie_n, hypothesis_id, run_id, run_digest,"
            " congelado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                assunto, efeito_minimo_cents, horizonte_barras, alvo, k,
                cal.alerta_milicents, cal.critico_milicents,
                cal.prob_alerta_ppm, cal.prob_critico_ppm,
                cal.semente, cal.algoritmo, cal.bloco, cal.repeticoes,
                cal.serie_hash, cal.serie_n, hypothesis_id, run_id,
                run_digest, agora,
            ),
        )

    log.info("monitor.congelado", extra={
        "assunto": assunto, "alerta": cal.alerta_milicents,
        "critico": cal.critico_milicents, "bloco": cal.bloco,
        "surdo_por_deriva": cal.surdo_por_deriva,
    })
    congelado = ler(conn, assunto)
    assert congelado is not None
    return congelado


def ler(conn: sqlite3.Connection, assunto: str) -> LimiarCongelado | None:
    linha = conn.execute(
        "SELECT * FROM monitor_limiar WHERE assunto = ?", (assunto,)
    ).fetchone()
    if linha is None:
        return None
    return LimiarCongelado(
        assunto=str(linha["assunto"]),
        efeito_minimo_cents=int(linha["efeito_minimo_cents"]),
        horizonte_barras=int(linha["horizonte_barras"]),
        alvo_milicents_por_barra=int(linha["alvo_milicents_por_barra"]),
        folga_milicents_por_barra=int(linha["folga_milicents_por_barra"]),
        alerta_milicents=int(linha["alerta_milicents"]),
        critico_milicents=int(linha["critico_milicents"]),
        prob_alerta_ppm=int(linha["prob_alerta_ppm"]),
        prob_critico_ppm=int(linha["prob_critico_ppm"]),
        semente=int(linha["semente"]),
        algoritmo=str(linha["algoritmo"]),
        bloco=int(linha["bloco"]),
        repeticoes=int(linha["repeticoes"]),
        serie_hash=str(linha["serie_hash"]),
        serie_n=int(linha["serie_n"]),
        hypothesis_id=(
            None if linha["hypothesis_id"] is None
            else int(linha["hypothesis_id"])
        ),
        run_id=None if linha["run_id"] is None else int(linha["run_id"]),
        run_digest=str(linha["run_digest"]),
        congelado_em=str(linha["congelado_em"]),
    )


def exigir(conn: sqlite3.Connection, assunto: str) -> LimiarCongelado:
    lim = ler(conn, assunto)
    if lim is None:
        raise LimiarNaoCongelado(
            f"nao ha limiar congelado para {assunto!r}: o monitor nao roda com"
            " limiar improvisado, porque um limiar escolhido depois do"
            " primeiro tick e um limiar escolhido olhando o forward"
        )
    return lim


# ===========================================================================
# A serie acumulada
# ===========================================================================


def serie(conn: sqlite3.Connection, assunto: str) -> cusum.Serie:
    """A serie persistida, remontada de `monitor_passo`.

    Remontada e nao recalculada: `s_milicents` foi gravado quando a barra
    chegou, sob o limiar que valia. Recalcular a partir do observado seria
    refazer a conta com o alvo de HOJE - e o alvo e imutavel, mas a garantia
    nao pode depender de ele ser.
    """
    passos = [
        cusum.Passo(
            t_ms=int(r["t_ms"]),
            observado_milicents=(
                None if r["observado_milicents"] is None
                else int(r["observado_milicents"])
            ),
            d_milicents=(
                None if r["d_milicents"] is None else int(r["d_milicents"])
            ),
            s_milicents=int(r["s_milicents"]),
            atualizou=bool(r["atualizou"]),
        )
        for r in conn.execute(
            "SELECT t_ms, observado_milicents, d_milicents, s_milicents,"
            "       atualizou FROM monitor_passo"
            " WHERE assunto = ? ORDER BY t_ms",
            (assunto,),
        )
    ]

    maximo = max((p.s_milicents for p in passos), default=0)
    puladas = sum(1 for p in passos if not p.atualizou)
    maior = corrida = 0
    for p in passos:
        corrida = 0 if p.atualizou else corrida + 1
        maior = max(maior, corrida)

    return cusum.Serie(
        passos=tuple(passos), maximo_milicents=maximo,
        barras_puladas=puladas, maior_lacuna=maior,
    )


def registrar(
    conn: sqlite3.Connection,
    *,
    assunto: str,
    observados: list[tuple[int, int | None]],
    regimes: dict[int, str] | None = None,
) -> dict:
    """Avança o CUSUM sobre barras NOVAS e grava os alarmes que cruzarem.

    Continua de onde a serie persistida parou. **Nao e reset:** `s_inicial` e
    o ultimo `S` gravado, e a unica coisa que zera `S` e o piso da propria
    estatistica.

    Barra ja registrada e **ignorada**, e nao regravada: `UNIQUE (assunto,
    t_ms)` recusaria, e reprocessar a mesma barra duas vezes daria a ela peso
    dobrado no acumulado.
    """
    lim = exigir(conn, assunto)
    ja = serie(conn, assunto)
    vistos = {p.t_ms for p in ja.passos}
    novos = [(t, v) for t, v in observados if t not in vistos]

    s_inicial = ja.passos[-1].s_milicents if ja.passos else 0
    avanco = cusum.acumular(
        novos,
        alvo_milicents_por_barra=lim.alvo_milicents_por_barra,
        folga_milicents_por_barra=lim.folga_milicents_por_barra,
        s_inicial_milicents=s_inicial,
    )

    agora = _agora()
    regimes = regimes or {}
    with bloco_atomico(conn, "monitor_registrar"):
        for p in avanco.passos:
            conn.execute(
                "INSERT INTO monitor_passo ("
                " assunto, t_ms, observado_milicents, atualizou,"
                " d_milicents, s_milicents, regime, registrado_em)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    assunto, p.t_ms, p.observado_milicents,
                    1 if p.atualizou else 0, p.d_milicents, p.s_milicents,
                    regimes.get(p.t_ms), agora,
                ),
            )

    gravados = _gravar_alarmes(conn, assunto=assunto, lim=lim)
    return {
        "assunto": assunto,
        "barras_novas": len(avanco.passos),
        "atualizadas": avanco.atualizadas,
        "puladas": avanco.barras_puladas,
        "s_milicents": (
            avanco.passos[-1].s_milicents if avanco.passos else s_inicial
        ),
        "alarmes_novos": gravados,
    }


def _gravar_alarmes(
    conn: sqlite3.Connection, *, assunto: str, lim: LimiarCongelado
) -> list[str]:
    """Grava os cruzamentos que ainda nao estao registrados, na ordem dos niveis.

    Sobre a SERIE INTEIRA, e nao so sobre o avanco: o alerta pode ter sido
    cruzado numa volta anterior em que o limiar de alerta ja existia, e o latch
    e por nivel. Reavaliar o acumulado inteiro e barato e nao muda nada -
    `UNIQUE (assunto, nivel)` absorve o reenvio, do mesmo jeito que a amostra
    de BBO absorve o reenvio identico.
    """
    completa = serie(conn, assunto)
    achados = cusum.cruzamentos(
        completa, alerta=lim.alerta_milicents, critico=lim.critico_milicents,
    )
    existentes = {
        str(r["nivel"])
        for r in conn.execute(
            "SELECT nivel FROM monitor_alarme WHERE assunto = ?", (assunto,)
        )
    }

    limiar_por_nivel = {
        cusum.ALERTA: lim.alerta_milicents,
        cusum.CRITICO: lim.critico_milicents,
    }
    prob_por_nivel = {
        cusum.ALERTA: lim.prob_alerta_ppm,
        cusum.CRITICO: lim.prob_critico_ppm,
    }

    agora = _agora()
    novos: list[str] = []
    with bloco_atomico(conn, "monitor_alarmes"):
        for nivel, passo in achados:
            if nivel in existentes:
                continue
            # Puladas e maior lacuna ATE o alarme: "alarmou" sobre um periodo
            # com metade dos dados ausentes e outra afirmacao.
            ate = [p for p in completa.passos if p.t_ms <= passo.t_ms]
            puladas = sum(1 for p in ate if not p.atualizou)
            maior = corrida = 0
            for p in ate:
                corrida = 0 if p.atualizou else corrida + 1
                maior = max(maior, corrida)

            conn.execute(
                "INSERT INTO monitor_alarme ("
                " assunto, nivel, t_ms, s_milicents, limiar_milicents,"
                " barras_puladas, maior_lacuna, motivo, registrado_em)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    assunto, nivel, passo.t_ms, passo.s_milicents,
                    limiar_por_nivel[nivel], puladas, maior,
                    f"CUSUM acumulou {passo.s_milicents} milicents e cruzou o"
                    f" limiar {nivel} de {limiar_por_nivel[nivel]} na barra"
                    f" {passo.t_ms}. Orcamento de falso alarme sob ausencia de"
                    f" degradacao: {prob_por_nivel[nivel]} ppm no horizonte de"
                    f" {lim.horizonte_barras} barras, calibrado por"
                    f" {lim.algoritmo} com semente {lim.semente} sobre a serie"
                    f" {lim.serie_hash[:12]}",
                    agora,
                ),
            )
            novos.append(nivel)

    if novos:
        log.warning("monitor.alarme", extra={
            "assunto": assunto, "niveis": novos,
        })
    return novos


def alarmes(conn: sqlite3.Connection, assunto: str) -> list[dict]:
    return [
        {
            "id": int(r["id"]), "nivel": str(r["nivel"]),
            "t_ms": int(r["t_ms"]), "s_milicents": int(r["s_milicents"]),
            "limiar_milicents": int(r["limiar_milicents"]),
            "barras_puladas": int(r["barras_puladas"]),
            "maior_lacuna": int(r["maior_lacuna"]),
            "motivo": str(r["motivo"]),
            "registrado_em": str(r["registrado_em"]),
        }
        for r in conn.execute(
            "SELECT * FROM monitor_alarme WHERE assunto = ?"
            " ORDER BY t_ms, nivel",
            (assunto,),
        )
    ]


def agendar_reteste(
    conn: sqlite3.Connection, *, alarme_id: int, de_ms: int,
    ate_ms_exclusive: int,
) -> int:
    """A janela do reteste. POSTERIOR ao alarme, e o banco e quem confere.

    §8.8: ao cruzar o alerta, *"um reteste e agendado"*. Passar nele **nao
    apaga historico e nao reinicia o CUSUM** - o passo e o alarme sao
    append-only, e este registro e novo, ao lado deles.
    """
    with bloco_atomico(conn, "monitor_reteste"):
        cur = conn.execute(
            "INSERT INTO monitor_reteste (alarme_id, de_ms, ate_ms_exclusive,"
            " agendado_em) VALUES (?,?,?,?)",
            (alarme_id, de_ms, ate_ms_exclusive, _agora()),
        )
    return int(cur.lastrowid or 0)


def retestes(conn: sqlite3.Connection, assunto: str) -> list[dict]:
    return [
        {
            "id": int(r["id"]), "alarme_id": int(r["alarme_id"]),
            "nivel": str(r["nivel"]),
            "de_ms": int(r["de_ms"]),
            "ate_ms_exclusive": int(r["ate_ms_exclusive"]),
            "agendado_em": str(r["agendado_em"]),
        }
        for r in conn.execute(
            "SELECT t.*, a.nivel AS nivel FROM monitor_reteste t"
            "  JOIN monitor_alarme a ON a.id = t.alarme_id"
            " WHERE a.assunto = ? ORDER BY t.de_ms",
            (assunto,),
        )
    ]


# ===========================================================================
# O caminho de producao
# ===========================================================================


def conhecimento_em_uso(conn: sqlite3.Connection) -> list[dict]:
    """As hipoteses em estado de conhecimento EM USO. Derivado da maquina.

    Le a view `hypothesis_estado_atual`, que e derivada do log de transicoes -
    nunca uma coluna de estado. Mesmo desenho do saldo, que sai do ledger.
    """
    marcas = ",".join("?" for _ in ESTADOS_EM_USO)
    return [
        {"hypothesis_id": int(r["hypothesis_id"]), "estado": str(r["estado"])}
        for r in conn.execute(
            "SELECT hypothesis_id, estado FROM hypothesis_estado_atual"
            f" WHERE estado IN ({marcas}) ORDER BY hypothesis_id",
            ESTADOS_EM_USO,
        )
    ]


def monitorar(
    conn: sqlite3.Connection, *, duracao_barra_ms: int,
) -> veredito_mod.Veredito:
    """O estado do monitor AGORA. Na 0C, `sem_conhecimento_em_uso`.

    **A recusa nao e um `if` sobre a fase.** Ela e a consulta: sem hipotese em
    `conhecimento_validado`, `revalidado` ou `condicionado`, nao existe
    conhecimento em uso a monitorar - e §8.8 monitora conhecimento em uso.
    Quando uma candidata for validada algum dia, esta mesma funcao passa a
    medir, sem que ninguem troque um literal.

    Ha guarda de codigo conferindo que este modulo nao importa `app.fase`.
    """
    em_uso = conhecimento_em_uso(conn)
    if not em_uso:
        return veredito_mod.sem_conhecimento_em_uso(
            "nenhuma hipotese em conhecimento_validado, revalidado ou"
            " condicionado: nao ha conhecimento em uso a monitorar. Na 0C isso"
            " e o esperado - o Portao B rejeitou a unica candidata e a D38"
            " decidiu que nenhuma entra no forward (ADR 0034). O B3 nao ocupa"
            " o lugar dela: sem pre-registro nao ha alvo, e R79 proibe"
            " inventar um"
        )

    # Ha conhecimento em uso: cada um tem assunto proprio, e o assunto e a
    # hipotese. Um veredito por assunto, e o pior deles e o do monitor - a
    # mesma logica do portao, em que uma condicao falsa decide.
    piores: list[veredito_mod.Veredito] = []
    for h in em_uso:
        assunto = assunto_da_hipotese(h["hypothesis_id"])
        lim = ler(conn, assunto)
        if lim is None:
            piores.append(veredito_mod.Veredito(
                estado=veredito_mod.INDISPONIVEL,
                motivo=(
                    f"hipotese {h['hypothesis_id']} esta em {h['estado']} e"
                    f" nao tem limiar congelado: o monitor nao roda com limiar"
                    " improvisado"
                ),
                mediu=False, cruzou_alerta=False, cruzou_critico=False,
                cobertura_ppm=0, barras_puladas=0, maior_lacuna=0,
                barras_avaliadas=0, maximo_milicents=0,
            ))
            continue
        piores.append(veredito_mod.avaliar(
            serie(conn, assunto), lim.como_limiares(),
            duracao_barra_ms=duracao_barra_ms,
        ))

    ordem = {
        veredito_mod.INVALIDADO: 0, veredito_mod.EM_SUSPEITA: 1,
        veredito_mod.INDISPONIVEL: 2, veredito_mod.SEM_ALARME: 3,
        veredito_mod.SEM_CONHECIMENTO: 4,
    }
    return sorted(piores, key=lambda v: ordem[v.estado])[0]


def assunto_da_hipotese(hypothesis_id: int) -> str:
    """O nome do assunto de uma hipotese. Uma definicao, num lugar so."""
    return f"hipotese:{hypothesis_id}"


def transitar_por_alarme(
    conn: sqlite3.Connection, *, hypothesis_id: int, nivel: str,
    motivo: str, alarme_id: int,
) -> int:
    """Move a hipotese na maquina de §8.1, pelo alarme.

    `alerta` -> `em_suspeita`; `critico` -> `invalidado`. Os tres gatilhos do
    incremento 10 continuam valendo: a transicao parte do estado ATUAL lido do
    banco, o par tem de existir em `transicao_legal`, e nada entra fora de
    `hipotese_registrada`. **Nenhum estado pode ser pulado**, e nao ha nada a
    acrescentar aqui para isso valer.

    R81: o motivo vai na evidencia, e a transicao e imutavel por gatilho.
    """
    para = (
        veredito_mod.EM_SUSPEITA if nivel == cusum.ALERTA
        else veredito_mod.INVALIDADO
    )
    return estados.transitar(
        conn, hypothesis_id, para=para,
        evidencia={
            "fonte": "monitoramento_continuo",
            "nivel": nivel,
            "alarme_id": alarme_id,
            "motivo": motivo,
        },
    )
