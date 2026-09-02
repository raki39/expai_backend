"""Cerebro lento: o unico lugar do projeto onde um LLM e chamado.

A fronteira da secao 3.2 e de importacao, nao de convencao: as maos rapidas
nao importam nada daqui, e ha teste que le o FONTE delas para garantir. O
inverso e permitido - o cerebro conhece a regra, o dataset e o ledger.

Os adaptadores de provedor carregam o SDK **dentro da funcao que chama**, e
nao no topo do modulo. Nao e estilo: e o que mantem verdadeira a afirmacao de
que rodar um baseline nao carrega provedor nenhum, mesmo depois de a API ter
importado o cerebro inteiro.
"""
