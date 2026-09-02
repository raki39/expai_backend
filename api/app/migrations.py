"""Migracoes de schema, aplicadas no BOOT da aplicacao.

Nunca no build: o volume da Railway e montado no start do container, e o que
for escrito em tempo de build se perde.

Migracoes sao aditivas e numeradas. O incremento 0 cria apenas o substrato:
configuracao versionada, o minimo de `run` que a trava de congelamento
precisa para ser real, e a sentinela que prova a persistencia do volume.
Ledger, dataset, eventos e o resto entram nos seus proprios incrementos.
"""

from __future__ import annotations

# Cada item: (versao, descricao, sql).
MIGRACOES: list[tuple[int, str, str]] = [
    (
        1,
        "substrato do incremento 0: config versionada, run minimo, sentinela",
        """
        -- ------------------------------------------------------------------
        -- Configuracao versionada (ADR 0008).
        -- A secao 10.2.3 exige alteracao "versionada no ledger, com autor,
        -- data, valor anterior e novo". Variavel de ambiente nao faz isso.
        -- ------------------------------------------------------------------
        CREATE TABLE config_version (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at          TEXT    NOT NULL,
            author              TEXT    NOT NULL,
            parent_version_id   INTEGER REFERENCES config_version(id),
            payload_json        TEXT    NOT NULL,
            config_hash         TEXT    NOT NULL,
            material            INTEGER NOT NULL CHECK (material IN (0, 1)),
            note                TEXT
        );

        CREATE INDEX idx_config_version_created
            ON config_version(created_at DESC);

        -- Log append-only: um registro por campo alterado.
        CREATE TABLE config_change (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id      INTEGER NOT NULL REFERENCES config_version(id),
            field           TEXT    NOT NULL,
            old_value_json  TEXT,
            new_value_json  TEXT,
            material        INTEGER NOT NULL CHECK (material IN (0, 1))
        );

        CREATE INDEX idx_config_change_version ON config_change(version_id);

        -- Imutabilidade imposta pelo banco, nao por disciplina.
        -- Mesma regra que o ledger tera no incremento 2: correcao e nova
        -- versao, nunca edicao.
        CREATE TRIGGER config_version_sem_update
        BEFORE UPDATE ON config_version
        BEGIN
            SELECT RAISE(ABORT,
                'config_version e imutavel: crie uma nova versao');
        END;

        CREATE TRIGGER config_version_sem_delete
        BEFORE DELETE ON config_version
        BEGIN
            SELECT RAISE(ABORT, 'config_version e append-only');
        END;

        CREATE TRIGGER config_change_sem_update
        BEFORE UPDATE ON config_change
        BEGIN
            SELECT RAISE(ABORT, 'config_change e imutavel');
        END;

        CREATE TRIGGER config_change_sem_delete
        BEFORE DELETE ON config_change
        BEGIN
            SELECT RAISE(ABORT, 'config_change e append-only');
        END;

        -- ------------------------------------------------------------------
        -- `run` minimo.
        -- Existe agora para que a trava "config congela durante run ativo"
        -- (ADR 0008) seja verificavel de verdade em vez de um stub. As demais
        -- colunas (semente, digest, dataset_hash, fidelity_level) entram nos
        -- incrementos que as usam.
        -- ------------------------------------------------------------------
        CREATE TABLE run (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id          TEXT    NOT NULL DEFAULT 'agent-0001',
            state             TEXT    NOT NULL
                                      CHECK (state IN ('pendente','executando',
                                             'pausado','concluido',
                                             'interrompido','abortado')),
            config_version_id INTEGER NOT NULL REFERENCES config_version(id),
            created_at        TEXT    NOT NULL,
            updated_at        TEXT    NOT NULL
        );

        CREATE INDEX idx_run_state ON run(state);

        -- ------------------------------------------------------------------
        -- Sentinela: prova que o volume persiste entre deploys.
        -- "Esta no volume" precisa ser provado, nao presumido.
        -- ------------------------------------------------------------------
        CREATE TABLE sentinel (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            label      TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        "incremento 1: dataset imutavel, barras e a reserva carvada no SQL",
        """
        -- ------------------------------------------------------------------
        -- Dataset ingerido uma vez e fixado.
        --
        -- `reserved_from_ms` nao e metadado decorativo: e o corte que a VIEW
        -- abaixo aplica. Carvar agora custa uma clausula WHERE; carvar depois
        -- significa que a janela ja foi vista e o reservado nasce contaminado
        -- (D11, secao 14.2).
        -- ------------------------------------------------------------------
        CREATE TABLE dataset (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            venue             TEXT    NOT NULL,
            symbol            TEXT    NOT NULL,
            timeframe         TEXT    NOT NULL,
            interval_ms       INTEGER NOT NULL CHECK (interval_ms > 0),

            -- Sempre em MILISSEGUNDOS. Os dumps da Binance mudam de ms para
            -- microssegundos em 2025-01, no meio da janela decidida; a
            -- ingestao normaliza e o banco guarda uma unidade so.
            start_ms          INTEGER NOT NULL,
            end_ms            INTEGER NOT NULL,
            reserved_from_ms  INTEGER NOT NULL,

            bars              INTEGER NOT NULL CHECK (bars > 0),

            -- Hash dos DADOS normalizados, nao dos bytes baixados. E o que
            -- torna a reingestao verificavel: recompactacao na origem mudaria
            -- o hash do zip sem mudar uma barra sequer.
            sha256            TEXT    NOT NULL,

            source            TEXT    NOT NULL,
            source_files_json TEXT    NOT NULL,
            fetched_at        TEXT    NOT NULL,

            -- Declarado aqui e propagado a todo resultado (secao 8.4.1.1).
            fidelity_level    INTEGER NOT NULL CHECK (fidelity_level >= 1),

            -- Expoente decimal dos inteiros de preco e volume. Sem isto,
            -- inteiro de precisao fixa vira numero sem unidade.
            price_scale_exp   INTEGER NOT NULL,
            volume_scale_exp  INTEGER NOT NULL,

            CHECK (start_ms < end_ms),
            CHECK (reserved_from_ms > start_ms
                   AND reserved_from_ms <= end_ms),
            UNIQUE (venue, symbol, timeframe, start_ms, end_ms)
        );

        -- ------------------------------------------------------------------
        -- Barras OHLCV.
        --
        -- Todos os valores em INTEIRO de precisao fixa (regra 5). Os dumps
        -- entregam preco como string decimal, entao a conversao vai direto
        -- para inteiro sem passar por ponto flutuante em momento algum.
        -- ------------------------------------------------------------------
        CREATE TABLE bar (
            dataset_id   INTEGER NOT NULL REFERENCES dataset(id),
            open_time_ms INTEGER NOT NULL,
            open         INTEGER NOT NULL,
            high         INTEGER NOT NULL,
            low          INTEGER NOT NULL,
            close        INTEGER NOT NULL,
            volume       INTEGER NOT NULL CHECK (volume >= 0),
            quote_volume INTEGER NOT NULL CHECK (quote_volume >= 0),
            trades       INTEGER NOT NULL CHECK (trades >= 0),

            CHECK (high >= low),
            CHECK (high >= open AND high >= close),
            CHECK (low  <= open AND low  <= close),

            PRIMARY KEY (dataset_id, open_time_ms)
        ) WITHOUT ROWID;

        -- ------------------------------------------------------------------
        -- Imutabilidade imposta pelo banco, como no ledger: um dataset fixado
        -- que pode ser editado nao esta fixado. Reingestao correta e no-op;
        -- reingestao divergente e erro, nunca sobrescrita silenciosa.
        -- ------------------------------------------------------------------
        CREATE TRIGGER dataset_sem_update
        BEFORE UPDATE ON dataset
        BEGIN
            SELECT RAISE(ABORT, 'dataset e imutavel apos a ingestao');
        END;

        CREATE TRIGGER dataset_sem_delete
        BEFORE DELETE ON dataset
        BEGIN
            SELECT RAISE(ABORT, 'dataset e append-only');
        END;

        CREATE TRIGGER bar_sem_update
        BEFORE UPDATE ON bar
        BEGIN
            SELECT RAISE(ABORT, 'bar e imutavel');
        END;

        CREATE TRIGGER bar_sem_delete
        BEFORE DELETE ON bar
        BEGIN
            SELECT RAISE(ABORT, 'bar e append-only');
        END;

        -- ------------------------------------------------------------------
        -- A RESERVA, CARVADA NO SQL.
        --
        -- Criterio 4 do incremento 1: "a restricao esta no SQL do loader, nao
        -- em uma checagem que o chamador poderia esquecer" (secao 8.4.1.2).
        --
        -- Esta view NAO CONSEGUE devolver barra reservada. Nao ha parametro,
        -- flag nem argumento que a faca devolver: o corte e parte da sua
        -- definicao. O loader do experimento le daqui e nunca de `bar`.
        -- ------------------------------------------------------------------
        CREATE VIEW bar_experimento AS
        SELECT
            b.dataset_id,
            b.open_time_ms,
            b.open,
            b.high,
            b.low,
            b.close,
            b.volume,
            b.quote_volume,
            b.trades,
            b.open_time_ms + d.interval_ms AS close_time_ms
        FROM bar b
        JOIN dataset d ON d.id = b.dataset_id
        WHERE b.open_time_ms < d.reserved_from_ms;
        """,
    ),
    (
        3,
        "incremento 2: ledger de partidas dobradas, dois livros e agent_event",
        """
        -- ==================================================================
        -- CONVENCAO DE SINAL, valida em todo o ledger:
        --
        --     amount_minor > 0  a conta RECEBE
        --     amount_minor < 0  a conta ENTREGA
        --
        -- Partidas dobradas viram entao: a soma dos lancamentos de uma
        -- transacao, DENTRO DE CADA LIVRO, e exatamente zero. E a mesma
        -- afirmacao que "soma de debitos = soma de creditos", numa forma que
        -- o banco consegue verificar sozinho.
        --
        -- Por livro, e nao no total: os dois livros estao em moedas
        -- diferentes (BRL e USD). Somar BRL com USD nao significa nada, e
        -- um "balanceamento" que atravessasse moedas esconderia erro em vez
        -- de revelar.
        --
        -- Todo valor monetario e INTEIRO de unidade menor (centavo).
        -- Nenhuma coluna monetaria em ponto flutuante (regra 5) - ha teste
        -- que le o schema e falha se aparecer REAL.
        -- ==================================================================

        CREATE TABLE account (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            code     TEXT    NOT NULL UNIQUE,
            book     TEXT    NOT NULL CHECK (book IN ('real','simulado')),
            currency TEXT    NOT NULL CHECK (currency IN ('BRL','USD')),
            kind     TEXT    NOT NULL CHECK (kind IN
                        ('ativo','patrimonio','despesa','resultado','tesouraria')),
            name     TEXT    NOT NULL,

            -- Livro e moeda andam juntos: real em BRL, simulado em USD
            -- (regra 7). Deixar isso solto permitiria uma conta em USD no
            -- livro real, e a soma por livro passaria a misturar moeda.
            CHECK ((book = 'real'     AND currency = 'BRL')
                OR (book = 'simulado' AND currency = 'USD'))
        );

        -- ------------------------------------------------------------------
        -- Uma transacao agrupa os lancamentos que se equilibram entre si.
        --
        -- Ciclo de vida: nasce ABERTA, recebe lancamentos, e e FECHADA. O
        -- fechamento e o momento em que o banco confere as partidas dobradas
        -- e congela tudo. Nao ha como fechar desequilibrada.
        -- ------------------------------------------------------------------
        CREATE TABLE ledger_transaction (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            kind          TEXT    NOT NULL CHECK (kind IN
                            ('abertura','reflexao','operacao','estorno')),
            occurred_at   TEXT    NOT NULL,
            posted_at     TEXT,          -- NULL = aberta
            run_id        INTEGER REFERENCES run(id),

            -- Ponte de cambio, gravada NO EVENTO (secao 5.1). Fixa a taxa do
            -- momento para que variacao cambial nunca seja confundida com
            -- desempenho do agente (secao 4.2).
            -- Inteiro de micros: 5,40 BRL/USD -> 5400000. Taxa vira dinheiro
            -- quando multiplicada; ponto flutuante aqui contaminaria o valor.
            fx_rate_micro    INTEGER CHECK (fx_rate_micro > 0),
            fx_rate_date     TEXT,

            -- Os dois registros se referenciam (criterio 9). O ledger e a
            -- autoridade sobre dinheiro; o fluxo de eventos, sobre decisao.
            agent_event_id   INTEGER,

            -- Correcao e SEMPRE estorno, nunca edicao (regra 6).
            reverses_transaction_id INTEGER REFERENCES ledger_transaction(id),

            memo          TEXT NOT NULL DEFAULT ''
        );

        -- Estornar duas vezes zeraria duas vezes. O banco impede.
        CREATE UNIQUE INDEX idx_estorno_unico
            ON ledger_transaction(reverses_transaction_id)
            WHERE reverses_transaction_id IS NOT NULL;

        CREATE INDEX idx_transaction_run ON ledger_transaction(run_id);
        CREATE INDEX idx_transaction_kind ON ledger_transaction(kind);

        CREATE TABLE ledger_entry (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL REFERENCES ledger_transaction(id),
            account_id     INTEGER NOT NULL REFERENCES account(id),

            -- Sinal conforme a convencao do topo. Zero e proibido: lancamento
            -- que nao move nada e ruido no historico.
            amount_minor   INTEGER NOT NULL CHECK (amount_minor <> 0),
            memo           TEXT    NOT NULL DEFAULT ''
        );

        CREATE INDEX idx_entry_transaction ON ledger_entry(transaction_id);
        CREATE INDEX idx_entry_account ON ledger_entry(account_id);

        -- ==================================================================
        -- Fluxo cognitivo. Imutavel como o ledger (regra 16).
        --
        -- Secao 10.6.2 chama os eventos de "fonte historica do agente" - um
        -- fluxo editavel nao e fonte de nada. Avaliacao posterior e evento
        -- NOVO, filho da decisao, nunca edicao dela (regra 17).
        -- ==================================================================
        CREATE TABLE agent_event (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id           INTEGER REFERENCES run(id),
            parent_event_id  INTEGER REFERENCES agent_event(id),
            occurred_at      TEXT    NOT NULL,

            -- Qual no do grafo produziu o evento, e de que tipo ele e.
            node             TEXT    NOT NULL,
            kind             TEXT    NOT NULL,

            -- Presente e INERTE na 0A (regra 18). Nenhum ramo de decisao le.
            profile_id       TEXT    NOT NULL DEFAULT 'neutro@1',

            -- O agente pede um TIER; provedor e modelo sao resolvidos pela
            -- configuracao (secao 3.9) e gravados aqui como fato do que
            -- aconteceu, nunca lidos de dentro do codigo do agente.
            tier             TEXT,
            provider         TEXT,
            model            TEXT,

            -- Lidos do `usage` da resposta, jamais estimados (secao 5.2).
            -- NULL significa QUE O PROVEDOR NAO INFORMOU. Nunca zero:
            -- "nao sei" e "foi zero" sao afirmacoes diferentes, e confundi-las
            -- corrompe o custo por decisao.
            tokens_in        INTEGER CHECK (tokens_in >= 0),
            tokens_out       INTEGER CHECK (tokens_out >= 0),
            tokens_cached    INTEGER CHECK (tokens_cached >= 0),
            usage_bruto_json TEXT,

            cost_usd_minor   INTEGER CHECK (cost_usd_minor >= 0),

            -- Declaradas ANTES da execucao (regra 17). Confianca em ppm,
            -- para nao existir ponto flutuante nem aqui.
            expectation      TEXT,
            confidence_ppm   INTEGER CHECK (confidence_ppm BETWEEN 0 AND 1000000),

            inputs_digest    TEXT,
            outputs_digest   TEXT,

            -- Contrapartida no ledger (criterio 9).
            ledger_transaction_id INTEGER REFERENCES ledger_transaction(id)
        );

        CREATE INDEX idx_event_run ON agent_event(run_id);
        CREATE INDEX idx_event_parent ON agent_event(parent_event_id);
        CREATE INDEX idx_event_transacao ON agent_event(ledger_transaction_id);

        -- ==================================================================
        -- IMUTABILIDADE E PARTIDAS DOBRADAS, IMPOSTAS PELO BANCO.
        --
        -- Nao por disciplina, nao por revisao de codigo, nao por um teste que
        -- alguem pode esquecer de rodar: por trigger.
        -- ==================================================================

        -- Uma transacao ABERTA e trabalho em andamento e pode ser ajustada.
        -- Uma transacao FECHADA e historia e nao muda mais - nunca.
        CREATE TRIGGER ledger_transaction_fechada_e_imutavel
        BEFORE UPDATE ON ledger_transaction
        BEGIN
            SELECT CASE
                WHEN OLD.posted_at IS NOT NULL THEN
                    RAISE(ABORT,
                    'transacao fechada e imutavel: corrija por estorno')

                -- O fechamento e onde as partidas dobradas sao conferidas.
                WHEN NEW.posted_at IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM ledger_entry
                                 WHERE transaction_id = OLD.id) THEN
                    RAISE(ABORT, 'transacao sem lancamento nao pode ser fechada')

                WHEN NEW.posted_at IS NOT NULL AND EXISTS (
                        SELECT 1
                        FROM ledger_entry e
                        JOIN account a ON a.id = e.account_id
                        WHERE e.transaction_id = OLD.id
                        GROUP BY a.book
                        HAVING SUM(e.amount_minor) <> 0
                    ) THEN
                    RAISE(ABORT,
                    'partidas dobradas violadas: a soma dos lancamentos de um livro nao e zero')
            END;
        END;

        CREATE TRIGGER ledger_transaction_sem_delete
        BEFORE DELETE ON ledger_transaction
        BEGIN
            SELECT RAISE(ABORT, 'ledger e apenas por acrescimo');
        END;

        -- Lancamento nunca muda. Nem em transacao aberta: se esta errado,
        -- a transacao inteira e refeita antes de fechar, ou estornada depois.
        CREATE TRIGGER ledger_entry_sem_update
        BEFORE UPDATE ON ledger_entry
        BEGIN
            SELECT RAISE(ABORT,
                'lancamento e imutavel: correcao e estorno, nunca edicao');
        END;

        CREATE TRIGGER ledger_entry_sem_delete
        BEFORE DELETE ON ledger_entry
        BEGIN
            SELECT RAISE(ABORT, 'ledger e apenas por acrescimo');
        END;

        -- Nao se acrescenta lancamento a transacao ja fechada: isso
        -- desequilibraria uma transacao que o banco ja declarou equilibrada.
        CREATE TRIGGER ledger_entry_so_em_transacao_aberta
        BEFORE INSERT ON ledger_entry
        BEGIN
            SELECT CASE WHEN (
                SELECT posted_at FROM ledger_transaction
                WHERE id = NEW.transaction_id
            ) IS NOT NULL THEN
                RAISE(ABORT, 'transacao fechada nao aceita novo lancamento')
            END;
        END;

        CREATE TRIGGER agent_event_sem_update
        BEFORE UPDATE ON agent_event
        BEGIN
            SELECT RAISE(ABORT,
                'agent_event e imutavel: avaliacao posterior e evento novo, filho da decisao');
        END;

        CREATE TRIGGER agent_event_sem_delete
        BEFORE DELETE ON agent_event
        BEGIN
            SELECT RAISE(ABORT, 'agent_event e apenas por acrescimo');
        END;

        -- ------------------------------------------------------------------
        -- Saldo por conta, derivado dos lancamentos.
        --
        -- Deriva de transacoes FECHADAS apenas: uma transacao aberta ainda
        -- nao passou pela conferencia de partidas dobradas, e incluir seus
        -- lancamentos exibiria um saldo que o banco nao garantiu.
        --
        -- Nao existe coluna de saldo em lugar nenhum. Onde ha uma fonte
        -- derivada e uma armazenada, elas divergem - e ai ha duas fontes de
        -- verdade sobre dinheiro, que a regra 16 proibe.
        -- ------------------------------------------------------------------
        CREATE VIEW account_balance AS
        SELECT
            a.id       AS account_id,
            a.code     AS code,
            a.book     AS book,
            a.currency AS currency,
            a.kind     AS kind,
            a.name     AS name,
            COALESCE((
                SELECT SUM(e.amount_minor)
                FROM ledger_entry e
                JOIN ledger_transaction t ON t.id = e.transaction_id
                WHERE e.account_id = a.id AND t.posted_at IS NOT NULL
            ), 0) AS balance_minor,
            (
                SELECT COUNT(*)
                FROM ledger_entry e
                JOIN ledger_transaction t ON t.id = e.transaction_id
                WHERE e.account_id = a.id AND t.posted_at IS NOT NULL
            ) AS entries
        FROM account a;
        """,
    ),
    (
        4,
        "incremento 3: execucoes do simulador pessimista",
        """
        -- ==================================================================
        -- Uma execucao simulada.
        --
        -- Guarda a DECOMPOSICAO, e nao so o total: o criterio 3 recusa um
        -- campo "custo" agregado. Sem separar taxa, spread, slippage e
        -- penalidade, e impossivel saber depois qual deles comeu o resultado
        -- - e essa e justamente a pergunta que a Fase 0A precisa poder fazer
        -- (secao 8.4.1).
        --
        -- Precos com a escala do dataset (1e-8). Dinheiro em centavos de USD.
        -- Quantidade de BTC em satoshis (1e-8). Tudo inteiro (regra 5).
        -- ==================================================================
        CREATE TABLE execution (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id            INTEGER NOT NULL REFERENCES run(id),
            dataset_id        INTEGER NOT NULL REFERENCES dataset(id),

            -- A latencia e estrutural: a execucao acontece numa barra
            -- POSTERIOR a da decisao, sempre (criterio 2). O CHECK abaixo
            -- torna "executou na mesma barra" impossivel, e nao apenas
            -- improvavel.
            decision_bar_ms   INTEGER NOT NULL,
            execution_bar_ms  INTEGER NOT NULL,

            side              TEXT    NOT NULL CHECK (side IN ('compra','venda')),
            quantity_sats     INTEGER NOT NULL CHECK (quantity_sats > 0),

            -- Referencia = limite ADVERSO da barra no nivel de fidelidade
            -- declarado: maxima na compra, minima na venda. Executado = a
            -- referencia piorada por spread, slippage e penalidade.
            price_ref         INTEGER NOT NULL CHECK (price_ref > 0),
            price_exec        INTEGER NOT NULL CHECK (price_exec > 0),

            notional_ref_cents INTEGER NOT NULL CHECK (notional_ref_cents > 0),
            fee_cents         INTEGER NOT NULL CHECK (fee_cents >= 0),
            spread_cents      INTEGER NOT NULL CHECK (spread_cents >= 0),
            slippage_cents    INTEGER NOT NULL CHECK (slippage_cents >= 0),
            penalty_cents     INTEGER NOT NULL CHECK (penalty_cents >= 0),

            -- Declarado em CADA execucao e propagado a todo resultado
            -- (criterio 5, secao 8.4.1.1). Nao e metadado do dataset que
            -- alguem consulta depois: viaja junto do numero.
            fidelity_level    INTEGER NOT NULL CHECK (fidelity_level >= 1),

            ledger_transaction_id INTEGER NOT NULL
                                  REFERENCES ledger_transaction(id),

            CHECK (execution_bar_ms > decision_bar_ms),

            -- Nunca favoravel (criterio 1). Na compra o executado nao pode
            -- ficar abaixo da referencia adversa; na venda, nao pode ficar
            -- acima. O banco recusa a execucao generosa.
            CHECK (
                (side = 'compra' AND price_exec >= price_ref)
             OR (side = 'venda'  AND price_exec <= price_ref)
            )
        );

        CREATE INDEX idx_execution_run ON execution(run_id);
        CREATE INDEX idx_execution_bar ON execution(execution_bar_ms);

        CREATE TRIGGER execution_sem_update
        BEFORE UPDATE ON execution
        BEGIN
            SELECT RAISE(ABORT, 'execucao e imutavel: corrija por estorno');
        END;

        CREATE TRIGGER execution_sem_delete
        BEFORE DELETE ON execution
        BEGIN
            SELECT RAISE(ABORT, 'execucao e apenas por acrescimo');
        END;

        -- ------------------------------------------------------------------
        -- Posicao corrente, derivada das execucoes.
        --
        -- Como o saldo, nao e armazenada: duas fontes de verdade sobre
        -- quanto se tem divergem, e ai nao ha como saber qual esta certa
        -- (regra 16). D1 fixou long/flat, entao o resultado e sempre >= 0.
        -- ------------------------------------------------------------------
        CREATE VIEW position_atual AS
        SELECT
            run_id,
            COALESCE(SUM(CASE WHEN side = 'compra' THEN quantity_sats
                              ELSE -quantity_sats END), 0) AS quantity_sats,
            COUNT(*) AS execucoes,
            MAX(fidelity_level) AS fidelity_level
        FROM execution
        GROUP BY run_id;
        """,
    ),
    (
        5,
        "saldo por run: cada run tem historia economica propria",
        """
        -- ==================================================================
        -- Saldo POR RUN.
        --
        -- `account_balance` soma o livro inteiro, o que esta certo para
        -- "quanto ja passou por esta conta na historia toda" - e errado para
        -- "quanto este run tem". Com contas globais, abrir um segundo run
        -- credita capital semente por cima do saldo do primeiro, e os dois
        -- passam a dividir a mesma carteira.
        --
        -- O defeito so apareceu quando o simulador rodou varios runs em
        -- sequencia: o caixa CRESCIA com mais operacoes. E ele inviabilizaria
        -- o incremento 4, onde o B1 sozinho precisa de mil historias
        -- economicas independentes.
        --
        -- Transacao sem `run_id` fica de fora: ela nao pertence a run nenhum,
        -- e atribui-la a algum seria inventar procedencia.
        -- ==================================================================
        CREATE VIEW account_balance_run AS
        SELECT
            t.run_id   AS run_id,
            a.id       AS account_id,
            a.code     AS code,
            a.book     AS book,
            a.currency AS currency,
            a.kind     AS kind,
            a.name     AS name,
            SUM(e.amount_minor) AS balance_minor,
            COUNT(e.id) AS entries
        FROM ledger_entry e
        JOIN ledger_transaction t ON t.id = e.transaction_id
        JOIN account a ON a.id = e.account_id
        WHERE t.posted_at IS NOT NULL AND t.run_id IS NOT NULL
        GROUP BY t.run_id, a.id;
        """,
    ),
    (
        6,
        "incremento 4: regra com hash, vinculo com a execucao e resultado dos baselines",
        """
        -- ==================================================================
        -- A regra que autorizou cada execucao.
        --
        -- Criterio 7 (R25.5): partindo de uma execucao qualquer, tem de ser
        -- possivel chegar a regra que a autorizou. Por isso TODA execucao
        -- aponta para uma linha daqui - inclusive as dos baselines, que
        -- tambem sao decisoes tomadas por alguma regra, ainda que trivial.
        --
        -- `kind` separa duas coisas que nao devem ser confundidas:
        --   catalogo  - familia do catalogo fechado da D5, que e o que o
        --               cerebro lento tem permissao de produzir
        --   baseline  - buy and hold e aleatorio, que nao vem do catalogo e
        --               nunca poderiam vir: nao sao hipoteses, sao controles
        --
        -- `params_json` e canonico (chaves ordenadas, sem espaco), porque o
        -- hash e do CONTEUDO. Duas regras iguais escritas em ordem diferente
        -- precisam ter o mesmo id.
        -- ==================================================================
        CREATE TABLE rule (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            hash         TEXT    NOT NULL UNIQUE,
            kind         TEXT    NOT NULL CHECK (kind IN ('catalogo','baseline')),
            family       TEXT    NOT NULL,
            params_json  TEXT    NOT NULL,

            -- Procedencia embrionaria (D5): mercado, instrumento, timeframe e
            -- fidelidade sob os quais a regra vale. Uma regra sem isso e uma
            -- afirmacao sem condicoes de validade.
            condicoes_validade_json TEXT NOT NULL,

            created_at   TEXT    NOT NULL,

            -- B3 e CONGELADO antes do primeiro run do agente (criterio 5,
            -- D4). Sem timestamp de congelamento, "congelado" e so intencao.
            frozen_at    TEXT
        );

        CREATE INDEX idx_rule_family ON rule(family);

        CREATE TRIGGER rule_sem_update
        BEFORE UPDATE ON rule
        BEGIN
            SELECT CASE
                -- Congelar e a UNICA alteracao permitida, e so uma vez.
                -- Retunar parametro depois de ver o resultado destroi o grupo
                -- de controle, e e exatamente isso que o trigger impede.
                WHEN OLD.frozen_at IS NOT NULL THEN
                    RAISE(ABORT, 'regra congelada e imutavel')
                WHEN NEW.hash <> OLD.hash
                  OR NEW.params_json <> OLD.params_json
                  OR NEW.family <> OLD.family THEN
                    RAISE(ABORT, 'so o congelamento pode ser alterado numa regra')
            END;
        END;

        CREATE TRIGGER rule_sem_delete
        BEFORE DELETE ON rule
        BEGIN
            SELECT RAISE(ABORT, 'rule e apenas por acrescimo');
        END;

        -- Vinculo execucao -> regra (criterio 7). Coluna nova em tabela
        -- existente: aditivo, e as execucoes do incremento 3 ficam com NULL,
        -- que e a verdade sobre elas - nao houve regra, foram chamadas
        -- diretas do simulador em teste.
        ALTER TABLE execution ADD COLUMN rule_id INTEGER REFERENCES rule(id);

        CREATE INDEX idx_execution_rule ON execution(rule_id);

        -- ==================================================================
        -- Resultado de uma repeticao de baseline.
        --
        -- Existe porque B1 precisa de mil repeticoes (secao 14.3) e mil
        -- historias completas no ledger seriam milhoes de lancamentos
        -- imutaveis para produzir tres numeros: p5, p50 e p95.
        --
        -- O que NAO muda: as mil repeticoes passam pelo MESMO nucleo de
        -- precificacao do simulador, com as mesmas taxas e o mesmo
        -- dimensionamento (criterio 6). O que muda e so onde o resultado e
        -- guardado. Uma repeticao representativa roda o caminho persistido
        -- inteiro, e um teste exige que os dois produzam o mesmo numero.
        -- ==================================================================
        CREATE TABLE baseline_result (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id         INTEGER NOT NULL REFERENCES run(id),
            baseline       TEXT    NOT NULL CHECK (baseline IN ('B1','B2','B3')),
            repeticao      INTEGER NOT NULL,

            -- Derivada deterministicamente da semente do run (criterio 4):
            -- a distribuicao inteira e reproduzivel a partir dela.
            seed           INTEGER NOT NULL,

            operacoes      INTEGER NOT NULL CHECK (operacoes >= 0),
            equity_final_cents INTEGER NOT NULL,
            fee_cents      INTEGER NOT NULL CHECK (fee_cents >= 0),
            spread_cents   INTEGER NOT NULL CHECK (spread_cents >= 0),
            slippage_cents INTEGER NOT NULL CHECK (slippage_cents >= 0),
            penalty_cents  INTEGER NOT NULL CHECK (penalty_cents >= 0),

            rule_id        INTEGER REFERENCES rule(id),
            fidelity_level INTEGER NOT NULL CHECK (fidelity_level >= 1),
            created_at     TEXT    NOT NULL,

            UNIQUE (run_id, baseline, repeticao)
        );

        CREATE INDEX idx_baseline_run ON baseline_result(run_id, baseline);

        CREATE TRIGGER baseline_result_sem_update
        BEFORE UPDATE ON baseline_result
        BEGIN
            SELECT RAISE(ABORT, 'resultado de baseline e imutavel');
        END;

        CREATE TRIGGER baseline_result_sem_delete
        BEFORE DELETE ON baseline_result
        BEGIN
            SELECT RAISE(ABORT, 'baseline_result e apenas por acrescimo');
        END;
        """,
    ),
    (
        7,
        "incremento 5: cerebro lento, proposta de regra e cache de respostas",
        """
        -- ==================================================================
        -- `tokens_cached` nao dizia QUAL cache, e sao dois numeros com
        -- precos diferentes: escrever no cache custa 1,25x o preco de
        -- entrada, ler custa 0,1x. Um campo so para os dois torna o custo
        -- impossivel de calcular sem estimar - e a secao 5.2 proibe estimar.
        --
        -- Renomear e mais honesto que documentar a ambiguidade: um nome que
        -- nao descreve o que guarda e a forma como este projeto ja se enganou
        -- quatro vezes.
        -- ==================================================================
        ALTER TABLE agent_event RENAME COLUMN tokens_cached TO tokens_cache_read;

        -- NULL continua significando NAO INFORMADO PELO PROVEDOR. A OpenAI
        -- nao cobra escrita de cache e nao reporta o numero: gravar zero ali
        -- afirmaria que nada foi escrito, que e coisa diferente de nao saber.
        ALTER TABLE agent_event ADD COLUMN tokens_cache_write INTEGER
            CHECK (tokens_cache_write >= 0);

        -- Qual tabela de precos converteu estes tokens em dinheiro.
        -- Redundante com a config do run DE PROPOSITO: e redundancia
        -- conferida, nao duplicada. `conferir_custo_recomputado` recalcula o
        -- custo a partir dos tokens e desta versao e compara com o gravado.
        -- Redundancia que ninguem confere e como um valor vira mentira.
        ALTER TABLE agent_event ADD COLUMN price_table_version TEXT;

        -- O custo EXATO, em micros de USD (1e-6). O ledger nao consegue
        -- representar menos que um centavo, e uma reflexao custa fracoes de
        -- centavo: postar o teto em centavos e o certo para o livro, mas
        -- guardar SO isso faria toda chamada custar "1 centavo" e apagaria
        -- exatamente a contabilidade de token que este incremento existe
        -- para provar.
        --
        -- Nao sao duas fontes de verdade sobre dinheiro (regra 16): o ledger
        -- continua sendo a autoridade sobre o que foi lancado, este campo e a
        -- medida exata do que foi consumido, e a relacao entre os dois e
        -- CONFERIDA - `conferir_arredondamento_do_custo` exige que o
        -- lancamento seja o teto em centavos deste valor.
        ALTER TABLE agent_event ADD COLUMN cost_usd_micro INTEGER
            CHECK (cost_usd_micro >= 0);

        CREATE INDEX idx_event_node ON agent_event(node);

        -- ==================================================================
        -- A proposta de regra do cerebro lento.
        --
        -- Existe separada de `rule` porque uma proposta REJEITADA nao vira
        -- regra nenhuma e mesmo assim precisa ficar registrada (criterio 2).
        -- Guardar so o que deu certo transforma o historico num relatorio de
        -- sucesso, e o unico jeito de diagnosticar um modelo que responde
        -- fora do schema e ter a resposta que ele deu.
        -- ==================================================================
        CREATE TABLE rule_proposal (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id         INTEGER NOT NULL REFERENCES run(id),

            -- O evento cognitivo que a produziu. E o `decision_id` do
            -- criterio 1: da proposta se chega ao custo, e do custo a
            -- proposta, nos dois sentidos.
            agent_event_id INTEGER NOT NULL REFERENCES agent_event(id),

            proposed_at    TEXT    NOT NULL,
            status         TEXT    NOT NULL
                                   CHECK (status IN ('aceita','rejeitada')),

            -- A resposta CRUA do modelo, sempre - inclusive invalida.
            raw_response_json TEXT NOT NULL,

            rejection_reason  TEXT,
            rule_id           INTEGER REFERENCES rule(id),

            -- Declaradas ANTES de qualquer execucao (regra 17, criterio 10).
            -- Confianca em ppm: nao ha ponto flutuante nem aqui.
            expectation    TEXT,
            confidence_ppm INTEGER
                           CHECK (confidence_ppm BETWEEN 0 AND 1000000),

            -- A janela que o cerebro OBSERVOU para propor, em ms de abertura
            -- de barra. Guardada para que a sobreposicao com a janela
            -- executada seja CALCULAVEL, e nao uma afirmacao em prosa que
            -- envelhece sozinha.
            observed_from_ms INTEGER,
            observed_to_ms   INTEGER,

            -- Aceita tem regra e nao tem motivo de recusa; rejeitada tem
            -- motivo e nao tem regra. Nunca as duas coisas, nunca nenhuma.
            CHECK ((status = 'aceita'    AND rule_id IS NOT NULL
                                         AND rejection_reason IS NULL)
                OR (status = 'rejeitada' AND rule_id IS NULL
                                         AND rejection_reason IS NOT NULL))
        );

        CREATE INDEX idx_proposal_run ON rule_proposal(run_id);
        CREATE INDEX idx_proposal_event ON rule_proposal(agent_event_id);

        CREATE TRIGGER rule_proposal_sem_update
        BEFORE UPDATE ON rule_proposal
        BEGIN
            SELECT RAISE(ABORT,
                'proposta e imutavel: reavaliacao e proposta nova');
        END;

        CREATE TRIGGER rule_proposal_sem_delete
        BEFORE DELETE ON rule_proposal
        BEGIN
            SELECT RAISE(ABORT, 'rule_proposal e apenas por acrescimo');
        END;

        -- ==================================================================
        -- Cache de respostas do provedor (criterio 4).
        --
        -- Enderecado por CONTEUDO: a chave e o hash do pedido inteiro -
        -- provedor, modelo, sistema, mensagem, schema e parametros. Trocar
        -- qualquer byte do pedido troca a chave, entao um acerto de cache so
        -- acontece quando o pedido e literalmente o mesmo. E o que permite
        -- reexecutar um run com custo adicional de R$0,00 sem que o cache
        -- possa devolver a resposta de outra pergunta.
        --
        -- NAO e historia, e memoria: esvaziar o cache nao apaga registro
        -- nenhum, so faz o proximo run pagar de novo. Por isso aceita DELETE
        -- e recusa UPDATE - a mesma chave nao pode passar a devolver outra
        -- resposta sem que a chave mude junto.
        -- ==================================================================
        CREATE TABLE llm_cache (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            key            TEXT    NOT NULL UNIQUE,
            provider       TEXT    NOT NULL,
            model          TEXT    NOT NULL,
            request_json   TEXT    NOT NULL,
            response_json  TEXT    NOT NULL,
            usage_json     TEXT    NOT NULL,
            cost_usd_minor INTEGER NOT NULL CHECK (cost_usd_minor >= 0),
            created_at     TEXT    NOT NULL
        );

        CREATE TRIGGER llm_cache_sem_update
        BEFORE UPDATE ON llm_cache
        BEGIN
            SELECT RAISE(ABORT,
                'entrada de cache e enderecada por conteudo: mude a chave, nao a resposta');
        END;
        """,
    ),
    (
        8,
        "incremento 7: avaliacao posterior como evento filho da decisao",
        """
        -- ==================================================================
        -- A avaliacao posterior e EVENTO NOVO (R25.3, regra 17).
        --
        -- Expectativa e confianca sao declaradas ANTES da execucao e ficam na
        -- decisao. Comparar depois o esperado com o realizado e informacao
        -- nova - e informacao nova nao entra editando o passado: entra como
        -- evento filho. `agent_event` ja recusa UPDATE e DELETE por gatilho
        -- desde a migracao 3, entao a alternativa nem esta disponivel.
        --
        -- Coluna propria, e nao `usage_bruto_json`: aquele campo descreve o
        -- consumo que o provedor informou. Guardar avaliacao dentro dele
        -- poria um valor a descrever coisa diferente do seu nome, que e a
        -- forma exata como este projeto ja se enganou cinco vezes.
        -- ==================================================================
        ALTER TABLE agent_event ADD COLUMN evaluation_json TEXT;

        -- As duas metades da invariante do R25.3, impostas pelo BANCO.
        --
        -- Um evento de avaliacao sem pai nao e avaliacao de nada, e um sem
        -- payload nao registra o que foi comparado. Deixar isso por conta da
        -- disciplina do modulo Python significaria que um defeito ali
        -- mascararia a ausencia da regra aqui - o mesmo motivo pelo qual as
        -- partidas dobradas moram em gatilho.
        CREATE TRIGGER avaliacao_exige_pai_e_payload
        BEFORE INSERT ON agent_event
        WHEN NEW.kind = 'avaliacao'
             AND (NEW.parent_event_id IS NULL OR NEW.evaluation_json IS NULL)
        BEGIN
            SELECT RAISE(ABORT,
                'avaliacao e evento filho da decisao e carrega o que comparou');
        END;

        CREATE TRIGGER evaluation_json_so_em_avaliacao
        BEFORE INSERT ON agent_event
        WHEN NEW.evaluation_json IS NOT NULL AND NEW.kind <> 'avaliacao'
        BEGIN
            SELECT RAISE(ABORT,
                'evaluation_json pertence ao evento de avaliacao');
        END;

        CREATE INDEX idx_event_kind ON agent_event(kind);
        """,
    ),
]

# Estados em que um run bloqueia alteracao de configuracao.
ESTADOS_ATIVOS = ("executando", "pausado")
