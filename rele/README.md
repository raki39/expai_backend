# Serviço `rele` — klines fechadas de Singapura para a `api`

Fase 0C, **ADR 0029**. Terceiro serviço do repositório `backend`.

Busca kline fechada na Binance e a entrega assinada na `api`. **Não decide nada, não guarda estado, não serve nada.**

## Por que ele existe

A `api` roda em **US East**, onde o volume com as 70.080 barras, todos os runs e o ledger vive. E `api.binance.com` devolve **HTTP 451** de lá — a Binance recusa por jurisdição (ADR 0028).

Havia duas saídas: migrar a `api` para Singapura, ou pôr um relé lá. **O relé foi escolhido porque a migração moveria o único artefato irrecuperável do projeto** para ganhar simplicidade de topologia.

```
Singapura                          US East
┌──────────────┐   HMAC + POST    ┌──────────────────┐
│    rele      │ ───────────────► │  api             │
│  (sem estado)│ ◄─────────────── │  stream_bar      │
└──────────────┘  ponto de        │  snapshot        │
       │           retomada       │  ledger, dataset │
       ▼                          └──────────────────┘
  api.binance.com
```

## O laço tem uma regra que decide o resto

**Pergunta o ponto de retomada a cada volta.** O estado de verdade é o da `api`, não o que este processo lembra — e é isso que torna queda do relé um **atraso recuperável** em vez de lacuna:

| situação | o que acontece |
|---|---|
| relé reiniciado | pergunta e retoma de onde a `api` parou |
| relé fora por uma hora | pergunta, vê o atraso, e busca o intervalo inteiro |
| dois relés por engano | os dois enviam a mesma barra, e a chave idempotente absorve |

Um relé que guardasse o próprio ponto divergiria no primeiro desencontro — e a divergência apareceria como lacuna que ninguém consegue explicar.

## Três decisões que valem registrar

**REST, e não WebSocket.** O que o forward precisa é **kline fechada**, e ela existe uma vez a cada 15 minutos. Um stream entregaria a barra em formação a cada segundo, e todas seriam descartadas menos a última. E o pull torna o backfill trivial: pedir `[de, ate]` é a *mesma* chamada que pedir as últimas — com stream, backfill exigiria um segundo caminho de código, e um segundo caminho é onde a divergência mora.

**A barra em formação é descartada aqui, e não depois.** A Binance a devolve junto com as fechadas, e ela muda a cada negócio. Se chegasse ao fluxo, a próxima tentativa levantaria `DivergenciaDeConteudo` na `api` — **erro alto**, apontando para corrupção de dado em vez de para cá.

**Inteiros por `Decimal`, nunca `float`.** Regra 5, e a razão aparece aqui: o hash canônico do snapshot compara inteiros byte a byte, e `float("0.1") * 10**8` não dá `10000000` exato em toda plataforma.

## Duas credenciais, e são duas de propósito

| | o que responde |
|---|---|
| `API_SERVICE_TOKEN` | **quem** está chamando |
| `RELE_HMAC_SECRET` | **este pedido exato** veio de quem tem o segredo, agora, e não é repetição |

Um deixa ler o painel; o outro deixa **escrever no fluxo**. Reusar o mesmo faria o comprometimento de um dar o outro.

A assinatura é `HMAC-SHA256` sobre `carimbo \n nonce \n corpo`, com o corpo **exatamente como vai no fio** — os bytes são produzidos uma vez, assinados e enviados. Serializar duas vezes faria a menor diferença de espaçamento quebrar a verificação, **parecendo credencial errada**.

## Rodar

```bash
pip install -r requirements-dev.txt
python -m pytest                    # 15 testes

RELE_API_URL=http://localhost:8000 \
API_SERVICE_TOKEN=... \
RELE_HMAC_SECRET=... \
python -m rele.main
```

`requirements.txt` está **vazio**: só biblioteca padrão. A lista curta é o que torna verificável a afirmação de que o relé não decide nada — ele não tem com o que decidir, e há teste conferindo que `sqlite3`, `langgraph`, `anthropic`, `openai` e `fastapi` não são importados.

## Configuração na Railway

```
Dockerfile Path ....... rele/Dockerfile
Config File ........... /rele/railway.toml
Regions ............... Southeast Asia (Singapore)      <- NÃO é preferência
Volume ................ NÃO — o relé não guarda estado
Domínio público ....... NÃO — ele não serve nada
```

**A região não é preferência.** US East devolve 451; foi medido no primeiro deploy do coletor, e está no ADR 0028.

`restartPolicyType = "ALWAYS"`: o relé é um laço que deve estar sempre de pé. Se ele sair, as barras param de chegar — o que se perde é **atraso recuperável**, mas atraso longo o bastante vira lacuna quando o histórico da Binance não alcança mais.

O start recusa subir sem `RELE_API_URL`, `API_SERVICE_TOKEN` e `RELE_HMAC_SECRET`, e a mensagem diz **qual** falta — em vez de um 401 opaco na primeira volta.
