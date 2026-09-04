#!/bin/sh
# Fase 0C - inicializacao do servico `rele` (ADR 0029).
#
# `exec` no fim: o Python vira PID 1 e recebe SIGTERM, encerrando o laco entre
# voltas em vez de ser morto no meio de um envio. Um envio interrompido nao
# corrompe nada - a chave idempotente absorve o reenvio -, mas o log fica mais
# limpo e o desligamento e explicito.
#
# LF obrigatorio (.gitattributes). CRLF faz o kernel nao achar o interpretador.

set -eu

registrar() {
    printf '{"ts":"%s","level":"%s","event":"%s","logger":"start-rele"%s}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "${3:-}"
}

registrar INFO startup.preflight \
    ",\"api\":\"${RELE_API_URL:-nao-definido}\",\"simbolo\":\"${RELE_SIMBOLO:-BTCUSDT}\",\"timeframe\":\"${RELE_TIMEFRAME:-15m}\""

# Falha FECHADO. Um rele sem credencial nao deve subir e tentar - e a mensagem
# diz QUAL falta, em vez de um 401 opaco na primeira volta.
for var in RELE_API_URL API_SERVICE_TOKEN RELE_HMAC_SECRET; do
    eval valor="\${$var:-}"
    if [ -z "$valor" ]; then
        registrar ERROR startup.failed \
            ",\"motivo\":\"${var} ausente\",\"acao\":\"defina as tres: RELE_API_URL, API_SERVICE_TOKEN e RELE_HMAC_SECRET\""
        exit 1
    fi
done

exec python -m rele.main
