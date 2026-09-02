"""Fechamento da 0A: o relatorio, o vinculo e a prova de reprodutibilidade.

    montar.montar(conn, run_id)  -> o dict, todo lido do banco
    texto.markdown(relatorio)    -> a versao para humano; so formata
    vinculo                      -> navegacao nos dois sentidos (R25.2)
    reprodutibilidade            -> os tres digests (R12)

Rodar como modulo escreve o arquivo:

    python -m app.relatorio [destino.md] [--run N]

O arquivo e o endpoint saem da MESMA funcao. Dois geradores independentes
poderiam discordar, e o relatorio e o documento em que a 0A responde a propria
pergunta - e o pior lugar do sistema para duas versoes da verdade.
"""

from __future__ import annotations

from . import montar, reprodutibilidade, texto, vinculo

__all__ = ["montar", "reprodutibilidade", "texto", "vinculo", "escrever"]


def escrever(conn, destino, *, run_id: int | None = None) -> dict:
    """Gera o relatorio e grava em `destino`. Devolve o dict que o produziu."""
    from pathlib import Path

    relatorio = montar.montar(conn, run_id)
    caminho = Path(destino)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(texto.markdown(relatorio), encoding="utf-8")
    return relatorio
