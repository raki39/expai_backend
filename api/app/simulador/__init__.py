"""Simulador pessimista da Fase 0A.

Maos rapidas: Python deterministico, sem framework. **Nao importa LangGraph,
nem provedor de LLM, nem o cerebro lento** - a separacao da secao 3.2 e
fronteira de importacao verificavel por teste, nao convencao (regra 3).

Zero chamadas de modelo dentro do laco por barra.
"""
