"""O manifesto do piloto: DERIVADO da fonte imutavel, e NUNCA gravado.

**A distincao importa, e o vocabulario e parte da garantia.** O que e GRAVADO
e a LINHA DE FECHAMENTO em `janela_piloto` - sete campos, uma vez so. Todo o
resto deste arquivo e DERIVADO de `bbo_amostra` dentro do intervalo fechado.
Dizer "manifesto gravado" seria falso, e o campo `o_que_e_gravado` da resposta
existe para que ninguem precise abrir este arquivo para saber disso.

## Por que derivar BASTA aqui, e nao e conveniencia

A fonte e congelada pelo proprio banco, e nao por disciplina:

| garantia | onde ela mora |
|---|---|
| um instante, uma linha | `PRIMARY KEY (venue, symbol, t_grid_ms, contrato)` |
| ninguem reescreve | gatilho `bbo_amostra_sem_update` |
| ninguem apaga | gatilho `bbo_amostra_sem_delete` |
| nada fora da grade | `CHECK (t_grid_ms % grade_ms = 0)` |
| a fronteira nao se move | `UNIQUE` + gatilhos de `janela_piloto` |

Com a fronteira imutavel e TODOS os instantes da grade ja gravados, o conjunto
que o manifesto descreve **nao pode mudar** - entao o mesmo manifesto sai
identico hoje e daqui a um ano, e o hash e a testemunha disso.

A unica porta que restaria e a **insercao tardia** de um instante que ainda nao
existisse: um `INSERT` e permitido onde nao ha linha. Ela e fechada ANTES do
fechamento, pela exigencia de grade completa - `GradeIncompleta`. Depois do
fechamento nao ha instante ausente para chegar.

## Por que nao gravar

Gravar estes campos exigiria coluna ou tabela nova, e `alvo.hash_do_schema` le
o `sqlite_master` inteiro: qualquer criacao mudaria o alvo de certificacao e
obrigaria a recertificar - com a OP-3 bloqueando. A alternativa A foi a escolha
do usuario em 2026-09-21, com a exigencia de grade completa como condicao.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

from ..aovivo.bbo import Serie
from . import piloto

#: A versao do FORMATO entra no hash. Sem ela, mudar a serializacao um dia
#: produziria outro numero sobre o mesmo dado, e os dois pareceriam divergencia
#: do dado em vez de mudanca de regua.
FORMATO = "piloto-manifesto@1"

#: Os campos de `bbo_amostra` que o PILOTO usa, e portanto os unicos que entram
#: no hash. Preco, quantidade e metadados de entrega sao materiais para a
#: CALIBRACAO, e nao para as duas travas; inclui-los faria o hash do piloto
#: mudar de significado sem que o piloto mudasse.
CAMPOS_MATERIAIS = ("t_grid_ms", "disponivel", "motivo")


class GradeIncompleta(Exception):
    """Falta instante da grade dentro da janela.

    Enquanto faltar, uma insercao tardia ainda pode mudar o conjunto - e um
    manifesto derivado deixaria de ser reproduzivel. Recusar e o que torna a
    derivacao equivalente a gravacao.
    """


class ManifestoDivergente(Exception):
    """O que foi GRAVADO discorda do que a fonte deriva AGORA.

    Nao ha leitura benigna para isto: ou a linha de fechamento foi forjada, ou
    a fonte mudou sob gatilhos que proibem mudar. Erro alto, e nunca um numero
    publicado ao lado de uma ressalva.
    """


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def grade_do_contrato(conn: sqlite3.Connection, contrato: str) -> int:
    linha = conn.execute(
        "SELECT grade_ms FROM bbo_contrato WHERE contrato = ?", (contrato,)
    ).fetchone()
    if linha is None:
        raise ValueError(f"contrato {contrato!r} nao existe")
    return int(linha["grade_ms"])


def _hash_do_intervalo(cabecalho: list[str], na_grade: list[dict]) -> str:
    """sha256 sobre uma serializacao CANONICA, e ela esta escrita aqui.

    Ordem: `t_grid_ms` ascendente, que e a ordem do `ORDER BY` da consulta e a
    unica ordem total que o instante admite. Uma linha por instante:

        <t_grid_ms>|<disponivel>|<motivo ou vazio>\\n

    O cabecalho vai antes, um campo por linha terminada em NUL, para que dois
    intervalos diferentes nunca produzam a mesma sequencia de bytes.
    """
    h = hashlib.sha256()
    for parte in cabecalho:
        h.update(parte.encode("utf-8"))
        h.update(b"\x00")
    for linha in na_grade:
        h.update(
            f"{linha['t_grid_ms']}|{linha['disponivel']}|{linha['motivo'] or ''}"
            .encode("utf-8")
        )
        h.update(b"\n")
    return h.hexdigest()


def derivar(
    conn: sqlite3.Connection,
    *,
    serie: Serie,
    contrato: str,
    de_ms: int,
    ate_ms_exclusive: int,
    grade_ms: int,
) -> dict:
    """O manifesto do intervalo `[de_ms, ate_ms_exclusive)`. NAO grava nada.

    Nada de fora do intervalo entra na contagem nem no hash, e nada que esteja
    fora da grade entra em coisa nenhuma - ele e contado a parte, porque uma
    linha desalinhada e um fato sobre o dado e nao um detalhe a ignorar.
    """
    esperadas = (ate_ms_exclusive - de_ms) // grade_ms
    linhas = [
        {
            "t_grid_ms": int(r["t_grid_ms"]),
            "disponivel": int(r["disponivel"]),
            "motivo": r["motivo"],
        }
        for r in conn.execute(
            "SELECT t_grid_ms, disponivel, motivo FROM bbo_amostra"
            " WHERE venue = ? AND symbol = ? AND contrato = ?"
            "   AND t_grid_ms >= ? AND t_grid_ms < ?"
            " ORDER BY t_grid_ms",
            (serie.venue, serie.symbol, contrato, de_ms, ate_ms_exclusive),
        )
    ]
    na_grade = [l for l in linhas if (l["t_grid_ms"] - de_ms) % grade_ms == 0]
    fora_da_grade = len(linhas) - len(na_grade)

    validas = [l for l in na_grade if l["disponivel"] == 1]
    indisponiveis = [l for l in na_grade if l["disponivel"] == 0]
    por_motivo: dict[str, int] = {}
    for l in indisponiveis:
        chave = l["motivo"] or "sem_motivo"
        por_motivo[chave] = por_motivo.get(chave, 0) + 1

    # As lacunas sao corridas de instantes CONSECUTIVOS sem validade. Contar
    # instantes soltos como "uma lacuna cada" e correto e util: o tamanho da
    # maior e o que diz se houve queda ou perda pontual.
    lacunas: list[dict] = []
    for l in na_grade:
        if l["disponivel"] == 1:
            continue
        atual = lacunas[-1] if lacunas else None
        if atual is not None and l["t_grid_ms"] == atual["ate_ms_exclusive"]:
            atual["ate_ms_exclusive"] += grade_ms
            atual["instantes"] += 1
        else:
            lacunas.append({
                "de_ms": l["t_grid_ms"],
                "ate_ms_exclusive": l["t_grid_ms"] + grade_ms,
                "instantes": 1,
            })
    maior = max(lacunas, key=lambda x: x["instantes"], default=None)

    milesima = (
        validas[piloto.OBSERVACOES_MINIMAS - 1]["t_grid_ms"]
        if len(validas) >= piloto.OBSERVACOES_MINIMAS
        else None
    )
    dias_x1000 = round((ate_ms_exclusive - de_ms) * 1000 / piloto.MS_POR_DIA)
    travas = {
        "dias": {
            "previstos": piloto.DIAS_MINIMOS,
            "corridos_x1000": dias_x1000,
            "alcancada": dias_x1000 >= piloto.DIAS_MINIMOS * 1000,
        },
        "observacoes": {
            "minimas": piloto.OBSERVACOES_MINIMAS,
            "validas_na_janela": len(validas),
            "alcancada": len(validas) >= piloto.OBSERVACOES_MINIMAS,
        },
    }
    travas["as_duas_alcancadas"] = bool(
        travas["dias"]["alcancada"] and travas["observacoes"]["alcancada"]
    )

    cabecalho = [
        FORMATO, contrato, serie.venue, serie.symbol,
        str(de_ms), str(ate_ms_exclusive), str(grade_ms), str(esperadas),
    ]
    return {
        "formato": FORMATO,
        "serie": {
            "venue": serie.venue, "symbol": serie.symbol,
            "contrato": contrato, "grade_ms": grade_ms,
        },
        "janela": {
            "de_ms": de_ms, "de": _iso(de_ms),
            "ate_ms_exclusive": ate_ms_exclusive,
            "ate_exclusive": _iso(ate_ms_exclusive),
            "dias_corridos_x1000": dias_x1000,
        },
        "grade": {
            "esperadas": esperadas,
            "recebidas": len(na_grade),
            "ausentes": esperadas - len(na_grade),
            "fora_da_grade": fora_da_grade,
            "completa": len(na_grade) == esperadas and fora_da_grade == 0,
            "unicidade_por_instante": (
                "estrutural: PRIMARY KEY (venue, symbol, t_grid_ms, contrato)"
            ),
            "imutabilidade": (
                "estrutural: gatilhos bbo_amostra_sem_update e"
                " bbo_amostra_sem_delete"
            ),
            "alinhamento": (
                "estrutural: CHECK (t_grid_ms % grade_ms = 0); aqui tambem se"
                " confere o alinhamento CONTRA a fronteira da janela"
            ),
        },
        "observacoes": {
            "validas": len(validas),
            "indisponiveis": len(indisponiveis),
            "por_motivo": dict(sorted(por_motivo.items())),
            "instante_da_milesima_ms": milesima,
            "instante_da_milesima": _iso(milesima) if milesima else None,
        },
        "lacunas": {
            "quantas": len(lacunas),
            "instantes_sem_validade": len(indisponiveis),
            "maior": (
                None if maior is None else {
                    **maior,
                    "de": _iso(maior["de_ms"]),
                    "ate_exclusive": _iso(maior["ate_ms_exclusive"]),
                    "duracao_ms": maior["instantes"] * grade_ms,
                }
            ),
            "o_que_e": (
                "corrida de instantes CONSECUTIVOS da grade sem observacao"
                " valida, dentro da janela"
            ),
        },
        "travas": travas,
        "hash_do_intervalo": _hash_do_intervalo(cabecalho, na_grade),
        "o_que_o_hash_cobre": {
            "formato": FORMATO,
            "cabecalho": [
                "formato", "contrato", "venue", "symbol", "de_ms",
                "ate_ms_exclusive", "grade_ms", "esperadas",
            ],
            "por_instante": list(CAMPOS_MATERIAIS),
            "ordem": "t_grid_ms ascendente",
            "serializacao": "<t_grid_ms>|<disponivel>|<motivo ou vazio> por linha",
            "so_dentro_do_intervalo": True,
        },
        "o_que_fica_de_fora_do_hash": [
            "o horario da requisicao e qualquer campo de leitura",
            "`recebido_em` e os carimbos de entrega: dizem como o dado chegou,"
            " e nao o que o piloto contou",
            "preco, quantidade e relogio: materiais para a CALIBRACAO, nao"
            " para as duas travas",
            "linhas fora do intervalo fechado, e linhas fora da grade",
        ],
        "o_que_e_gravado": (
            "apenas a linha de fechamento em `janela_piloto`: de_ms,"
            " ate_ms_exclusive, observacoes_validas, observacoes_totais,"
            " dias_corridos_x1000, fechada_por e criado_em"
        ),
        "o_que_e_derivado": (
            "todo o resto deste manifesto, de `bbo_amostra` - que e por"
            " acrescimo, com um instante por linha e alinhamento imposto pelo"
            " banco. Por isso ele sai igual a cada leitura"
        ),
    }


def _campos(j: piloto.Janela) -> dict:
    return {
        "de_ms": j.de_ms,
        "ate_ms_exclusive": j.ate_ms_exclusive,
        "observacoes_validas": j.observacoes_validas,
        "observacoes_totais": j.observacoes_totais,
        "dias_corridos_x1000": j.dias_corridos_x1000,
        "fechada_por": j.fechada_por,
    }


def _conferir(
    gravada: piloto.Janela, rederivada: piloto.Janela, m: dict
) -> None:
    """Tres leituras do mesmo fato, e elas tem de coincidir.

    | | o que e |
    |---|---|
    | `gravada` | a linha de `janela_piloto`, escrita uma vez |
    | `rederivada` | a janela que o REGISTRO produz **agora**, pelas duas travas |
    | `m` | as contagens **recontadas** sobre o intervalo gravado |

    A segunda existe porque comparar a linha gravada com um manifesto montado
    a partir dela seria **tautologia**: o intervalo entraria de um lado e sairia
    do outro. `piloto.derivar` nao sabe o que foi gravado - ele recomeca da
    primeira valida e das duas travas -, e por isso ele e capaz de discordar.
    E ele nao pode mudar depois do fechamento: `de_ms`, o fim por dias e o
    instante da milesima ja estao congelados na fonte.

    A terceira existe porque `m` conta por uma consulta **diferente** da que
    `piloto.derivar` usa, e duas contagens do mesmo numero em modulos
    diferentes e a forma do defeito que este projeto ja pagou caro - o
    `n_efetivo` do agente contra o do validador, no run 30.
    """
    armazenado, derivado = _campos(gravada), _campos(rederivada)
    recontado = {
        "observacoes_validas": m["observacoes"]["validas"],
        "observacoes_totais": m["grade"]["recebidas"],
    }
    # As chaves levam de QUEM veio a discordancia. Sem isso, um campo que
    # divergisse das DUAS leituras teria a primeira divergencia sobrescrita
    # pela segunda - perder metade do diagnostico dentro da funcao que existe
    # para diagnosticar.
    diferentes = {
        f"{k} (gravado x rederivado)": {
            "armazenado": armazenado[k], "derivado": derivado[k],
        }
        for k in armazenado
        if armazenado[k] != derivado[k]
    }
    diferentes.update({
        f"{k} (gravado x recontado)": {
            "armazenado": armazenado[k], "recontado": recontado[k],
        }
        for k in recontado
        if armazenado[k] != recontado[k]
    })
    if diferentes:
        raise ManifestoDivergente(
            "a linha de fechamento discorda da fonte imutavel: "
            f"{diferentes}"
        )


def do_registro(
    conn: sqlite3.Connection, *, serie: Serie, contrato: str
) -> dict:
    """O que a leitura publica: fechado com manifesto, ou a previa e o motivo.

    **Esta e a UNICA montagem da resposta**, e o POST devolve exatamente o que
    ela devolve. Duas montagens seriam duas verdades sobre o mesmo fechamento,
    e a exigencia de "POST e GET byte a byte" viraria disciplina.
    """
    grade_ms = grade_do_contrato(conn, contrato)
    gravada = piloto.ler(conn, serie, contrato)
    if gravada is None:
        try:
            prevista = piloto.derivar(conn, serie, contrato)
        except piloto.PilotoNaoFechaAinda as e:
            return {
                "fechado": False,
                "manifesto": None,
                "por_que": str(e),
            }
        previa = derivar(
            conn, serie=serie, contrato=contrato, de_ms=prevista.de_ms,
            ate_ms_exclusive=prevista.ate_ms_exclusive, grade_ms=grade_ms,
        )
        return {
            "fechado": False,
            # Os SETE campos que o fechamento gravaria, e nao seis. A previa
            # dizia "descreve a janela que o fechamento gravaria" e omitia
            # `fechada_por` - a trava que vence -, que e justamente o campo
            # que nao da para ler do manifesto. Deixa-lo de fora obrigaria
            # quem le a DEDUZIR a trava de `dias_corridos_x1000`, e uma
            # deducao correta hoje e a forma como um numero para de descrever.
            "seria_gravado": {
                "de_ms": prevista.de_ms,
                "ate_ms_exclusive": prevista.ate_ms_exclusive,
                "observacoes_validas": prevista.observacoes_validas,
                "observacoes_totais": prevista.observacoes_totais,
                "dias_corridos_x1000": prevista.dias_corridos_x1000,
                "fechada_por": prevista.fechada_por,
                "fechada_por_e": (
                "a TRAVA que venceu - `dias` ou `observacoes` -, e nunca uma"
                " pessoa. O fechamento nao tem autor porque nao tem escolha:"
                " a janela sai do registro, e o pedido nao carrega decisao"
                " nenhuma para atribuir a alguem"
                ),
            },
            "manifesto": previa,
            "por_que": (
                "a janela ainda NAO foi gravada: este manifesto descreve a"
                " janela que o fechamento gravaria, e `seria_gravado` traz os"
                " sete campos da linha - mas nada foi escrito"
            ),
        }

    m = derivar(
        conn, serie=serie, contrato=contrato, de_ms=gravada.de_ms,
        ate_ms_exclusive=gravada.ate_ms_exclusive, grade_ms=grade_ms,
    )
    try:
        rederivada = piloto.derivar(conn, serie, contrato)
    except piloto.PilotoNaoFechaAinda as e:
        # Depois de um fechamento legitimo isto e impossivel: o dado so
        # cresce, e as duas travas ja cairam. Se acontecer, o registro deixou
        # de produzir a janela que ele afirma ter fechado - divergencia, e nao
        # "ainda nao".
        raise ManifestoDivergente(
            "ha janela fechada gravada e o registro nao consegue mais"
            f" deriva-la: {e}"
        ) from e
    _conferir(gravada, rederivada, m)
    linha = conn.execute(
        "SELECT criado_em FROM janela_piloto"
        " WHERE contrato = ? AND venue = ? AND symbol = ?",
        (contrato, serie.venue, serie.symbol),
    ).fetchone()
    return {
        "fechado": True,
        "gravado": {
            "de_ms": gravada.de_ms,
            "ate_ms_exclusive": gravada.ate_ms_exclusive,
            "observacoes_validas": gravada.observacoes_validas,
            "observacoes_totais": gravada.observacoes_totais,
            "dias_corridos_x1000": gravada.dias_corridos_x1000,
            "fechada_por": gravada.fechada_por,
            "fechada_por_e": (
                "a TRAVA que venceu - `dias` ou `observacoes` -, e nunca uma"
                " pessoa. O fechamento nao tem autor porque nao tem escolha:"
                " a janela sai do registro, e o pedido nao carrega decisao"
                " nenhuma para atribuir a alguem"
            ),
            "criado_em": linha["criado_em"] if linha else None,
        },
        "manifesto": m,
    }


def fechar(conn: sqlite3.Connection, *, serie: Serie, contrato: str) -> dict:
    """Grava a linha de fechamento, e so ela. Idempotente, e seguro na corrida.

    ## `BEGIN IMMEDIATE`, e por que nao `bloco_atomico` aqui

    `bloco_atomico` abre um SAVEPOINT, e um SAVEPOINT em autocommit inicia uma
    transacao **DEFERRED**: o lock de escrita so e tomado no `INSERT`. Duas
    chamadas concorrentes entao leem "nao ha linha", as duas seguem, e a
    segunda descobre tarde demais - com `IntegrityError` se chegar depois do
    commit da primeira, ou com `database is locked` se chegar durante, porque
    o SQLite **nao espera** para promover uma transacao de leitura a escrita:
    esperar ali seria deadlock, entao ele recusa na hora e o `busy_timeout`
    nao ajuda.

    **Medido antes de corrigir**, com duas conexoes ao mesmo arquivo e uma
    barreira: a perdedora subia `OperationalError: database is locked`, e a
    rota devolvia 500. Uma linha so - a unicidade segurou -, mas com erro para
    quem chamou, que e exatamente o "erro parcial" que nao pode existir.

    Com `BEGIN IMMEDIATE` o lock e tomado **antes** de qualquer leitura, o
    `busy_timeout` passa a valer, e a perdedora espera, entra, **ve a linha** e
    devolve o mesmo fechamento com `criado_agora = False`. A decisao "ja
    existe?" e o `INSERT` passam a ser o mesmo ato.

    ## A ordem, e a escrita unica

    Dentro do lock: a janela e derivada do registro, as duas travas sao
    reconferidas, a grade e exigida completa, a linha e gravada, e o gravado e
    conferido contra o derivado. Qualquer recusa desfaz tudo - e, como a
    escrita e **UMA**, nao existe fechamento pela metade para alguem ler.

    **Nao dispara estimativa, calibracao nem revalidacao.** Elas pedem a janela
    fechada; fecha-las aqui juntaria dois atos que o ADR 0027 mantem separados.
    """
    if conn.in_transaction:
        # `BEGIN` aninhado falha com "cannot start a transaction within a
        # transaction". Fechar o piloto e ato de topo - uma rota -, e dizer
        # isso alto e melhor que cair com a mensagem do SQLite, que manda
        # procurar no lugar errado.
        raise RuntimeError(
            "fechar o piloto abre a propria transacao IMMEDIATE e nao pode"
            " rodar dentro de outra ja aberta"
        )

    def resposta(criado_agora: bool) -> dict:
        return {
            "criado_agora": criado_agora,
            **do_registro(conn, serie=serie, contrato=contrato),
        }

    grade_ms = grade_do_contrato(conn, contrato)
    # O lock de escrita ANTES da primeira leitura. E ele que serializa as duas
    # chamadas; a `UNIQUE (contrato, venue, symbol)` continua sendo a garantia
    # de ultima instancia, e nao o mecanismo.
    conn.execute("BEGIN IMMEDIATE")
    try:
        if piloto.ler(conn, serie, contrato) is not None:
            conn.execute("COMMIT")
            return resposta(False)

        # A janela e DERIVADA do registro: nada do pedido. Recusa aqui se as
        # travas nao cairam, ou se o dado ainda nao alcanca o fim da janela.
        prevista = piloto.derivar(conn, serie, contrato)
        m = derivar(
            conn, serie=serie, contrato=contrato, de_ms=prevista.de_ms,
            ate_ms_exclusive=prevista.ate_ms_exclusive, grade_ms=grade_ms,
        )
        if not m["grade"]["completa"]:
            raise GradeIncompleta(
                f"a grade da janela tem {m['grade']['esperadas']} instantes"
                f" esperados e {m['grade']['recebidas']} gravados"
                f" ({m['grade']['ausentes']} ausentes,"
                f" {m['grade']['fora_da_grade']} fora da grade). Fechar agora"
                " deixaria o manifesto sujeito a uma insercao tardia"
            )
        if not m["travas"]["as_duas_alcancadas"]:
            raise piloto.PilotoNaoFechaAinda(
                "as duas travas reconferidas dentro da transacao nao estao"
                f" alcancadas: {m['travas']}"
            )
        piloto.fechar(conn, serie, contrato)
        gravada = piloto.ler(conn, serie, contrato)
        assert gravada is not None  # gravada agora, na mesma transacao
        _conferir(gravada, prevista, m)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return resposta(True)
