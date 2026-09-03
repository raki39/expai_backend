"""Pre-registro de hipotese: a secao 8.2, e o que ela torna computavel.

Este modulo nao importa o cerebro nem provedor nenhum. O pre-registro e
estrutura de dados e aritmetica; quem preenche os campos que vem do modelo e
o grafo, que depende daqui - nunca o contrario.
"""

from . import poder, registro, schema, veredito

__all__ = ["poder", "registro", "schema", "veredito"]
