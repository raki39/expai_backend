# Fase 0A — serviço `api`

Backend do experimento: agente, simulador, carteira, ledger e baselines.
**Um agente, um processo.**

Tem domínio público, porque o painel roda na Vercel e ela não alcança a rede
privada da Railway (ADR 0010). Por isso o `API_SERVICE_TOKEN` é **a única
tranca** entre a internet e estes endpoints — não é opcional e não é
defesa em profundidade.

Estado: **incremento 0 — substrato**. Ainda não há dataset, ledger, simulador
nem agente. O que existe é o que sustenta tudo isso.

## Rodar local

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt  # Linux/Mac

cp .env.example .env        # preencha API_SERVICE_TOKEN
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

O bind é `0.0.0.0` (IPv4), que é o que o proxy público da Railway usa. Sobreponha com `HOST=::` apenas se um serviço precisar falar por rede privada.

## Testes

```bash
.venv/Scripts/python.exe -m pytest
```

57 testes. Cobrem os critérios do incremento 0: autenticação em todas as
rotas, versionamento de configuração com autor/data/antes/depois, precedência
entre ambiente e banco, teto inviolável, imutabilidade por trigger,
persistência, migração idempotente, e não vazamento de segredo.

## Configuração — três camadas

Ver `.docs/adr/0008-configuracao-versionada-no-banco.md`.

| Camada | Onde | Editável no painel |
|---|---|---|
| Segredos (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `API_SERVICE_TOKEN`) | env | não |
| Bootstrap (`PORT`, `DB_PATH`, `DATA_DIR`, `LOG_LEVEL`, `LLM_MAX_USD_ABSOLUTE`) | env | não |
| Parâmetros do experimento (mercado, timeframe, taxas, tetos, câmbio, tiers, preços, B1, B3) | **banco, versionado** | **sim** |

A seção 10.2.3 do documento exige alteração de configuração "versionada no
ledger, com autor, data, valor anterior e novo". Variável de ambiente não faz
nada disso — por isso os parâmetros do experimento não moram lá.

Depois do primeiro boot, a versão 1 é criada dos defaults e **o ambiente passa
a ser ignorado** para esses campos.

## Endpoints

Todos exigem `Authorization: Bearer <API_SERVICE_TOKEN>`. Sem exceção —
`/api/health` inclusive. Com a api pública, isto é o que a protege.

| Método | Rota | Função |
|---|---|---|
| GET | `/api/health` | substrato: banco, volume, schema, config vigente |
| GET | `/api/config` | configuração vigente |
| GET | `/api/config/history` | versões com autor, data, valor anterior e novo |
| POST | `/api/config` | nova versão (recusada durante run ativo ou acima do teto) |
| POST | `/api/sentinel` | grava marcador de persistência |
| GET | `/api/sentinel` | lista marcadores |

Sem `/docs`, `/redoc` ou `/openapi.json`: a superfície é consumida pelo proxy
do `web`, e um endpoint a menos é uma coisa a menos para proteger.

## Estrutura

```
app/
├── settings.py       env: segredos e bootstrap
├── logging_setup.py  JSON estruturado + redação de segredo
├── migrations.py     DDL versionado
├── store.py          conexão, pragmas, migração atômica
├── security.py       token de serviço
├── config/
│   ├── schema.py     parâmetros do experimento + config_hash
│   └── service.py    versionamento e as três travas
├── api/routes.py     HTTP, sem lógica de negócio
└── main.py           boot
```

## Docker

O `Dockerfile` e o `docker-compose.yml` ficam na **raiz do repositório**, não
aqui: é assim que a Railway os encontra sem precisar de Root Directory
configurado. O contexto de build é a raiz, e o código Python vive em `api/`.

```bash
cd ..                          # raiz do repositorio backend
docker compose up --build      # api em http://localhost:8000
docker compose down            # para, preservando o volume
docker compose down -v         # apaga o banco
```

O compose serve **apenas para desenvolvimento local**. A Railway não orquestra
compose: ela constrói um serviço a partir do `Dockerfile`, conforme
`railway.toml`.

**O container roda como root, de propósito.** A documentação da Railway avisa
que imagens rodando como UID não-root têm problema de permissão com volumes —
e o SQLite do experimento vive no volume. É desvio consciente da boa prática,
forçado pela plataforma, comentado no `Dockerfile`.

## Deploy

Ver `railway.toml` e `.docs/adr/0010-frontend-na-vercel.md`.

O volume é montado no **start** do container, nunca no build — por isso a
migração roda no boot da aplicação.

## CORS

Desligado por padrão, e é o correto: o painel faz proxy no servidor, então o
navegador nunca chama esta api direto, e requisição servidor-para-servidor não
passa por CORS.

`CORS_ALLOWED_ORIGINS` existe para destravar chamada direta do navegador sem
mudança de código. Allowlist explícita, nunca `*`. E vale lembrar: **CORS não
autentica** — é política do navegador sobre *ler* a resposta. `curl` e qualquer
script a ignoram. O que protege esta api é o token.
