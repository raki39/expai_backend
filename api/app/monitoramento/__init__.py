"""Monitoramento continuo da secao 8.8. Incremento 20, ADR 0035.

Quatro modulos, e a fronteira entre eles e a garantia:

    cusum.py     PURO e INTEIRO. A estatistica, sem banco e sem opiniao
    limiar.py    a calibracao por block bootstrap sobre o in-sample congelado
    veredito.py  PURO. Recebe medicoes e limiares, devolve estado
    monitor.py   o caminho de PRODUCAO, que na 0C nao tem sujeito

`cusum` e `veredito` nao importam `sqlite3` e nao recebem conexao, e ha teste
sobre as duas coisas. Sem isso, "a avaliacao nao recalcula metrica usando
estado atual" seria disciplina - e o incremento 19 ja mostrou que a versao
estrutural da mesma garantia e a que sobrevive.
"""

from __future__ import annotations
