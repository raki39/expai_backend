# Fase 0C - servico `rele` (ADR 0029)
#
# Terceiro servico do repositorio `backend`. CONTEXTO DE BUILD e a raiz, por
# isso todo COPY vem prefixado com `rele/`.
#
# Roda em SINGAPURA. US East devolve HTTP 451 - a Binance recusa por
# jurisdicao (ADR 0028).

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# `requirements.txt` esta VAZIO de proposito: o rele usa so a biblioteca
# padrao. A camada fica aqui de todo modo, para que acrescentar dependencia um
# dia nao mude a forma do Dockerfile.
COPY rele/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY rele/rele ./rele

COPY rele/start-rele.sh ./
RUN chmod +x ./start-rele.sh

# SEM volume, e a ausencia e deliberada: o rele nao guarda estado. O ponto de
# retomada e PERGUNTADO a api a cada volta, e o estado de verdade e o dela.
# Por isso nao ha pre-voo de montagem aqui, ao contrario do coletor e da api.

CMD ["./start-rele.sh"]
