#!/bin/sh
# Fase 0C - inicializacao do servico `coletor` (ADR 0028).
#
# Existe pelos dois motivos do start-backend.sh:
#
# 1. SINAIS. `exec` no fim faz o Python virar PID 1 e RECEBER SIGTERM. Aqui
#    isso decide se o arquivo presta: gzip guarda estado interno, e um processo
#    morto sem fechar o membro deixa o final do arquivo do dia ilegivel.
#
# 2. PRE-VOO com mensagem clara, antes de gravar em disco efemero.
#
# Terminadores de linha: LF obrigatorio (.gitattributes). CRLF faz o kernel
# nao encontrar o interpretador, e o container morre com "no such file or
# directory".

set -eu

DESTINO="${COLETOR_DIR:-/dados}"

registrar() {
    printf '{"ts":"%s","level":"%s","event":"%s","logger":"start-coletor"%s}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "${3:-}"
}

registrar INFO startup.preflight \
    ",\"app_env\":\"${APP_ENV:-nao-definido}\",\"coletor_dir\":\"${DESTINO}\",\"simbolo\":\"${COLETOR_SIMBOLO:-btcusdt}\""

if ! mkdir -p "$DESTINO" 2>/dev/null; then
    registrar ERROR startup.failed \
        ",\"motivo\":\"nao foi possivel criar ${DESTINO}\",\"acao\":\"confira o volume\""
    exit 1
fi

if ! touch "$DESTINO/.escrita_ok" 2>/dev/null; then
    registrar ERROR startup.failed \
        ",\"motivo\":\"${DESTINO} nao aceita escrita\",\"acao\":\"confira o mount path do volume\""
    exit 1
fi
rm -f "$DESTINO/.escrita_ok"

exec python -m coletor.main
