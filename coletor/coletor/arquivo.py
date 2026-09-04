"""Escrita em JSONL comprimido, rotacionado por dia UTC.

ADR 0028: arquivo PROPRIO, comprimido e rotacionado, **nunca** o SQLite do
experimento. A separacao e o que permite que a D44 mantenha escritor unico no
banco - o coletor nao encosta nele.

Um detalhe que decide se o arquivo presta depois de uma queda: **gzip guarda
estado interno**, e um processo morto sem fechar o membro deixa o final do
arquivo ilegivel. Por isso ha `flush()` periodico e fechamento no SIGTERM. E a
mesma licao do `exec` no start-backend.sh: desligar direito nao e detalhe.
"""

from __future__ import annotations

import gzip
import json
import os
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

# Intervalo de descarga do buffer do gzip, EM SEGUNDOS.
#
# Por TEMPO e nao por contagem de linhas: se a taxa de amostragem mudar um dia,
# uma regra por contagem deixaria de significar o que significa hoje, calada.
#
# O numero saiu de MEDICAO, e a primeira versao estava errada. Ela usava 60
# linhas, e o teste de fumaca real provou o problema: em 40 s nenhum flush
# ocorreu, o zlib segurou tudo no buffer interno, e o leitor recuperou ZERO
# linhas de um arquivo de 1,8 KB. Nao se perde "o ultimo minuto" - perde-se
# TUDO desde a ultima descarga, porque sem uma fronteira Z_SYNC_FLUSH o
# descompressor nao entrega nada.
#
# Custo medido sobre uma hora de amostras reais a 1 Hz:
#
#     flush      MB/mes    overhead    exposicao
#     nunca        15,8           -    tudo
#     10 s         21,5      +35,6%    10 s     <- adotado
#     30 s         17,1       +7,7%    30 s
#     60 s         16,0       +1,1%    60 s
#
# 21,5 MB/mes num volume de 5 GB dao 19 anos. Com essa folga compra-se a menor
# exposicao: numa queda suja perdem-se 10 amostras, e nao 60.
#
# (De quebra, a medicao corrigiu a estimativa da D32: ela previa ~105 MB/mes
# supondo 40 B por linha comprimida. O real e ~16 - nomes de campo repetidos
# comprimem quase a zero. Sobra folga de 6,5x sobre o que foi orcado.)
SEGUNDOS_POR_FLUSH = 10.0


def dia_utc(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d")


class Diario:
    """Um arquivo por dia UTC, por prefixo.

    Nao ha `append` a arquivo de dia passado: a rotacao e por carimbo da
    linha, entao uma linha atrasada cai no arquivo do dia dela.
    """

    def __init__(self, diretorio: Path, prefixo: str, *,
                 segundos_por_flush: float = SEGUNDOS_POR_FLUSH,
                 agora: Callable[[], float] = time.monotonic) -> None:
        self.diretorio = Path(diretorio)
        self.prefixo = prefixo
        self.diretorio.mkdir(parents=True, exist_ok=True)
        self.segundos_por_flush = segundos_por_flush
        self._agora = agora
        self._dia: str | None = None
        self._f: gzip.GzipFile | None = None
        self._ultimo_flush = agora()
        self.linhas = 0
        self.flushes = 0

    def caminho(self, dia: str) -> Path:
        return self.diretorio / f"{self.prefixo}-{dia}.jsonl.gz"

    def _abrir(self, dia: str) -> None:
        self._fechar()
        # mtime=0 para que o gzip seja reproduzivel byte a byte dado o mesmo
        # conteudo - o cabecalho do gzip carrega o horario de criacao, e sem
        # isso dois arquivos identicos teriam hashes diferentes.
        self._f = gzip.GzipFile(
            filename=str(self.caminho(dia)), mode="ab", compresslevel=6, mtime=0
        )
        self._dia = dia
        self._ultimo_flush = self._agora()

    def _fechar(self) -> None:
        if self._f is not None:
            self._f.flush()
            self._f.close()
            self._f = None

    def escrever(self, linha: dict[str, Any], *, ns: int) -> None:
        dia = dia_utc(ns)
        if dia != self._dia:
            self._abrir(dia)
        assert self._f is not None
        self._f.write((json.dumps(linha, separators=(",", ":")) + "\n").encode("utf-8"))
        self.linhas += 1
        if self._agora() - self._ultimo_flush >= self.segundos_por_flush:
            self.flush()

    def flush(self) -> None:
        """Descarga com Z_SYNC_FLUSH: cria uma fronteira RECUPERAVEL.

        E o que permite a `ler()` devolver tudo o que veio antes de um
        truncamento. Sem fronteira, o descompressor nao entrega nada - foi
        exatamente o que o primeiro teste de fumaca produziu.
        """
        if self._f is not None:
            self._f.flush()
            self._ultimo_flush = self._agora()
            self.flushes += 1

    def fechar(self) -> None:
        self._fechar()

    def __enter__(self) -> "Diario":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.fechar()


def conferir_destino(destino: Path, db_path: str | None) -> None:
    """Recusa gravar onde mora o banco do experimento.

    Na Railway os dois servicos tem volumes distintos e a colisao nao acontece.
    Localmente, com um compose mal configurado, acontece - e o sintoma seria o
    coletor escrevendo no volume cuja propriedade exclusiva de escrita a D32
    nomeou como bloqueio.

    Falhar no boot com mensagem clara e melhor que descobrir depois.
    """
    if not db_path:
        return
    dir_banco = Path(db_path).expanduser().resolve().parent
    d = Path(destino).expanduser().resolve()
    if d == dir_banco or dir_banco in d.parents or d in dir_banco.parents:
        raise SystemExit(
            f"coletor: destino {d} colide com o diretorio do banco do "
            f"experimento ({dir_banco}). O ADR 0028 exige arquivo proprio, "
            f"em volume proprio - nunca o SQLite do experimento. "
            f"Ajuste COLETOR_DIR."
        )


def volume_montado(destino: Path) -> bool:
    """`st_dev` de destino difere do de `/`?

    Mesma checagem que o incremento 0 escreveu para a api, e pela mesma razao:
    **escrever com sucesso nao e persistir**. Sem volume montado, o coletor
    grava no filesystem efemero e perde tudo no redeploy, sem erro nenhum.
    """
    try:
        return os.stat(destino).st_dev != os.stat("/").st_dev
    except OSError:
        return False


def ler(caminho: Path) -> Iterator[dict[str, Any]]:
    """Le um arquivo do coletor, TOLERANDO truncamento no fim.

    Isto nao e defensividade decorativa: o primeiro teste de fumaca real
    produziu exatamente esse arquivo. O processo levou SIGKILL, o membro gzip
    ficou sem marcador de fim, e `gzip.decompress` recusou o arquivo INTEIRO -
    nao a ultima linha, o dia todo.

    E nao da para prevenir na escrita: SIGTERM a gente trata, SIGKILL nao - e
    a Railway derruba container em redeploy. Entao quem aguenta e o leitor.

    **Usa `zlib.decompressobj`, e nao `gzip.open`, por um motivo medido.**
    `GzipFile.read(n)` tenta satisfazer o pedido inteiro e levanta EOFError ao
    encontrar o corte **sem devolver o que ja tinha descomprimido** - o que faz
    um arquivo truncado render zero linhas mesmo tendo dados validos. O
    decompressor incremental devolve tudo a medida que sai, e o truncamento
    vira simplesmente o fim da entrada.

    Membros concatenados sao tratados: reabrir o arquivo em modo `ab` depois de
    um restart cria um membro gzip novo, e ignorar isso perderia todo o dado
    posterior ao primeiro restart do dia.

    A ultima linha e descartada se estiver incompleta (sem quebra), porque uma
    linha cortada no meio nao e um registro - e completar por conta seria
    inventar dado, que e o que nenhuma parte deste projeto faz.
    """
    WBITS_GZIP = 31
    resto = b""
    try:
        bruto = caminho.read_bytes()
    except OSError:
        return
    d = zlib.decompressobj(WBITS_GZIP)
    while bruto:
        try:
            saida = d.decompress(bruto)
        except zlib.error:
            # Lixo no meio do fluxo. Tudo que saiu antes continua valendo.
            break
        if saida:
            resto += saida
            *completas, resto = resto.split(b"\n")
            for linha in completas:
                if linha.strip():
                    try:
                        yield json.loads(linha)
                    except ValueError:
                        # Linha corrompida no corte. Descartada, nao adivinhada.
                        continue
        if not d.eof:
            # Entrada consumida sem terminar o membro: e o truncamento.
            break
        bruto = d.unused_data
        if not bruto:
            break
        d = zlib.decompressobj(WBITS_GZIP)
    # `resto` sem quebra de linha e registro incompleto, descartado de proposito.


def integridade(caminho: Path) -> dict[str, Any]:
    """Quantas linhas o arquivo entrega, e se ele terminou limpo.

    `truncado` e FATO OBSERVADO, nao suspeita: ou o membro gzip fecha, ou nao.
    Vira campo do relatorio de calibracao, para que uma lacuna de dado nunca
    seja confundida com um mercado parado.
    """
    linhas = 0
    for _ in ler(caminho):
        linhas += 1
    truncado = False
    try:
        with gzip.open(caminho, "rb") as f:
            while f.read(1 << 20):
                pass
    except (EOFError, OSError, gzip.BadGzipFile):
        truncado = True
    return {"caminho": str(caminho), "linhas": linhas, "truncado": truncado}
