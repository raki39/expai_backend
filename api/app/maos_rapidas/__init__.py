"""Maos rapidas: execucao deterministica por barra, e os tres baselines.

Fronteira de importacao (regra 3, secao 3.2): **nao entra LangGraph, provedor
de LLM nem o cerebro lento**. Zero chamadas de modelo dentro do laco por
barra. As maos rapidas nao sao nos do grafo.

Neste ponto o sistema produz uma comparacao completa sem nenhum LLM
envolvido, e isso e deliberado: se o encanamento nao fecha sem o modelo, o
problema nao e o modelo.
"""
