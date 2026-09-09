"""Certificação: reexecutar a suíte do Portão A sem tocar o experimento.

Ver `.docs/adr/0040`. O objeto é próprio de propósito — uma reexecução de
certificação e uma tentativa experimental têm a mesma forma em `hypothesis` e
são coisas diferentes, e o que decide é a **multiplicidade**: BY corrige o
risco de que, entre muitas afirmações, alguma pareça verdadeira por acaso. Uma
certificação não faz afirmação nova sobre o mercado.
"""
