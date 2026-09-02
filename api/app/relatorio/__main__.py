"""Escreve o relatorio de fechamento num arquivo.

    python -m app.relatorio                       # -> /data/relatorio-0a.md
    python -m app.relatorio saida.md              # destino explicito
    python -m app.relatorio saida.md --run 14     # um run especifico

O destino padrao fica ao lado do banco, no volume: e onde o arquivo sobrevive
a um redeploy. Escrever na imagem daria um arquivo que existe ate o proximo
deploy e desaparece sem aviso - a mesma armadilha de `volume_gravavel`.

Nao ha calculo aqui: o arquivo sai da mesma `montar.montar` que a rota usa.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..settings import get_settings
from ..store import conectar, migrar
from . import escrever

NOME_PADRAO = "relatorio-0a.md"


def main(argv: list[str]) -> int:
    run_id: int | None = None
    argumentos = list(argv)
    if "--run" in argumentos:
        i = argumentos.index("--run")
        try:
            run_id = int(argumentos[i + 1])
        except (IndexError, ValueError):
            print("uso: --run <numero do run>", file=sys.stderr)
            return 2
        del argumentos[i : i + 2]

    settings = get_settings()
    destino = Path(argumentos[0]) if argumentos else settings.db_path.parent / NOME_PADRAO

    conn = conectar(settings.db_path)
    migrar(conn)
    relatorio = escrever(conn, destino, run_id=run_id)

    if not relatorio.get("existe"):
        print(f"{destino}: {relatorio.get('motivo')}")
        return 1

    resposta = relatorio["resposta_da_0a"]
    print(f"{destino}")
    print(f"run {relatorio['run']['id']} · o ciclo basico fecha: "
          f"{'SIM' if resposta['fecha'] else 'NAO'}")
    for nome in resposta["faltando"]:
        print(f"  falta: {nome}")
    # Codigo de saida diferente quando nao fecha: um script de CI que gerasse
    # o relatorio e ignorasse o resultado seria decoracao.
    return 0 if resposta["fecha"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
