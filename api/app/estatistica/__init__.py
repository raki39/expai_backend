"""Estatistica do protocolo: Sharpe realizado, p-valor, BH/BY e DSR.

Pacote sem estado e sem banco. Recebe numeros e devolve numeros - nenhuma
funcao aqui abre conexao, le tabela ou consulta orcamento.

Isso nao e organizacao: e o que torna o criterio 7 do incremento 11
verificavel. A secao 8.6.1 diz que creditos e FDR sao "mecanismos distintos
(...) nenhum substitui o outro", e um procedimento estatistico que consultasse
saldo faria da escassez uma entrada da matematica. Ha teste varrendo estes
arquivos por qualquer mencao a credito ou a orcamento.
"""

from . import dsr, fdr, pvalor, sharpe

__all__ = ["dsr", "fdr", "pvalor", "sharpe"]
