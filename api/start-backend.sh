#!/bin/sh
# Fase 0A - inicializacao do servico `api`.
#
# Por que existe um script em vez de `CMD uvicorn ...` direto:
#
# 1. SINAIS. `CMD uvicorn ...` em forma shell roda /bin/sh -c "uvicorn ...",
#    e o shell vira PID 1. Ele NAO repassa SIGTERM ao uvicorn, entao no
#    redeploy o processo nao desliga graciosamente: a plataforma espera o
#    timeout e manda SIGKILL. Com `exec` no fim deste script, o uvicorn
#    SUBSTITUI o shell e vira PID 1 - recebendo o sinal e fechando o SQLite
#    corretamente. Com um run em andamento, essa diferenca importa.
#
# 2. PRE-VOO. Falhar aqui, com mensagem clara, e melhor que falhar depois
#    dentro do Python - ou pior, subir e gravar em disco efemero.
#
# Terminadores de linha: LF obrigatorio (ver .gitattributes). CRLF faz o
# kernel nao encontrar o interpretador e o container morre com
# "no such file or directory", que e um dos erros mais confusos do Docker.

set -eu

PORTA="${PORT:-8000}"

# ATENCAO - o endereco de bind decide se a plataforma alcanca o app.
#
#   0.0.0.0  IPv4. E o que o proxy PUBLICO da Railway usa para conectar no
#            container. E o default correto enquanto o painel estiver na
#            Vercel e falar com a api pela internet (ADR 0010).
#
#   ::       IPv6. So e necessario para a REDE PRIVADA da Railway
#            (*.railway.internal). Um socket em "::" sem dual-stack explicito
#            RECUSA conexao IPv4 - e o sintoma e exatamente
#            "Application failed to respond" com o app rodando e logando
#            "Uvicorn running on http://[::]:PORTA".
#
# Se um dia um segundo servico precisar falar com este pela rede privada,
# defina HOST=:: na plataforma. Nao mude o default sem esse motivo.
ENDERECO="${HOST:-0.0.0.0}"
CAMINHO_BANCO="${DB_PATH:-/data/fase0a.sqlite3}"
DIR_BANCO=$(dirname "$CAMINHO_BANCO")
DIR_DADOS="${DATA_DIR:-/data/datasets}"

# Uma linha JSON, no mesmo formato do log da aplicacao, para que o diagnostico
# de boot nao destoe do resto.
registrar() {
    printf '{"ts":"%s","level":"%s","event":"%s","logger":"start-backend"%s}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "${3:-}"
}

registrar INFO startup.preflight \
    ",\"app_env\":\"${APP_ENV:-nao-definido}\",\"host\":\"${ENDERECO}\",\"port\":\"${PORTA}\",\"db_path\":\"${CAMINHO_BANCO}\",\"data_dir\":\"${DIR_DADOS}\""

# --------------------------------------------------------------- pre-voo
# O volume e montado no START do container, nunca no build. Se ele nao estiver
# montado, este diretorio existe mas mora no filesystem efemero - e o banco
# desaparece no proximo deploy, sem erro nenhum. Melhor descobrir agora.
if ! mkdir -p "$DIR_BANCO" "$DIR_DADOS" 2>/dev/null; then
    registrar ERROR startup.failed \
        ",\"motivo\":\"nao foi possivel criar ${DIR_BANCO}\",\"acao\":\"confira o volume montado em /data\""
    exit 1
fi

if ! touch "$DIR_BANCO/.escrita_ok" 2>/dev/null; then
    registrar ERROR startup.failed \
        ",\"motivo\":\"${DIR_BANCO} nao aceita escrita\",\"acao\":\"na Railway, confira o mount path do volume\""
    exit 1
fi
rm -f "$DIR_BANCO/.escrita_ok"

# Escrever com sucesso NAO prova persistencia. O Dockerfile cria /data na
# propria imagem, entao o app grava normalmente mesmo sem volume - e perde
# tudo no redeploy seguinte, em silencio.
#
# Um volume montado e outro dispositivo de arquivos. Se o device de
# $DIR_BANCO for o mesmo de "/", nao ha volume: e diretorio da imagem.
DEV_DADOS=$(df -P "$DIR_BANCO" 2>/dev/null | tail -1 | awk '{print $1}')
DEV_RAIZ=$(df -P / 2>/dev/null | tail -1 | awk '{print $1}')

# AVISO, nao bloqueio: `df` varia entre ambientes e esta checagem nao pode
# ser testada fora de Linux. Quem RECUSA subir e a checagem em Python
# (app/main.py), que compara st_dev e tem teste unitario. Aqui so adiantamos
# o diagnostico no log.
if [ "${APP_ENV:-local}" = "railway" ] && [ -n "$DEV_DADOS" ] && [ "$DEV_DADOS" = "$DEV_RAIZ" ]; then
    registrar WARNING startup.volume_ausente ",\"motivo\":\"${DIR_BANCO} parece nao estar num volume montado\",\"device\":\"${DEV_DADOS}\",\"acao\":\"Railway > Settings > Volumes: mount path exatamente /data\""
fi

registrar INFO startup.preflight_ok     ",\"db_dir_gravavel\":true,\"device\":\"${DEV_DADOS:-desconhecido}\",\"volume_montado\":$([ "$DEV_DADOS" != "$DEV_RAIZ" ] && echo true || echo false)"

# ------------------------------------------------------------------ start
# `exec` e o ponto do script: o uvicorn substitui este shell e vira PID 1,
# passando a receber SIGTERM diretamente.
exec uvicorn app.main:app --host "$ENDERECO" --port "$PORTA"
