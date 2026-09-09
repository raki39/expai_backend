"""Um executor por escopo. O que muda entre eles é o que precisa de cópia.

| escopo | precisa de cópia? | por quê |
|---|---|---|
| **a1a** | **sim** | injeta pelo caminho real: registra hipótese, cobra crédito, escreve transição e ledger |
| **a1b** | **não** | `calibre.rodar` é pura — demonstrado em `test_a1b_parcelado`. Nem lê nem escreve banco |
| **a2** | **sim** | precisa dos baselines B1 em dois giros, e rodá-los criaria runs reais |
| **a3** | **não** | purga e embargo são conferência de leitura sobre a separação |
| **a4** | **não** | ledger, custo de IA e contagem de tentativas são leitura do registro |

**Escopo que não precisa de cópia não usa cópia.** Copiar 48 MB para fazer três
`SELECT` seria custo operacional sem contrapartida — e a cópia existe para
conter escrita, não por simetria.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from . import escopos as esc
from . import laboratorio


class CertificacaoRecusada(ValueError):
    """A certificação rodou e o certificado NÃO pode existir."""


@dataclass(frozen=True)
class Resultado:
    """O que um escopo produziu: casos canônicos e o custo de produzi-los."""

    casos: list[dict]
    micros_para_copiar: int
    micros_da_suite: int
    bytes_da_copia: int
    intocado_antes: dict
    intocado_depois: dict
    preparo: dict


# ---------------------------------------------------------------------------
# a1a — na cópia, pelo caminho real
# ---------------------------------------------------------------------------


def rodar_a1a(oficial, *, dataset_id, config, config_version_id) -> Resultado:
    from ..a1a import braco as a1a_braco
    from .suite import _caso_canonico, _preparar_laboratorio

    antes = laboratorio.fotografia(oficial)
    inicio = time.perf_counter_ns()
    with laboratorio.laboratorio_descartavel(oficial) as copia:
        preparo = _preparar_laboratorio(
            copia.conn, dataset_id=dataset_id, config=config,
            config_version_id=config_version_id,
        )
        r = a1a_braco.rodar(
            copia.conn, dataset_id=dataset_id, config=config,
            config_version_id=config_version_id,
        )
        casos = []
        for controle in r.controles:
            caso = _caso_canonico(controle)
            # O MECANISMO, e nao so `promovido: False`. Ver
            # `escopos.classificar_bloqueio`.
            caso["bloqueio"] = esc.classificar_bloqueio(controle)
            # `contido` sobe para o caso: e ele que o selo le. Para A1a,
            # contido = nao promovido - mas o COMO fica visivel no bloqueio,
            # porque barrado na porta e contido pela estatistica sao
            # informacoes diferentes.
            caso["contido"] = caso["bloqueio"]["contido"]
            casos.append(caso)
        micros_copia = copia.micros_para_copiar
        bytes_copia = copia.bytes_em_disco()
    return Resultado(
        casos=casos,
        micros_para_copiar=micros_copia,
        micros_da_suite=(time.perf_counter_ns() - inicio) // 1_000 - micros_copia,
        bytes_da_copia=bytes_copia,
        intocado_antes=antes,
        intocado_depois=laboratorio.fotografia(oficial),
        preparo=preparo,
    )


# ---------------------------------------------------------------------------
# a2 — na cópia, porque precisa dos baselines
# ---------------------------------------------------------------------------


def rodar_a2(oficial, *, dataset_id, config, config_version_id) -> Resultado:
    """B1 perde, e perde proporcionalmente ao giro.

    Na 0A isto era sanidade observada; §14.4 o torna **portão**. Se operar ao
    acaso desse lucro, nada medido no simulador significaria coisa alguma.
    """
    from ..relatorio import portao_a as portao
    from .suite import _preparar_laboratorio

    antes = laboratorio.fotografia(oficial)
    inicio = time.perf_counter_ns()
    with laboratorio.laboratorio_descartavel(oficial) as copia:
        preparo = _preparar_laboratorio(
            copia.conn, dataset_id=dataset_id, config=config,
            config_version_id=config_version_id,
        )
        bloco = portao._a2(copia.conn, config_version_id, config)
        micros_copia = copia.micros_para_copiar
        bytes_copia = copia.bytes_em_disco()

    casos = [
        {
            "chave": "b1_negativo",
            "familia_de_defeito": "simulador que da lucro ao acaso",
            "tipo": "estrutural",
            "observado": bloco,
            "contido": bool(bloco.get("negativo")),
            "promovido": False,
            "barrado": bool(bloco.get("negativo")),
        },
        {
            "chave": "b1_proporcional_ao_giro",
            "familia_de_defeito": "custo que nao acompanha o giro",
            "tipo": "estrutural",
            "observado": {
                "proporcional": bloco.get("proporcional_ao_giro"),
                "por_que": bloco.get("por_que_sem_inclinacao"),
            },
            # `None` quando ha um giro so: um ponto nao tem inclinacao, e
            # afirmar que tem seria inventar a segunda medida. `None` SOBE
            # como `None` - quem sela recusa, em vez de tratar como falha.
            "contido": bloco.get("proporcional_ao_giro"),
            "promovido": False,
            "barrado": bloco.get("proporcional_ao_giro") is True,
        },
    ]
    return Resultado(
        casos=casos,
        micros_para_copiar=micros_copia,
        micros_da_suite=(time.perf_counter_ns() - inicio) // 1_000 - micros_copia,
        bytes_da_copia=bytes_copia,
        intocado_antes=antes,
        intocado_depois=laboratorio.fotografia(oficial),
        preparo=preparo,
    )


# ---------------------------------------------------------------------------
# a3 e a4 — leitura, sem cópia
# ---------------------------------------------------------------------------


def rodar_a3(oficial, *, dataset_id, config, config_version_id) -> Resultado:
    """Purga e embargo, nas três janelas. Conferência de leitura."""
    from ..relatorio import portao_a as portao

    antes = laboratorio.fotografia(oficial)
    inicio = time.perf_counter_ns()
    bloco = portao._a3(oficial, dataset_id, config_version_id)
    casos = [
        {
            "chave": "sem_vazamento",
            "familia_de_defeito": "vazamento entre treino e teste",
            "tipo": "estrutural",
            "observado": bloco,
            # `sem_vazamento` e a chave que o relatorio le; `None` e "nao
            # medido", e nao "sem vazamento".
            "contido": bloco.get("sem_vazamento"),
            "promovido": False,
            "barrado": bloco.get("sem_vazamento") is True,
        }
    ]
    return Resultado(
        casos=casos, micros_para_copiar=0,
        micros_da_suite=(time.perf_counter_ns() - inicio) // 1_000,
        bytes_da_copia=0, intocado_antes=antes,
        intocado_depois=laboratorio.fotografia(oficial),
        preparo={"nada": "a3 e leitura: nao ha o que preparar nem que copiar"},
    )


def rodar_a4(oficial, *, dataset_id, config, config_version_id) -> Resultado:
    """Ledger reconcilia, custo de IA por decisão, nenhuma tentativa some."""
    from ..relatorio import portao_a as portao

    antes = laboratorio.fotografia(oficial)
    inicio = time.perf_counter_ns()
    bloco = portao._a4(oficial, config_version_id)
    nomes = (
        ("ledger_reconcilia", "ledger_reconcilia", "ledger que nao fecha"),
        ("custo_de_ia_por_decisao", "custo_de_ia_por_decisao",
         "decisao sem custo registrado"),
        ("nenhuma_tentativa_some", "nenhuma_tentativa_some",
         "tentativa que desaparece do registro"),
    )
    casos = [
        {
            "chave": chave,
            "familia_de_defeito": familia,
            "tipo": "estrutural",
            "observado": {campo: bloco.get("conferencias", {}).get(campo)},
            # `None` NAO e `True`: um criterio que ninguem mediu nao e um
            # criterio satisfeito. A licao do Portao A, aplicada aqui.
            "contido": bloco.get("conferencias", {}).get(campo),
            "promovido": False,
            "barrado": bloco.get("conferencias", {}).get(campo) is True,
        }
        for chave, campo, familia in nomes
    ]
    return Resultado(
        casos=casos, micros_para_copiar=0,
        micros_da_suite=(time.perf_counter_ns() - inicio) // 1_000,
        bytes_da_copia=0, intocado_antes=antes,
        intocado_depois=laboratorio.fotografia(oficial),
        preparo={"nada": "a4 e leitura do registro: nao ha o que copiar"},
    )


#: O executor de cada escopo que roda de uma vez. A1b é parcelado e tem
#: caminho próprio — ver `suite.iniciar_a1b` e `suite.rodar_bloco`.
DE_UMA_VEZ = {
    esc.A1A: rodar_a1a,
    esc.A2: rodar_a2,
    esc.A3: rodar_a3,
    esc.A4: rodar_a4,
}
