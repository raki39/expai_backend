# Fase 0A - servico `api`
#
# Build determinístico: versao de Python e dependencias fixadas, sem depender
# da deteccao automatica da plataforma.

FROM python:3.12-slim AS base

# PYTHONUNBUFFERED: sem isso o log JSON fica preso no buffer e a Railway
# mostra as linhas fora de ordem (ou nao mostra ate o processo morrer).
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

# Diretorio do volume. Em producao a Railway monta o volume aqui e o conteudo
# desta linha e substituido; ela existe para o container funcionar tambem sem
# volume (compose local, teste rapido).
RUN mkdir -p /data/datasets

# ---------------------------------------------------------------------------
# ATENCAO - rodamos como ROOT, de proposito.
#
# A documentacao da Railway e explicita: "Docker images that run as a non-root
# UID by default will have permissions issues" com volumes. Como o SQLite do
# experimento vive no volume montado em /data, um usuario nao-root quebraria a
# escrita - e o incremento 0 inteiro existe para provar que essa escrita
# persiste.
#
# E um desvio consciente da boa pratica de container, forcado pela plataforma.
# Revisitar se a Railway passar a mapear o UID do volume.
# ---------------------------------------------------------------------------

EXPOSE 8000

# Forma shell (nao exec) para que ${PORT} seja expandido.
# O bind em "::" e dual-stack: atende IPv4 e IPv6 sem custo.
#
# NAO defina `startCommand` no railway.toml quando houver Dockerfile: naquele
# caso o start command sobrescreve o ENTRYPOINT em forma exec, e variaveis de
# ambiente deixam de ser expandidas a menos que voce embrulhe em shell. Um
# lugar so evita a armadilha.
CMD uvicorn app.main:app --host :: --port ${PORT:-8000}
