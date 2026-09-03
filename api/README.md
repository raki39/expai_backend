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

Esta tabela é **conferida por teste** (`test_o_readme_descreve_todas_as_rotas`):
toda rota do router aparece aqui, e toda rota citada aqui existe. Uma tabela de
endpoints que ninguém confere envelhece em silêncio — esta já tinha
envelhecido, listando 6 rotas quando existiam 26.

| Método | Rota | Função |
|---|---|---|
| GET | `/api/health` | substrato: banco, volume, schema, config vigente |
| GET | `/api/config` | configuração vigente e se o catálogo está defasado |
| GET | `/api/config/history` | versões com autor, data, valor anterior e novo |
| POST | `/api/config` | nova versão (recusada durante run ativo ou acima do teto) |
| POST | `/api/config/catalogo` | adota o catálogo de provedores vigente |
| POST | `/api/config/reancorar` | regrava a config sob o hash correto após deriva de schema |
| GET | `/api/dataset` | dataset vigente: janela, sha256, barras, reserva |
| `/api/separacao` | GET | Os quatro conjuntos por finalidade, as janelas de walk-forward e o uso do holdout. Nao devolve barra nenhuma |
| `/api/validador` | GET | Maquina de estados do conhecimento, contador de tentativas e as transicoes legais |
| `/api/lote` | GET | Procedimento de lote (BH/BY) sobre a familia fechada, mais o DSR por hipotese |
| `/api/creditos` | GET | Saldo de creditos por braco e os quatro numeros de calibracao da secao 8.6.1 |
| `/api/validador/hipotese/{hypothesis_id}` | GET | O caminho inteiro de uma hipotese: pre-registro, estado e historico |
| POST | `/api/dataset/ingest` | baixa e fixa o dataset (~35 s, 46 arquivos) |
| GET | `/api/ledger` | carteira e escopo (run ativo, ou livro inteiro) |
| GET | `/api/ledger/transacoes` | histórico, com estornos |
| POST | `/api/run` | abre run e credita capital semente |
| POST | `/api/run/{run_id}/encerrar` | encerra num dos três estados terminais |
| GET | `/api/simulador` | parâmetros efetivos e condições de validade |
| GET | `/api/execucoes` | ordens e execuções simuladas |
| GET | `/api/comparacao` | última comparação B1, B2, B3 |
| POST | `/api/comparacao` | roda a comparação (sem LLM) |
| GET | `/api/curva` | curva de patrimônio e excesso sobre baseline |
| GET | `/api/agente` | caminho percorrido, propostas, gasto |
| POST | `/api/agente` | **roda o ciclo com LLM — gasta dinheiro de verdade** |
| GET | `/api/relatorio` | relatório de fechamento da 0A, em JSON |
| GET | `/api/relatorio/markdown` | o mesmo relatório, para humano |
| GET | `/api/exportar` | baixa um JSON com o estado inteiro, para anexar |
| POST | `/api/reprodutibilidade` | prova de R12: três digests, sem LLM |
| GET | `/api/vinculo/execucao/{execution_id}` | da execução ao evento cognitivo (R25.2) |
| GET | `/api/vinculo/evento/{event_id}` | da decisão ao custo, regra, execuções e resultado |
| POST | `/api/sentinel` | grava marcador de persistência |
| GET | `/api/sentinel` | lista marcadores |

Sem `/docs`, `/redoc` ou `/openapi.json`: a superfície é consumida pelo proxy
do `web`, e um endpoint a menos é uma coisa a menos para proteger.

## Operação: do zero ao relatório

A ordem importa. Cada passo depende do anterior, e pular um faz o seguinte
recusar com `409` em vez de produzir número errado.

```bash
T="Authorization: Bearer $API_SERVICE_TOKEN"
API=http://localhost:8000

# 1. o substrato responde, e o volume PERSISTE (não apenas aceita escrita)
curl -sH "$T" $API/api/health

# 2. o dataset, uma vez só. ~35 s. Fixa janela, sha256 e reserva.
curl -sH "$T" -XPOST $API/api/dataset/ingest -d '{"author":"voce"}'

# 3. a comparação sem LLM: B1, B2 e B3. Nenhum centavo gasto.
curl -sH "$T" -XPOST $API/api/comparacao -d '{"author":"voce"}'

# 4. a prova de reprodutibilidade: três digests. Também sem LLM.
curl -sH "$T" -XPOST $API/api/reprodutibilidade -d '{}'

# 5. o ciclo do agente. ESTE gasta dinheiro de verdade.
curl -sH "$T" -XPOST $API/api/agente -d '{"author":"voce"}'

# 6. o relatório
curl -sH "$T" $API/api/relatorio/markdown
```

**O passo 5 é o único que custa dinheiro.** Os tetos são rígidos: ao atingir,
as mãos rápidas continuam e o cérebro para (§3.6 regra 2). O teto do banco
nunca pode exceder `LLM_MAX_USD_ABSOLUTE` do env.

Se `/api/config` acusar `config_hash` como **não descreve mais**, um campo
novo entrou no schema. Abrir run fica bloqueado até `POST
/api/config/reancorar` — é o mecanismo funcionando, não uma falha.

### Como ler o relatório

```bash
python -m app.relatorio                    # escreve no volume, ao lado do banco
python -m app.relatorio saida.md --run 14  # destino e run explícitos
```

Sai `0` quando o ciclo fecha e `1` quando não fecha — um script de CI que
gerasse o relatório e ignorasse o resultado seria decoração.

O relatório abre pela **pergunta da 0A**: *o ciclo básico fecha?*. A resposta
não é digitada em lugar nenhum: é a conjunção de doze condições, cada uma um
booleano vindo de uma consulta. Quando alguma é falsa, o relatório **diz
qual** — e `nao se aplica` é diferente de `NAO`, porque com o teto em zero não
há custo de decisão a registrar (D23), e reprovar o run por isso confundiria
"não sei" com "não".

As dez seções respondem, nesta ordem: o que o agente **observou** (janela e
dataset com hash), quanto **refletiu** (tokens e custo lidos do `usage` real),
que **regra propôs** (hash, JSON e a expectativa declarada *antes* de
executar), o que **executou**, quanto **custou nos dois livros**, como se saiu
**contra B1, B2 e B3**, o que a **avaliação posterior** comparou, o **caminho
percorrido** com o vínculo navegado nos dois sentidos, a **integridade
contábil** e a **reprodutibilidade**.

Duas leituras que enganam se lidas rápido:

- **Excesso sobre baseline, nunca valor absoluto.** "US$ 620" não diz nada; o
  que diz é a faixa contra o B1 casado, que gira o mesmo número de vezes **e
  com o mesmo tamanho de posição**.
- **Zero reflexões significa que o agente É o B3** (D23). Um run assim não
  mede cérebro nenhum, e o relatório informa isso na seção 2.

## Estrutura

```
app/
├── settings.py       env: segredos e bootstrap
├── logging_setup.py  JSON estruturado + redação de segredo
├── migrations.py     DDL versionado (8 migrações)
├── store.py          conexão, pragmas, migração atômica
├── security.py       token de serviço
├── config/           parâmetros versionados no banco + config_hash
├── dataset/          ingestão da Binance, integridade, reserva
├── ledger/           partidas dobradas, dois livros, contas por run
├── simulador/        execução pessimista, custos decompostos
├── regra/            catálogo fechado de 3 famílias, hash de conteúdo
├── maos_rapidas/     executor por barra, B1/B2/B3, curva — SEM LLM
├── cerebro/          grafo de 4 nós, adaptadores, custo, cache, tetos
│   └── avaliacao.py  a avaliação posterior: evento filho da decisão
├── relatorio/        fechamento da 0A: montar · texto · vínculo · R12
├── api/routes.py     HTTP, sem lógica de negócio
└── main.py           boot
```

**A fronteira que não é convenção:** `maos_rapidas/` não importa `cerebro/`,
nem LangGraph, nem provedor — e há teste lendo o código-fonte para provar
(§3.2). Um import dentro de função escaparia da inspeção em tempo de execução,
então a verificação é sobre o texto, não sobre o processo.

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
