# Fase 0C - servico `coletor` (ADR 0028)
#
# Segundo servico do repositorio `backend`. Como o da `api`, o CONTEXTO DE
# BUILD e a raiz do repositorio - por isso todo COPY vem prefixado com
# `coletor/`, e nao e preciso configurar Root Directory na Railway.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY coletor/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY coletor/coletor ./coletor

COPY coletor/start-coletor.sh ./
RUN chmod +x ./start-coletor.sh

# Existe na imagem para o container rodar sem volume (compose local). E
# justamente por existir que "consigo escrever" NAO prova persistencia - quem
# recusa subir sem volume e o pre-voo em Python, comparando st_dev.
RUN mkdir -p /dados

# ROOT de proposito, mesma razao da `api`: a Railway documenta problema de
# permissao com volumes em imagens nao-root.

CMD ["./start-coletor.sh"]
