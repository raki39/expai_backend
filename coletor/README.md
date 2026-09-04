# Serviço `coletor` — topo de livro a 1 Hz

Fase 0C, **ADR 0028**. Segundo serviço do repositório `backend`.

Grava o topo de livro de BTC/USDT (melhor bid e ask, com tamanhos) em arquivo próprio, comprimido e rotacionado por dia. **Nunca toca o SQLite do experimento.**

## O que ele é, e o que ele não é

| | |
|---|---|
| **não participa** | de nenhuma **decisão** da Fase 0 — nem do agente, nem do validador, nem de promoção |
| **participa** | da **medição** de calibração do simulador (ADR 0027), e essa leitura é do calibrador |

Essa é a **R83 reescrita**. A frase original da D32 — *"o único ganho é histórico que só a Fase 1–2 consome"* — deixou de ser verdade quando a D45 fechou. Quem impõe a separação é a guarda de importação de R82, em `backend/api/tests/test_fronteira_coletor.py`.

## Rodar

```bash
pip install -r requirements-dev.txt
python -m pytest                       # 23 testes

COLETOR_DIR=./dados APP_ENV=local python -m coletor.main
```

Ou pelo compose da raiz do repositório: `docker compose up coletor`.

## Ler o que ele gravou

```python
from pathlib import Path
from coletor.arquivo import ler, integridade

p = Path("dados/bookticker-btcusdt-2026-09-04.jsonl.gz")
print(integridade(p))          # {'linhas': 103, 'truncado': True}
for linha in ler(p):
    ...
```

**Use `ler()`, nunca `gzip.decompress()`.** O arquivo pode estar truncado — o processo leva SIGKILL em redeploy —, e `gzip.decompress` recusa o arquivo **inteiro** nesse caso, não a última linha. `ler()` entrega tudo até o corte.

## O registro

Uma linha por segundo:

```json
{"sampled_at_ns":…, "received_at_ns":…, "idade_ms":0,
 "u":99707809437, "bid":"79486.00000000", "bid_qty":"2.62166000",
 "ask":"79486.01000000", "ask_qty":"2.23666000",
 "disponivel":true, "motivo":null,
 "u_duplicado":false, "u_regrediu":false, "delta_u":1}
```

E, intercalada, a telemetria do relógio a cada 5 min:

```json
{"tipo":"relogio", "rtt_ms":319.9, "offset_ms":-2450.1,
 "incerteza_bruta_ms":2610.1, "incerteza_residual_ms":160.0, "server_time_ms":…}
```

### Três coisas do registro que são decisão, não detalhe

**1 · Não existe carimbo da exchange.** O `bookTicker` **spot** entrega seis campos — `u, s, b, B, a, A` — e não tem `E` nem `T`. Quem tem é o de **futuros**. O horário disponível é **só o de recebimento local**, e gravar uma coluna `exchange_ts` sempre nula seria declarar um campo inexistente.

**2 · `u` não decide disponibilidade.** Um `u` parado é compatível com mercado calmo **e** com stream travado. Ele serve para **duplicação, regressão e salto** — observações. Quem decide validade é `received_at`, a idade da mensagem, o estado da conexão e as lacunas registradas.

> Medido nos primeiros 102 samples reais: **18 tinham `u` parado**. Tratá-lo como prova de defasagem teria descartado 18% das amostras por engano.

**3 · Amostra indisponível não repete a anterior.** Sai com os preços nulos e o motivo escrito — `sem_mensagem`, `desconectado` ou `defasada`. Sem interpolação, sem preencher. Mesma regra da subseção 3b do ADR 0026.

## O relógio é medido, não suposto

A cada 5 minutos, `GET /api/v3/time` com o cálculo clássico de ida e volta. A referência é o relógio da **própria Binance**, e não um NTP genérico: é contra o relógio dela que as cotações dela são comparadas.

**Por que isso não é zelo excessivo** — medido nesta máquina:

| | |
|---|---|
| offset | **−2.450 ms** |
| incerteza **bruta** (ignorando o offset) | **2.610 ms** — estoura a tolerância inteira de 2.000 ms do ADR 0027 |
| incerteza **residual** (corrigido) | **160 ms** — cabe com 12× de folga |

O offset é grande e **corrigível**; a assimetria da viagem é pequena e **não é**. Quem não mede carrega o primeiro como se fosse zero.

## Volume e rotação

| | |
|---|---|
| arquivo | `bookticker-<simbolo>-YYYY-MM-DD.jsonl.gz`, rotação por dia **UTC** |
| descarga | **a cada 10 s**, por tempo e não por contagem de linhas |
| tamanho medido | **~21,5 MB/mês** — 19 anos num volume de 5 GB |

**O intervalo de descarga saiu de medição, e a primeira versão estava errada.** Ela descarregava a cada 60 linhas; o teste de fumaça real provou o problema — em 40 s nenhuma descarga ocorreu, e o leitor recuperou **zero** linhas. Sem uma fronteira `Z_SYNC_FLUSH` o descompressor não entrega nada, então não se perde "o último minuto": perde-se **tudo desde a última descarga**.

| flush | MB/mês | overhead | exposição |
|---|---|---|---|
| nunca | 15,8 | — | tudo |
| **10 s** | **21,5** | +35,6% | **10 s** |
| 30 s | 17,1 | +7,7% | 30 s |
| 60 s | 16,0 | +1,1% | 60 s |

De quebra, a medição corrigiu a estimativa da **D32**: ela previa ~105 MB/mês supondo 40 B por linha comprimida. O real é ~16 — nomes de campo repetidos comprimem quase a zero.

## Configuração na Railway

Ver `railway.toml`. O essencial:

```
Dockerfile Path ....... coletor/Dockerfile
Config File ........... /coletor/railway.toml
Volume ................ /dados          <- PRÓPRIO, nunca o /data da api
Domínio público ....... NÃO
APP_ENV=railway         sem volume montado, RECUSA subir
```

`restartPolicyType = "ALWAYS"`, e não `ON_FAILURE`: se o coletor sair por qualquer motivo, o custo é dado perdido que não volta.

**Não defina `DB_PATH` neste serviço.** Ele existe só para o pré-voo detectar colisão com o volume do experimento e recusar o boot.
