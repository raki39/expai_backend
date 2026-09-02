# Fase 0A - servico `api`
#
# Este servico e auto-contido: Dockerfile, compose e railway.toml vivem aqui,
# nao na raiz do repositorio. O repositorio `backend` abriga mais de um
# servico, e cada um constroi o seu.
#
# Contexto de build: esta pasta (api/).

FROM python:3.12-slim AS base

# PYTHONUNBUFFERED: sem isso o log JSON fica preso no buffer e as linhas
# aparecem fora de ordem, ou so quando o processo morre.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Camada de dependencias separada do codigo: mexer no app nao reinstala tudo.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY pytest.ini ./

# Diretorio do volume. Em producao a Railway monta o volume aqui e substitui
# o conteudo; existe para o container tambem funcionar sem volume.
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

# Forma shell (nao exec) para que ${PORT} seja expandido.
# Bind em "::" e dual-stack: atende IPv4 e IPv6.
#
# NAO defina `startCommand` no railway.toml: com Dockerfile ele sobrescreve o
# ENTRYPOINT em forma exec e ${PORT} deixa de ser expandido.
CMD uvicorn app.main:app --host :: --port ${PORT:-8000}
