"""O checkpoint auditável: tudo que descreve o estado, num documento só.

> *"gere um checkpoint/export auditável contendo alvo, manifestos, componentes
> do hash, contadores, integridade, versões e commit."* — o usuário, 2026-09-10

## O que o torna AUDITÁVEL, e não só grande

Um export que apenas despeja o banco não é auditável: quem o lê não sabe o que
podia ter mudado sem ele notar. Este documento carrega, junto de cada número, a
**procedência** dele — de qual `config_version`, de qual alvo, sob qual
manifesto, com qual commit.

E ele traz o **hash do próprio conteúdo**, calculado sobre a serialização
determinística. Dois checkpoints com o mesmo `checkpoint_hash` descrevem o mesmo
estado; um diferente diz **onde** difere, porque os componentes vão publicados
ao lado.

## O que ele NÃO é

**Não é backup, e não é fonte de verdade.** O banco continua sendo. Este
documento é uma fotografia com procedência — se ele divergir do banco, o banco
está certo e o checkpoint está velho.

**E ele não calcula nada novo.** Cada bloco vem do módulo que já é dono do
número: a integridade de `relatorio.integridade`, os contadores de
`validador.contador`, o alvo de `certificacao.alvo`. Recalcular aqui criaria a
segunda fonte que a regra 16 proíbe.

**Este módulo está FORA do fechamento transitivo do alvo.** Conferido: o
fechamento tem `app.relatorio` e `app.relatorio.portao_a`, e nada mais de
`relatorio`. Gerar checkpoint não invalida certificado.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from typing import Any

#: Os blocos que o `checkpoint_hash` cobre. **Lista positiva**, e a ordem nao
#: importa porque `_estavel` ordena as chaves.
#:
#: O que entra e o ESTADO REGISTRADO: o que esta gravado no banco e nao muda
#: sozinho. O que fica de fora e o que depende do relogio de leitura - e o
#: documento diz qual e qual, em `o_que_o_hash_cobre`.
COBERTO_PELO_HASH = (
    "versoes",
    "alvo_de_certificacao",
    "composicao_do_portao_a",
    "manifestos",
    "quantos_manifestos",
    "contadores",
    "integridade",
    "lote_historico",
)


def _commit() -> dict[str, Any]:
    """O commit, e o que fazer quando ele não existe.

    Na imagem da Railway não há `.git` — o build copia `app/`,
    `requirements.txt`, `pytest.ini` e `start-backend.sh`. Então `None` aqui é
    **esperado em produção**, e o campo diz isso em vez de sumir: um checkpoint
    sem a linha do commit parece um checkpoint que esqueceu de olhar.
    """
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        sha = r.stdout.strip() or None
    except Exception:  # noqa: BLE001 - rastreamento nao derruba checkpoint
        sha = None
    return {
        "sha": sha,
        "por_que_pode_faltar": (
            None
            if sha
            else (
                "a imagem nao contem `.git`: o build copia `app/`,"
                " `requirements.txt`, `pytest.ini` e `start-backend.sh`. Use o"
                " campo `build` de `/api/substrato/health`, que vem da"
                " variavel de deploy"
            )
        ),
    }


def _estavel(valor: Any) -> str:
    """Serialização determinística — a mesma regra do alvo.

    `sort_keys` não basta: ele ordena chaves e **não** ordena listas, e uma
    lista que chegasse em outra ordem produziria outro hash sobre o mesmo
    estado. Listas de string são ordenadas; as demais preservam a ordem, que
    ali é informação.
    """

    def normalizar(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: normalizar(v[k]) for k in sorted(v)}
        if isinstance(v, (list, tuple)):
            itens = [normalizar(x) for x in v]
            if all(isinstance(x, str) for x in itens):
                return sorted(itens)
            return itens
        return v

    return json.dumps(
        normalizar(valor), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str,
    )


def _creditos(conn: sqlite3.Connection) -> dict[str, Any]:
    """As TRES grandezas de credito, cada uma no seu campo, e o que cada uma e.

    > "Nao deixe 'lancamentos' substituir silenciosamente o antigo contador de
    > creditos." - o usuario, 2026-09-10

    Foi o que eu fiz no relatorio da janela de manutencao: publiquei 33
    "lancamentos" na linha onde antes estavam 65 "creditos". Sao grandezas
    diferentes - um reteste e UMA linha e custa TRES creditos -, e trocar uma
    pela outra sob o mesmo rotulo e a forma exata do padrao que este projeto
    conta.

    Derivado de creditos.calibracao, o dono do numero: nenhum SQL novo aqui. A
    calibracao agrupa test_credit_entry por tipo com COUNT e SUM, e lista uma
    linha por orcamento - entao as somas abaixo sao exatas, e nao estimativas.
    Este modulo esta FORA dos 71 do alvo de certificacao, e mexer nele nao
    invalida os cinco escopos.
    """
    from .. import creditos as creditos_mod

    cal = creditos_mod.calibracao(conn)
    por_tipo = cal.get("por_tipo", [])
    por_braco = cal.get("por_braco", [])
    total = sum(int(t["creditos"]) for t in por_tipo)
    return {
        "credit_entry_rows": sum(int(t["testes"]) for t in por_tipo),
        "creditos_consumidos_total": total,
        "orcamentos_de_credito": len(por_braco),
        # A conferencia cruzada: o consumido por orcamento soma o total.
        "consumo_confere_com_os_orcamentos": (
            sum(int(b["consumido"]) for b in por_braco) == total
        ),
        "o_que_cada_um_e": {
            "credit_entry_rows": (
                "linhas de test_credit_entry: um TESTE cobrado por linha. NAO e"
                " quantidade de credito - um reteste e uma linha e custa 3"
            ),
            "creditos_consumidos_total": (
                "a soma dos creditos cobrados, com os pesos de 8.6.1 (in-sample"
                " 1, reteste 3). E o contador de creditos dos relatorios"
                " anteriores"
            ),
            "orcamentos_de_credito": (
                "linhas de test_credit_budget: um orcamento concedido por"
                " (braco, config_version). NAO e saldo nem consumo"
            ),
        },
    }


def montar(conn: sqlite3.Connection, *, potencia_ppm: int) -> dict[str, Any]:
    """O checkpoint inteiro, com o hash do próprio conteúdo no fim."""
    from ..certificacao import alvo as alvo_mod
    from ..certificacao import escopos as escopos_mod
    from ..config import service as config_service
    from ..dataset import loader as dataset_loader
    from ..store import versao_schema
    from ..validador import contador as validador_contador
    from ..validador import lote_congelado

    from . import fase_0c as relatorio_fase_0c
    from . import integridade as relatorio_integridade
    from . import piloto_estado

    gerado_em = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    atual = config_service.versao_atual(conn)
    meta = dataset_loader.dataset_vigente(conn)

    # ------------------------------------------------------------ o ALVO
    alvo_bloco: dict[str, Any]
    manifestos: list[dict[str, Any]] = []
    composicao: dict[str, Any] | None = None
    try:
        o_alvo = alvo_mod.montar(
            conn, dataset_hash=getattr(meta, "sha256", None) if meta else None
        )
        alvo_bloco = o_alvo
        composicao = escopos_mod.composicao(
            conn, o_alvo["alvo_de_certificacao_hash"]
        )
        for linha in conn.execute(
            "SELECT e.id, e.escopo, e.alvo_hash, e.iniciada_em,"
            "       e.build_do_backend, e.micros_para_copiar,"
            "       e.micros_da_suite, e.bytes_da_copia, e.blocos_esperados,"
            "       m.manifesto_json, m.selado_em, m.passa"
            "  FROM certificacao_execucao e"
            "  JOIN certificacao_manifesto m ON m.execucao_id = e.id"
            " WHERE m.alvo_hash = ? ORDER BY e.escopo",
            (o_alvo["alvo_de_certificacao_hash"],),
        ):
            manifestos.append(
                {
                    "execucao_id": int(linha["id"]),
                    "escopo": linha["escopo"],
                    "passa": bool(linha["passa"]),
                    "selado_em": linha["selado_em"],
                    "iniciada_em": linha["iniciada_em"],
                    "build_do_backend": linha["build_do_backend"],
                    "blocos_esperados": int(linha["blocos_esperados"]),
                    "custo_operacional": {
                        "micros_para_copiar": int(linha["micros_para_copiar"]),
                        "micros_da_suite": int(linha["micros_da_suite"]),
                        "bytes_da_copia": int(linha["bytes_da_copia"]),
                    },
                    "manifesto": json.loads(linha["manifesto_json"]),
                }
            )
    except ValueError as erro:
        alvo_bloco = {"disponivel": False, "por_que": str(erro)}

    lote_id = lote_congelado.do_lote(conn)

    documento: dict[str, Any] = {
        "checkpoint": "0C",
        "gerado_em": gerado_em,
        "o_que_isto_e": (
            "uma fotografia COM PROCEDENCIA do estado, para auditoria. NAO e"
            " backup e NAO e fonte de verdade: se divergir do banco, o banco"
            " esta certo e este documento esta velho"
        ),
        # ------------------------------------------------------- versoes
        "versoes": {
            "schema": versao_schema(conn),
            "config_version_vigente": atual.id if atual else None,
            "config_hash": atual.config_hash if atual else None,
            "dataset_id": meta.id if meta else None,
            "dataset_sha256": getattr(meta, "sha256", None) if meta else None,
            "commit": _commit(),
        },
        # ---------------------------------- alvo e componentes do hash
        "alvo_de_certificacao": alvo_bloco,
        "composicao_do_portao_a": composicao,
        "manifestos": manifestos,
        "quantos_manifestos": len(manifestos),
        # ----------------------------------------------------- contadores
        "contadores": {
            **validador_contador.resumo(conn),
            "creditos": _creditos(conn),
            "o_que_eles_provam": (
                "que a certificacao nao tocou o experimento: se algum destes"
                " tiver andado entre dois checkpoints sem uma hipotese ter"
                " sido registrada de proposito, alguma certificacao escreveu"
                " onde nao devia"
            ),
        },
        # ---------------------------------------------------- integridade
        "integridade": relatorio_integridade.montar(conn),
        # --------------------------------------------------------- lote
        "lote_historico": {
            "config_version_id": lote_id,
            "hipoteses_no_lote": (
                lote_congelado.quantas_no_lote(conn, lote_id) if lote_id else 0
            ),
            "relacao_com_a_vigente": (
                lote_congelado.equivalencia(
                    conn, lote_id=lote_id, vigente_id=atual.id
                )
                if lote_id is not None and atual is not None
                else None
            ),
        },
        # -------------------------------------------------------- piloto
        "piloto": piloto_estado.montar(conn),
        "regimes_observados": piloto_estado.regimes_observados(conn),
        # ----------------------------------------------- a resposta da fase
        "fase_0c": relatorio_fase_0c.montar(conn, potencia_ppm=potencia_ppm),
    }

    # ------------------------------------------------------------- o hash
    #
    # Sobre uma lista POSITIVA de blocos, e nao sobre o documento menos alguns.
    #
    # A lista negativa foi tentada primeiro e nao funcionou: alem do
    # `gerado_em` do topo, ha o de `fase_0c`, o de `integridade`, e blocos
    # inteiros que dependem do relogio de LEITURA - dias decorridos, taxa das
    # ultimas 24h, projecao de fechamento. Dois checkpoints do mesmo estado
    # saiam com hashes diferentes, e o campo virava identificador de
    # requisicao com nome de identificador de estado.
    #
    # Positiva tambem falha melhor: um bloco novo fica FORA do hash ate alguem
    # decidir inclui-lo, em vez de entrar calado e tornar o hash volatil.
    documento["checkpoint_hash"] = hashlib.sha256(
        _estavel({k: documento[k] for k in COBERTO_PELO_HASH}).encode("utf-8")
    ).hexdigest()
    documento["o_que_o_hash_cobre"] = {
        "blocos": list(COBERTO_PELO_HASH),
        "por_que_positiva": (
            "um bloco novo fica FORA do hash ate alguem decidir inclui-lo. A"
            " lista negativa deixaria qualquer campo novo entrar calado - e se"
            " ele dependesse do relogio, o hash viraria volatil sem que nada"
            " avisasse"
        ),
        "o_que_fica_de_FORA_e_por_que": {
            "gerado_em": "relogio de leitura",
            "piloto": (
                "dias decorridos, taxa das ultimas 24h e projecao de"
                " fechamento mudam com o relogio mesmo sem estado novo. O"
                " piloto e publicado no documento e NAO entra no hash"
            ),
            "fase_0c": (
                "ele carrega o proprio `gerado_em` e re-publica blocos que ja"
                " estao aqui - inclui-lo contaria as mesmas coisas duas vezes"
            ),
            "regimes_observados": (
                "derivado do fluxo ao vivo, que cresce a cada barra"
            ),
        },
    }
    return documento
