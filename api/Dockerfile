# Fase 0A - servico `api`
#
# O repositorio `backend` abriga mais de um servico. Cada servico tem o seu
# Dockerfile, mas o CONTEXTO DE BUILD e a raiz do repositorio - por isso todo
# COPY aqui vem prefixado com `api/`.
#
# Quem aponta para este arquivo:
#   - docker-compose.yml da raiz:  dockerfile: api/Dockerfile
#   - Railway:                     Dockerfile Path = api/Dockerfile
#
# Consequencia pratica: nao e preciso configurar Root Directory na Railway.

FROM python:3.12-slim AS base

# PYTHONUNBUFFERED: sem isso o log JSON fica preso no buffer e as linhas
# aparecem fora de ordem, ou so quando o processo morre.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Camada de dependencias separada do codigo: mexer no app nao reinstala tudo.
COPY api/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY api/app ./app
COPY api/pytest.ini ./

# chmod aqui, e nao via bit de permissao do git: repositorio desenvolvido no
# Windows nao preserva o bit executavel de forma confiavel.
COPY api/start-backend.sh ./
RUN chmod +x ./start-backend.sh

# Diretorio do volume, para o container tambem funcionar sem volume montado
# (compose local, teste rapido).
#
# CUIDADO: e justamente por existir na imagem que "consigo escrever em /data"
# NAO prova persistencia. Sem volume, o app grava aqui e perde tudo no
# redeploy. Por isso o start-backend.sh compara o device de /data com o de /
# e recusa subir em producao se forem o mesmo.
RUN mkdir -p /data/datasets

# ---------------------------------------------------------------------------
# ATENCAO - rodamos como ROOT, de proposito.
#
# A documentacao da Railway e explicita: imagens que rodam como UID nao-root
# tem problema de permissao com volumes. O SQLite do experimento vive no
# volume montado em /data, e o incremento 0 inteiro existe para provar que
# essa escrita persiste. Desvio consciente, forcado pela plataforma.
# ---------------------------------------------------------------------------

EXPOSE 8000

# Forma exec, chamando o script. O script faz o pre-voo (volume montado e
# gravavel?), decide o endereco de bind (0.0.0.0 por padrao - IPv4, que e o
# que o proxy publico da Railway usa) e termina com `exec uvicorn`, de modo
# que o uvicorn vire PID 1 e receba SIGTERM no redeploy - fechando o SQLite
# graciosamente em vez de levar SIGKILL depois do timeout.
#
# NAO defina `startCommand` no railway.toml: com Dockerfile ele sobrescreve
# este CMD, e o pre-voo deixaria de rodar.
CMD ["./start-backend.sh"]
