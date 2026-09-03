"""O validador: independente do agente, e quem promove (§8.1, R36).

Este pacote **não importa** `app/cerebro`, adaptador de provedor nem
LangGraph. A fronteira é verificada por AST, como a de §3.2 entre mãos rápidas
e cérebro — independência que depende de disciplina já foi violada.

A direção permitida é a inversa: o ciclo do agente **solicita** ao validador,
que é o que §11.2.1 descreve para `validate_on_holdout` — "o agente solicita;
quem executa é o Validador".
"""

from . import contador, estados, lote, promocao

__all__ = ["contador", "estados", "lote", "promocao"]
