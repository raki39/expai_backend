# Fase 0A — serviço `api`

Backend do experimento: agente, simulador, carteira, ledger e baselines.
**Um agente, um processo.** Sem domínio público — só o serviço `web` o alcança,
pela rede privada da Railway.

Estado: **incremento 0 — substrato**. Ainda não há dataset, ledger, simulador
nem agente. O que existe é o que sustenta tudo isso.

## Rodar local

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt  # Linux/Mac

cp .env.example .env        # preencha API_SERVICE_TOKEN
python -m uvicorn app.main:app --host :: --port 8000
```

O bind em `::` não é detalhe: a rede privada da Railway resolve para IPv6 além
de IPv4, e ligar só em `0.0.0.0` produz falha de conectividade que parece bug
de aplicação.

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
`/api/health` inclusive.

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

## Deploy

Ver `railway.toml` e `.docs/adr/0007-topologia-dois-servicos.md`.

O volume é montado no **start** do container, nunca no build — por isso a
migração roda no boot da aplicação.
