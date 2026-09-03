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
    (
        9,
        "incremento 8: pre-registro imutavel de hipotese (secao 8.2)",
        """
        -- ==================================================================
        -- PRE-REGISTRO DE HIPOTESE - os dez campos da secao 8.2.
        --
        -- Na 0A a intencao do agente era `expectation`: uma frase em
        -- linguagem natural, do tipo "espero entre 3 e 8 operacoes e
        -- desempenho abaixo do buy-and-hold". Aquilo bastava para a pergunta
        -- da 0A, e nao basta para a da 0B. R51 exige separar REJEITADO de
        -- INCONCLUSIVO, e R33 exige condicoes de falseamento declaradas -
        -- nenhum dos dois e computavel sobre prosa.
        --
        -- Esta tabela e o que substitui aquela frase. Ela nao a apaga:
        -- `rule_proposal.expectation` continua existindo e continua sendo
        -- gravado, porque os runs da 0A foram feitos sob ele e precisam
        -- continuar legiveis exatamente como foram.
        --
        -- "O pre-registro e imutavel. Isso elimina a possibilidade de o
        -- agente ajustar a metrica depois de ver o resultado." - secao 8.2.
        -- Imutavel aqui quer dizer imposto por GATILHO, como o ledger: se
        -- dependesse da disciplina do modulo Python, um defeito ali
        -- mascararia a ausencia da regra aqui.
        -- ==================================================================
        CREATE TABLE hypothesis (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id         INTEGER NOT NULL REFERENCES run(id),

            -- O evento cognitivo que a propos. Mesmo vinculo de ida e volta
            -- que `rule_proposal` ja tem: da hipotese se chega ao custo da
            -- decisao que a produziu, e do custo a hipotese.
            agent_event_id INTEGER NOT NULL REFERENCES agent_event(id),

            -- ---------------------------------------------- secao 8.2, 1-4
            -- `id` e a chave acima. `timestamp_registro` e imutavel por
            -- consequencia: a tabela inteira recusa UPDATE.
            enunciado          TEXT NOT NULL CHECK (length(enunciado) > 0),
            agente_origem      TEXT NOT NULL CHECK (length(agente_origem) > 0),
            timestamp_registro TEXT NOT NULL,

            -- ------------------------------------------------ secao 8.2, 5
            -- "Uma UNICA metrica, definida antes do teste." Enum fechado de
            -- proposito: metrica livre por hipotese tornaria a familia
            -- estatistica incoerente, porque BY ordena p-valores que
            -- precisam medir a mesma coisa.
            metrica_primaria TEXT NOT NULL
                CHECK (metrica_primaria IN (
                    'patrimonio_final_cents',
                    'excesso_sobre_b1_p50_cents',
                    'excesso_sobre_b2_cents',
                    'excesso_sobre_b3_cents',
                    'idas_e_voltas'
                )),

            -- ------------------------------------------------ secao 8.2, 6
            -- Tamanho de efeito minimo que importa ECONOMICAMENTE, na
            -- unidade da metrica primaria. Inteiro, como todo o resto
            -- (regra 5): a metrica acaba multiplicando dinheiro.
            efeito_minimo INTEGER NOT NULL,

            -- ------------------------------------------------ secao 8.2, 7
            -- CALCULADO por poder estatistico, nunca escolhido (R34), a
            -- partir do Sharpe declarado. Em observacoes EFETIVAS, nao
            -- brutas - secao 8.3: "mil candles autocorrelacionados nao
            -- equivalem a mil observacoes independentes".
            n_minimo INTEGER NOT NULL CHECK (n_minimo > 0),
            sharpe_esperado_milesimos INTEGER NOT NULL
                CHECK (sharpe_esperado_milesimos > 0),

            -- ------------------------------------------------ secao 8.2, 8
            criterio_parada TEXT NOT NULL
                CHECK (criterio_parada IN (
                    'fim_da_janela',
                    'n_minimo_alcancado',
                    'falseamento_observado'
                )),

            -- ------------------------------------------------ secao 8.2, 9
            -- Mercado, ativo, timeframe, regime, liquidez, horario, nivel de
            -- fidelidade. Vem da CONFIG, nunca do modelo - deixa-lo declarar
            -- sob que condicoes a propria hipotese vale seria deixa-lo
            -- carimbar a propria procedencia. Mesmo motivo pelo qual
            -- `condicoes_validade` ficou fora do contrato de saida na 0A.
            condicoes_validade_json TEXT NOT NULL
                CHECK (json_valid(condicoes_validade_json)),

            -- ----------------------------------------------- secao 8.2, 10
            -- "O campo de falseamento e OBRIGATORIO. Uma hipotese que nao
            -- pode ser refutada nao entra no sistema."
            --
            -- NOT NULL sozinho nao cumpre isso: '' e '[]' passam por NOT
            -- NULL e nao refutam nada. A exigencia real e "existe ao menos
            -- uma clausula", e e o banco que a impoe.
            condicoes_falseamento_json TEXT NOT NULL
                CHECK (json_valid(condicoes_falseamento_json)
                       AND json_type(condicoes_falseamento_json) = 'array'
                       AND json_array_length(condicoes_falseamento_json) >= 1),

            -- ------------------------------------------------------ estado
            -- Secao 8.3: "Uma hipotese cujo `n_minimo` nao e alcancavel no
            -- horizonte disponivel e marcada como NAO TESTAVEL e arquivada,
            -- em vez de ser testada mal."
            --
            -- O que "arquivada" GATEIA esta em D33 (ADR 0020), e nao e
            -- obvio: ela nao impede a execucao retrospectiva, impede a
            -- PROMOCAO. O motivo da secao 8.3 e capacidade de observacao no
            -- forward, e o forward e 0C. Recusar a execucao aqui faria
            -- "arquivada" e "refutada" terem o mesmo efeito pratico, que e a
            -- confusao que a secao 14.4 nomeia.
            --
            -- O motivo e obrigatorio quando ela nasce nao testavel, e
            -- PROIBIDO quando nao. Um motivo sobrando descreveria uma
            -- condicao que nao existe - e um faltando deixaria "nao sei" e
            -- "nao" com a mesma aparencia.
            testavel            INTEGER NOT NULL CHECK (testavel IN (0, 1)),
            motivo_nao_testavel TEXT,
            horizonte_barras    INTEGER NOT NULL CHECK (horizonte_barras >= 0),

            -- A regra que esta hipotese propoe executar, quando ha uma.
            rule_id INTEGER REFERENCES rule(id),

            -- Correcao e registro NOVO que substitui o anterior, nunca
            -- edicao - o mesmo desenho do estorno no ledger. Uma hipotese
            -- nao substitui a si mesma.
            supersedes INTEGER REFERENCES hypothesis(id)
                       CHECK (supersedes IS NULL OR supersedes <> id),

            -- Do CONTEUDO: duas hipoteses identicas escritas em ordem
            -- diferente tem o mesmo hash. E o que permite reconhecer o
            -- reteste da MESMA hipotese (3 creditos, secao 8.6.1) em vez de
            -- confia-lo a boa memoria de alguem.
            content_hash TEXT NOT NULL,

            -- Restricoes de tabela ficam no fim porque o SQLite nao aceita
            -- coluna depois delas.
            --
            -- O motivo e obrigatorio quando a hipotese nasce nao testavel, e
            -- PROIBIDO quando nao. Um motivo sobrando descreveria condicao
            -- que nao existe; um faltando deixaria "nao sei" e "nao" com a
            -- mesma aparencia.
            CHECK ((testavel = 1 AND motivo_nao_testavel IS NULL)
                OR (testavel = 0 AND motivo_nao_testavel IS NOT NULL
                                 AND length(motivo_nao_testavel) > 0))
        );

        CREATE INDEX idx_hypothesis_run ON hypothesis(run_id);
        CREATE INDEX idx_hypothesis_event ON hypothesis(agent_event_id);
        CREATE INDEX idx_hypothesis_hash ON hypothesis(content_hash);

        -- As duas metades da imutabilidade da secao 8.2, no banco.
        CREATE TRIGGER hypothesis_sem_update
        BEFORE UPDATE ON hypothesis
        BEGIN
            SELECT RAISE(ABORT,
                'pre-registro e imutavel: correcao e hipotese nova com supersedes');
        END;

        CREATE TRIGGER hypothesis_sem_delete
        BEFORE DELETE ON hypothesis
        BEGIN
            SELECT RAISE(ABORT,
                'hypothesis e apenas por acrescimo: tentativa descartada e a origem exata das falsas descobertas (secao 8.6)');
        END;

        -- `n_minimo` nao pode nascer maior que o horizonte SEM que a hipotese
        -- esteja marcada nao testavel. E a metade da triagem da secao 8.3 que
        -- o banco consegue impor: se o calculo de poder for contornado algum
        -- dia, a linha nao entra.
        CREATE TRIGGER hypothesis_triagem_coerente
        BEFORE INSERT ON hypothesis
        WHEN NEW.n_minimo > NEW.horizonte_barras AND NEW.testavel = 1
        BEGIN
            SELECT RAISE(ABORT,
                'n_minimo excede o horizonte: a hipotese e NAO TESTAVEL (secao 8.3) e precisa nascer marcada como tal');
        END;
        """,
    ),
    (
        10,
        "incremento 9: quatro conjuntos por finalidade e o holdout selado",
        """
        -- ==================================================================
        -- SEPARACAO DE DADOS POR FINALIDADE (secao 8.5.1).
        --
        -- A 0A tinha dois conjuntos: o que o experimento le (`bar_experimento`)
        -- e a reserva, carvada na ingestao (D11). A secao 8.5.1 pede QUATRO:
        --
        --   Exploracao     | Agente               | conhecer o mercado
        --   In-sample      | Agente e simulador   | desenvolver e ajustar
        --   Walk-forward   | SO o Validador       | decisoes sequenciais
        --   Holdout selado | SO o Validador       | teste final, uso unico
        --
        -- A reserva da D11 NAO passa a ser o holdout: ela SEMPRE foi, e agora
        -- ganha o nome e a permissao. O corte e o mesmo `reserved_from_ms`,
        -- intocado desde a ingestao - e um teste prova que o intervalo e
        -- identico ao carvado la.
        --
        -- "A separacao e garantida pela ESTRUTURA DE DADOS e pelas permissoes
        -- da ferramenta, nao pela disciplina do agente (...) Um holdout que
        -- depende de boa vontade ja foi consumido." - secao 8.5.1
        -- ==================================================================
        CREATE TABLE dataset_split (
            dataset_id INTEGER NOT NULL REFERENCES dataset(id),

            finalidade TEXT NOT NULL
                CHECK (finalidade IN (
                    'exploracao', 'in_sample', 'walk_forward', 'holdout'
                )),

            -- Semiaberto [from, to): o fim de um conjunto e o inicio do
            -- seguinte, sem barra em dois lugares nem barra em nenhum.
            from_ms         INTEGER NOT NULL,
            to_ms_exclusive INTEGER NOT NULL,
            bars            INTEGER NOT NULL CHECK (bars >= 0),

            -- Quem pode ler. Nao e documentacao: `carregar` recusa finalidade
            -- cujo acesso nao inclui quem pede, e o holdout tem caminho
            -- proprio.
            acesso TEXT NOT NULL
                CHECK (acesso IN ('agente', 'validador')),

            PRIMARY KEY (dataset_id, finalidade),
            CHECK (from_ms < to_ms_exclusive)
        );

        CREATE INDEX idx_split_dataset ON dataset_split(dataset_id);

        CREATE TRIGGER dataset_split_sem_update
        BEFORE UPDATE ON dataset_split
        BEGIN
            SELECT RAISE(ABORT,
                'a divisao por finalidade e fixada na ingestao: mover a fronteira depois e contaminar o conjunto do outro lado');
        END;

        CREATE TRIGGER dataset_split_sem_delete
        BEFORE DELETE ON dataset_split
        BEGIN
            SELECT RAISE(ABORT, 'dataset_split e apenas por acrescimo');
        END;

        -- O holdout nao pode ser lido pelo agente, e a marcacao vive no dado.
        CREATE TRIGGER holdout_e_do_validador
        BEFORE INSERT ON dataset_split
        WHEN NEW.finalidade IN ('holdout', 'walk_forward')
             AND NEW.acesso <> 'validador'
        BEGIN
            SELECT RAISE(ABORT,
                'walk-forward e holdout sao do validador (secao 8.5.1); marca-los como do agente seria entregar a separacao a disciplina');
        END;

        -- ------------------------------------------------------------------
        -- A VIEW por finalidade.
        --
        -- Le de `bar`, e nao de `bar_experimento`: aquela ja corta a reserva,
        -- e o holdout E a reserva. Ler dali tornaria o holdout inalcancavel
        -- ate para o validador, e a separacao viraria ausencia.
        --
        -- Toda leitura por esta view CARREGA a finalidade na linha. Nao ha
        -- como ler uma barra daqui sem saber de que conjunto ela veio.
        -- ------------------------------------------------------------------
        CREATE VIEW bar_por_finalidade AS
        SELECT
            b.dataset_id,
            s.finalidade,
            s.acesso,
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
        JOIN dataset d       ON d.id = b.dataset_id
        JOIN dataset_split s ON s.dataset_id = b.dataset_id
                            AND b.open_time_ms >= s.from_ms
                            AND b.open_time_ms <  s.to_ms_exclusive;

        -- ==================================================================
        -- USO UNICO DO HOLDOUT (R28, secoes 8.4 e 8.5.1).
        --
        -- "Out-of-sample: reservado, usado uma unica vez por hipotese."
        --
        -- `UNIQUE (hypothesis_id)` e a regra inteira. Nao ha contador em
        -- Python, nao ha flag que alguem esqueca de conferir: a SEGUNDA
        -- leitura nao entra na tabela, e sem linha na tabela nao ha leitura.
        --
        -- Append-only como o resto: apagar o registro de um uso e a forma
        -- exata de reusar o dado mais escasso do sistema sem que nada acuse.
        -- ==================================================================
        CREATE TABLE holdout_access (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis_id INTEGER NOT NULL UNIQUE REFERENCES hypothesis(id),
            dataset_id    INTEGER NOT NULL REFERENCES dataset(id),
            requested_at  TEXT    NOT NULL,

            -- Quem pediu, e por que. A secao 8.5.1 exige que o acesso declare
            -- finalidade e custo; o custo em creditos e cobrado no incremento
            -- 11, e o campo ja existe para nao precisar de retrofit.
            solicitante   TEXT    NOT NULL CHECK (solicitante = 'validador'),
            finalidade    TEXT    NOT NULL CHECK (length(finalidade) > 0),
            creditos      INTEGER NOT NULL CHECK (creditos >= 0),

            barras_lidas  INTEGER NOT NULL CHECK (barras_lidas >= 0)
        );

        CREATE TRIGGER holdout_access_sem_update
        BEFORE UPDATE ON holdout_access
        BEGIN
            SELECT RAISE(ABORT,
                'o registro de uso do holdout e imutavel: e ele que prova que o periodo selado foi consumido uma vez so');
        END;

        CREATE TRIGGER holdout_access_sem_delete
        BEFORE DELETE ON holdout_access
        BEGIN
            SELECT RAISE(ABORT,
                'apagar o uso do holdout e reusar o dado mais escasso do sistema sem que nada acuse');
        END;

        -- ==================================================================
        -- JANELAS DE WALK-FORWARD (R30, secoes 8.4 e 8.5.1).
        --
        -- Geradas, gravadas e reproduziveis. Purga e embargo sao gravados em
        -- CADA janela e nao lidos de uma constante: se o catalogo ganhar uma
        -- familia de lookback maior, a purga muda, e uma janela antiga tem de
        -- continuar dizendo sob que purga ELA foi construida.
        -- ==================================================================
        CREATE TABLE walk_forward_window (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset_id INTEGER NOT NULL REFERENCES dataset(id),
            ordem      INTEGER NOT NULL CHECK (ordem >= 1),

            treino_de_ms    INTEGER NOT NULL,
            treino_ate_ms   INTEGER NOT NULL,
            teste_de_ms     INTEGER NOT NULL,
            teste_ate_ms    INTEGER NOT NULL,

            purga_barras   INTEGER NOT NULL CHECK (purga_barras >= 0),
            embargo_barras INTEGER NOT NULL CHECK (embargo_barras >= 0),

            -- De onde a purga saiu. Sem isto, "purga 400" nao diz se veio do
            -- catalogo ou de alguem digitando - e o numero para de descrever
            -- no dia em que o catalogo mudar.
            purga_origem TEXT NOT NULL CHECK (length(purga_origem) > 0),

            created_at TEXT NOT NULL,

            UNIQUE (dataset_id, ordem),

            -- Treino termina ANTES do teste comecar, sempre. O intervalo
            -- entre os dois e a purga mais o embargo.
            CHECK (treino_de_ms  < treino_ate_ms),
            CHECK (teste_de_ms   < teste_ate_ms),
            CHECK (treino_ate_ms <= teste_de_ms)
        );

        CREATE INDEX idx_wf_dataset ON walk_forward_window(dataset_id, ordem);

        CREATE TRIGGER walk_forward_window_sem_update
        BEFORE UPDATE ON walk_forward_window
        BEGIN
            SELECT RAISE(ABORT,
                'janela de walk-forward e imutavel: mover a fronteira depois de ver o resultado e o vazamento que ela existe para impedir');
        END;

        CREATE TRIGGER walk_forward_window_sem_delete
        BEFORE DELETE ON walk_forward_window
        BEGIN
            SELECT RAISE(ABORT, 'walk_forward_window e apenas por acrescimo');
        END;
        """,
    ),
    (
        11,
        "incremento 10: maquina de estados do conhecimento e o validador",
        """
        -- ==================================================================
        -- ESTADOS DO CONHECIMENTO (secao 8.1).
        --
        -- "Nenhum estado pode ser pulado. Um agente nao pode promover a
        -- propria hipotese; a promocao e feita pelo modulo validador, que e
        -- independente do agente."
        --
        -- **Log de transicoes, e nao coluna em `hypothesis`.** O pre-registro
        -- e imutavel desde a migracao 9 - nao existe UPDATE nele. Um estado
        -- que muda precisaria de UPDATE, entao o estado corrente e DERIVADO
        -- da ultima transicao. Mesmo desenho do saldo, que sai do ledger e
        -- nao de uma coluna (regra 16): duas fontes de verdade sobre o
        -- estado divergiriam no dia em que alguem esquecesse de atualizar uma.
        -- ==================================================================
        CREATE TABLE transicao_legal (
            de   TEXT NOT NULL,
            para TEXT NOT NULL,
            PRIMARY KEY (de, para)
        );

        -- O grafo da secao 8.1, como DADO. Em tabela, e nao numa cadeia de
        -- CASE dentro do gatilho: a lista de transicoes validas e a coisa
        -- mais provavel de mudar entre fases, e mudar dado e mais barato e
        -- mais visivel que mudar logica escondida num trigger.
        INSERT INTO transicao_legal (de, para) VALUES
            -- o caminho principal
            ('hipotese_registrada', 'candidata'),
            ('candidata',           'em_quarentena'),
            ('em_quarentena',       'conhecimento_validado'),
            -- monitoramento continuo, depois de validado
            ('conhecimento_validado', 'revalidado'),
            ('conhecimento_validado', 'condicionado'),
            ('conhecimento_validado', 'em_suspeita'),
            ('conhecimento_validado', 'invalidado'),
            ('revalidado',            'em_suspeita'),
            ('revalidado',            'invalidado'),
            ('condicionado',          'em_suspeita'),
            ('condicionado',          'invalidado'),
            ('em_suspeita',           'revalidado'),
            ('em_suspeita',           'invalidado'),
            -- Saidas por REFUTACAO, antes de validar. A secao 8.1 desenha a
            -- seta de INVALIDADO saindo so do monitoramento; a secao 14.4
            -- exige o desfecho "rejeitado" tambem antes disso, e a secao 8.6
            -- exige que toda tentativa fique registrada, inclusive as que
            -- falharam. Sem estas duas linhas, uma hipotese refutada no
            -- in-sample nao teria para onde ir - e ficaria parada em
            -- `hipotese_registrada`, indistinguivel de uma que nunca foi
            -- testada.
            ('hipotese_registrada', 'invalidado'),
            ('candidata',           'invalidado'),
            -- Triagem da secao 8.3: nao testavel e ARQUIVADA, e terminal.
            ('hipotese_registrada', 'nao_testavel');

        CREATE TABLE hypothesis_state (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis_id INTEGER NOT NULL REFERENCES hypothesis(id),

            -- Ordem dentro da hipotese. Torna a sequencia reconstruivel sem
            -- depender do id global, que mistura hipoteses.
            seq INTEGER NOT NULL CHECK (seq >= 1),

            from_state TEXT,
            state      TEXT NOT NULL
                CHECK (state IN (
                    'hipotese_registrada', 'candidata', 'em_quarentena',
                    'conhecimento_validado', 'revalidado', 'condicionado',
                    'em_suspeita', 'invalidado', 'nao_testavel'
                )),

            occurred_at TEXT NOT NULL,

            -- QUEM promoveu. A secao 8.1 e literal: "um agente nao pode
            -- promover a propria hipotese". O CHECK aqui e a metade da
            -- garantia que o banco consegue dar sozinho; a outra metade e a
            -- fronteira de importacao, verificada por AST.
            promoted_by TEXT NOT NULL CHECK (promoted_by = 'validador'),

            -- O que sustentou a transicao. Obrigatorio e nao vazio: uma
            -- promocao sem evidencia registrada e indistinguivel de uma
            -- promocao por engano, e e justamente a que o Portao A precisa
            -- ser capaz de pegar.
            evidence_json TEXT NOT NULL
                CHECK (json_valid(evidence_json)
                       AND json_type(evidence_json) = 'object'),

            UNIQUE (hypothesis_id, seq)
        );

        CREATE INDEX idx_hstate_hyp ON hypothesis_state(hypothesis_id, seq);
        CREATE INDEX idx_hstate_state ON hypothesis_state(state);

        CREATE TRIGGER hypothesis_state_sem_update
        BEFORE UPDATE ON hypothesis_state
        BEGIN
            SELECT RAISE(ABORT,
                'transicao de estado e imutavel: corrija com uma transicao nova, como o estorno no ledger');
        END;

        CREATE TRIGGER hypothesis_state_sem_delete
        BEFORE DELETE ON hypothesis_state
        BEGIN
            SELECT RAISE(ABORT,
                'apagar transicao e apagar a historia que prova que nenhum estado foi pulado');
        END;

        -- ------------------------------------------------------------------
        -- As tres metades da invariante "nenhum estado pode ser pulado".
        -- ------------------------------------------------------------------

        -- 1. A entrada e sempre por `hipotese_registrada`, e vinda do nada.
        --
        -- IDEIA existe na secao 8.1 e NAO e persistida: antes do pre-registro
        -- nao ha hipotese a que atribuir estado - a secao 8.2 diz que ela e
        -- gravada NO pre-registro. Gravar 'ideia' e 'hipotese_registrada' no
        -- mesmo instante seria uma transicao que nunca falha e nada informa.
        -- Declarado aqui em vez de resolvido em silencio.
        CREATE TRIGGER estado_entra_por_registrada
        BEFORE INSERT ON hypothesis_state
        WHEN NEW.seq = 1
             AND (NEW.from_state IS NOT NULL
                  OR NEW.state <> 'hipotese_registrada')
        BEGIN
            SELECT RAISE(ABORT,
                'toda hipotese entra na maquina por hipotese_registrada, sem estado anterior (secao 8.1)');
        END;

        -- 2. A transicao parte do estado ATUAL, e nao de um estado antigo.
        --
        -- Sem isto, daria para promover de `hipotese_registrada` para
        -- `candidata` uma hipotese que ja esta em `invalidado`, bastando
        -- declarar o `from_state` conveniente. Pular estado nao seria o unico
        -- jeito de burlar a maquina - voltar no tempo tambem seria.
        CREATE TRIGGER estado_parte_do_atual
        BEFORE INSERT ON hypothesis_state
        WHEN NEW.seq > 1
             AND NEW.from_state IS NOT (
                 SELECT state FROM hypothesis_state
                 WHERE hypothesis_id = NEW.hypothesis_id
                 ORDER BY seq DESC LIMIT 1
             )
        BEGIN
            SELECT RAISE(ABORT,
                'a transicao precisa partir do estado atual da hipotese, e nao de um estado ja superado');
        END;

        -- 3. O par (de, para) precisa existir no grafo da secao 8.1.
        CREATE TRIGGER estado_nao_pula
        BEFORE INSERT ON hypothesis_state
        WHEN NEW.from_state IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM transicao_legal
                 WHERE de = NEW.from_state AND para = NEW.state
             )
        BEGIN
            SELECT RAISE(ABORT,
                'transicao inexistente no grafo da secao 8.1: nenhum estado pode ser pulado');
        END;

        -- ------------------------------------------------------------------
        -- O estado corrente, DERIVADO. Nunca armazenado.
        -- ------------------------------------------------------------------
        CREATE VIEW hypothesis_estado_atual AS
        SELECT
            h.id AS hypothesis_id,
            h.agente_origem,
            h.content_hash,
            COALESCE(
                (SELECT s.state FROM hypothesis_state s
                  WHERE s.hypothesis_id = h.id
                  ORDER BY s.seq DESC LIMIT 1),
                'sem_estado'
            ) AS estado,
            (SELECT s.occurred_at FROM hypothesis_state s
              WHERE s.hypothesis_id = h.id
              ORDER BY s.seq DESC LIMIT 1) AS desde,
            (SELECT COUNT(*) FROM hypothesis_state s
              WHERE s.hypothesis_id = h.id) AS transicoes
        FROM hypothesis h;

        -- ==================================================================
        -- CONTADOR GLOBAL DE TENTATIVAS (R37, secao 8.6).
        --
        -- "O sistema mantem um contador global de hipoteses testadas por
        -- especialidade. Esse contador NUNCA e zerado."
        --
        -- View, e nao coluna. Um numero armazenado poderia ser zerado por
        -- UPDATE, e a secao 8.6 diz que "descartar tentativas fracassadas do
        -- registro e o mecanismo exato que produz falsas descobertas".
        -- Derivar de uma tabela append-only torna zerar impossivel em vez de
        -- proibido.
        --
        -- Conta hipoteses REGISTRADAS, nao promovidas: e o numero que alimenta
        -- o DSR, e o DSR desconta por tentativas, nao por sucessos.
        -- ==================================================================
        CREATE VIEW tentativas_por_especialidade AS
        SELECT
            agente_origem AS especialidade,
            COUNT(*)      AS tentativas,
            COUNT(DISTINCT content_hash) AS hipoteses_distintas,
            SUM(CASE WHEN testavel = 0 THEN 1 ELSE 0 END) AS nao_testaveis
        FROM hypothesis
        GROUP BY agente_origem;
        """,
    ),
    (
        12,
        "incremento 11: familia fechada com teto, e creditos de teste",
        """
        -- ==================================================================
        -- FAMILIA FECHADA, COM O TETO IMPOSTO PELO BANCO (R38, secao 8.6).
        --
        -- "Numero maximo de hipoteses: fixado antes de comecar, NAO
        -- AJUSTAVEL DURANTE."
        --
        -- A hipotese de numero 49 e RECUSADA, nunca truncada em silencio.
        -- Truncar seria pior que recusar: o lote continuaria parecendo
        -- completo e a multiplicidade estaria subestimada, o que empurra o
        -- limiar de BY na direcao de promover.
        --
        -- O teto vem da `config_version` sob a qual o RUN foi aberto, e nao
        -- da vigente. Um lote e definido pela config que o abriu; ler a
        -- vigente faria o teto mudar no meio do lote, que e exatamente o que
        -- "nao ajustavel durante" proibe.
        --
        -- **A familia e por config_version, e o contador do DSR nao.** Trocar
        -- de config abre familia nova - e a secao 10.2.3 ja diz que mudanca
        -- material invalida toda comparacao que a atravesse, entao usar isso
        -- para comprar tentativas custa a comparacao inteira. O contador
        -- global de `tentativas_por_especialidade` continua somando TUDO, e e
        -- ele que alimenta o DSR (secao 8.6): "o contador global e registro
        -- historico (...) alimenta o calculo do DSR".
        -- ==================================================================
        CREATE TRIGGER familia_fechada_nao_estica
        BEFORE INSERT ON hypothesis
        WHEN (
            SELECT COUNT(*)
              FROM hypothesis h
              JOIN run r ON r.id = h.run_id
             WHERE r.config_version_id = (
                 SELECT config_version_id FROM run WHERE id = NEW.run_id
             )
        ) >= (
            SELECT COALESCE(
                json_extract(cv.payload_json, '$.familia_max_hipoteses'), 48
            )
              FROM config_version cv
             WHERE cv.id = (
                 SELECT config_version_id FROM run WHERE id = NEW.run_id
             )
        )
        BEGIN
            SELECT RAISE(ABORT,
                'familia fechada cheia: o teto da config que abriu este run ja foi alcancado, e a secao 8.6 diz que ele nao e ajustavel durante o experimento');
        END;

        -- ==================================================================
        -- CREDITOS DE TESTE (R42, R43, secao 8.6.1).
        --
        -- "Tentativa estatistica e recurso escasso, consome creditos e sai do
        -- orcamento do agente."
        --
        -- **NAO e o ledger.** A regra 7 fixa DOIS livros - real em BRL e
        -- simulado em USD - e credito nao e nenhum dos dois: na Fase 0 os
        -- pesos "sao pesos administrativos iniciais, nao custos economicos
        -- demonstrados (...) servem apenas para criar escassez". Enfia-los
        -- num terceiro livro tornaria "somar o ledger" uma operacao sem
        -- significado.
        --
        -- O que se herda do ledger e a DISCIPLINA: apenas por acrescimo,
        -- saldo derivado, nunca armazenado.
        -- ==================================================================
        CREATE TABLE test_credit_budget (
            braco             TEXT    NOT NULL
                                      CHECK (braco IN ('agente', 'b4')),
            config_version_id INTEGER NOT NULL REFERENCES config_version(id),
            creditos          INTEGER NOT NULL CHECK (creditos > 0),
            created_at        TEXT    NOT NULL,

            -- Por config_version, como a familia. E o mesmo raciocinio:
            -- orcamento e propriedade do lote, nao do calendario.
            PRIMARY KEY (braco, config_version_id)
        );

        CREATE TRIGGER test_credit_budget_sem_update
        BEFORE UPDATE ON test_credit_budget
        BEGIN
            SELECT RAISE(ABORT,
                'aumentar o orcamento no meio do lote e comprar tentativas depois de ver resultado');
        END;

        CREATE TRIGGER test_credit_budget_sem_delete
        BEFORE DELETE ON test_credit_budget
        BEGIN
            SELECT RAISE(ABORT, 'test_credit_budget e apenas por acrescimo');
        END;

        CREATE TABLE test_credit_entry (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            braco             TEXT    NOT NULL
                                      CHECK (braco IN ('agente', 'b4')),
            config_version_id INTEGER NOT NULL REFERENCES config_version(id),
            hypothesis_id     INTEGER NOT NULL REFERENCES hypothesis(id),

            -- Os quatro tipos da tabela da secao 8.6.1, e so eles.
            tipo TEXT NOT NULL
                 CHECK (tipo IN ('in_sample', 'reteste_parametro',
                                 'out_of_sample', 'quarentena')),

            creditos    INTEGER NOT NULL CHECK (creditos > 0),
            occurred_at TEXT    NOT NULL,

            -- ------------------------------------------------- R43
            -- "O que deve ser medido durante a fase, para calibra-los
            -- depois" - secao 8.6.1. Os quatro numeros, gravados POR TESTE.
            --
            -- Medidos e nao estimados no fim: estimar depois exigiria supor
            -- quantos testes de cada tipo houve e quanto cada um custou, e a
            -- calibracao existe justamente porque ninguem sabe isso.
            --
            -- 1. consumo por tipo -> derivado de (tipo, creditos)
            -- 2. impacto no orcamento estatistico da especialidade
            impacto_fdr_bps   INTEGER NOT NULL CHECK (impacto_fdr_bps >= 0),
            -- 3. custo computacional real
            cpu_micros        INTEGER NOT NULL CHECK (cpu_micros >= 0),
            -- 4. custo de oportunidade do dado reservado consumido
            barras_reservadas INTEGER NOT NULL CHECK (barras_reservadas >= 0)

            -- NAO existe coluna de saldo. Duas fontes de verdade sobre
            -- quanto resta divergiriam, e ai nao havia como saber qual esta
            -- certa (regra 16). O saldo sai da view abaixo.
        );

        CREATE INDEX idx_credit_braco
            ON test_credit_entry(braco, config_version_id);
        CREATE INDEX idx_credit_hyp ON test_credit_entry(hypothesis_id);

        CREATE TRIGGER test_credit_entry_sem_update
        BEFORE UPDATE ON test_credit_entry
        BEGIN
            SELECT RAISE(ABORT,
                'consumo de credito e imutavel: e ele que prova quantas tentativas foram compradas');
        END;

        CREATE TRIGGER test_credit_entry_sem_delete
        BEFORE DELETE ON test_credit_entry
        BEGIN
            SELECT RAISE(ABORT,
                'apagar consumo de credito e devolver tentativa ja gasta, que e a forma exata de burlar a escassez da secao 8.6.1');
        END;

        -- Os pesos sao do DOCUMENTO, e nao configuraveis. O gatilho torna
        -- impossivel cobrar 1 por um out-of-sample - que consumiria dado
        -- reservado ao preco de um teste in-sample.
        CREATE TRIGGER credito_usa_o_peso_do_documento
        BEFORE INSERT ON test_credit_entry
        WHEN NEW.creditos <> (
            CASE NEW.tipo
                WHEN 'in_sample'         THEN 1
                WHEN 'reteste_parametro' THEN 3
                WHEN 'out_of_sample'     THEN 5
                WHEN 'quarentena'        THEN 10
            END
        )
        BEGIN
            SELECT RAISE(ABORT,
                'peso errado: a secao 8.6.1 fixa 1 in-sample, 3 reteste com parametro, 5 out-of-sample, 10 quarentena');
        END;

        -- Sem orcamento nao ha teste. Precisa ser gatilho proprio: a
        -- comparacao do gatilho seguinte daria NULL com orcamento ausente, e
        -- NULL nao dispara WHEN - o teste passaria de graca.
        CREATE TRIGGER credito_exige_orcamento
        BEFORE INSERT ON test_credit_entry
        WHEN NOT EXISTS (
            SELECT 1 FROM test_credit_budget
             WHERE braco = NEW.braco
               AND config_version_id = NEW.config_version_id
        )
        BEGIN
            SELECT RAISE(ABORT,
                'nao ha orcamento de creditos para este braco nesta config: testar sem orcamento e testar de graca');
        END;

        CREATE TRIGGER credito_nao_estoura_orcamento
        BEFORE INSERT ON test_credit_entry
        WHEN NEW.creditos > (
            SELECT b.creditos - COALESCE((
                SELECT SUM(e.creditos) FROM test_credit_entry e
                 WHERE e.braco = NEW.braco
                   AND e.config_version_id = NEW.config_version_id
            ), 0)
              FROM test_credit_budget b
             WHERE b.braco = NEW.braco
               AND b.config_version_id = NEW.config_version_id
        )
        BEGIN
            SELECT RAISE(ABORT,
                'creditos insuficientes: o orcamento do braco acabou, e a secao 8.6.1 existe para que acabar signifique parar');
        END;

        -- Saldo DERIVADO. Mesmo desenho de `account_balance`: uma view sobre
        -- tabela append-only nao pode divergir do que aconteceu.
        CREATE VIEW test_credit_balance AS
        SELECT
            b.braco,
            b.config_version_id,
            b.creditos AS orcamento,
            COALESCE((
                SELECT SUM(e.creditos) FROM test_credit_entry e
                 WHERE e.braco = b.braco
                   AND e.config_version_id = b.config_version_id
            ), 0) AS consumido,
            b.creditos - COALESCE((
                SELECT SUM(e.creditos) FROM test_credit_entry e
                 WHERE e.braco = b.braco
                   AND e.config_version_id = b.config_version_id
            ), 0) AS restante
        FROM test_credit_budget b;
        """,
    ),
    (
        13,
        "incremento 11b: a parada diz POR QUE parou (D35)",
        """
        -- ==================================================================
        -- O caminho registrava QUE parou e nao POR QUE.
        --
        -- `caminho_percorrido` promete: "Inclui paradas e erros. Um caminho
        -- que so mostra os runs bem-sucedidos nao e o caminho percorrido, e a
        -- metade agradavel dele." Ele incluia a parada e nao incluia o que a
        -- causou - a metade que so importa quando algo da errado.
        --
        -- O motivo existia: `_parar` o passava a `log.info` e ao resultado do
        -- POST. Sumia no GET, no export e no painel. Diagnosticar um run
        -- exigia procurar no log da plataforma, que e o mesmo que dizer que
        -- nao da para diagnosticar.
        --
        -- Decima ocorrencia do padrao deste projeto, e minha: um valor que
        -- descrevia algo, num lugar onde ninguem consegue le-lo.
        -- ==================================================================
        ALTER TABLE agent_event ADD COLUMN stop_category TEXT;
        ALTER TABLE agent_event ADD COLUMN stop_reason   TEXT;

        -- Os gatilhos sao BEFORE INSERT, e nao CHECK de tabela, de propósito:
        -- as paradas ja gravadas em producao (o run 27 entre elas) nasceram
        -- antes destas colunas e tem NULL nas duas. Um CHECK de tabela
        -- recusaria o proprio banco existente; o gatilho exige a partir daqui
        -- e deixa o passado legivel como o que ele e - registro incompleto,
        -- nao registro invalido.
        CREATE TRIGGER parada_exige_categoria_e_motivo
        BEFORE INSERT ON agent_event
        WHEN NEW.kind = 'parada'
             AND (NEW.stop_category IS NULL
                  OR NEW.stop_reason IS NULL
                  OR TRIM(NEW.stop_reason) = '')
        BEGIN
            SELECT RAISE(ABORT,
                'parada sem categoria e motivo nao registra o caminho percorrido');
        END;

        -- Lista FECHADA, e cada nome tem um caminho que o emite. Nao entra
        -- categoria "para depois": `sem_hipotese` seria a obvia a acrescentar
        -- aqui, e nao entra porque o contrato de saida EXIGE uma familia - o
        -- modelo hoje nao tem como responder "nao achei". Declarar o nome sem
        -- o caminho e exatamente o defeito `BLOCOS` do incremento 6.
        --
        -- `pedido_recusado` e `erro_schema` NAO sao a mesma coisa, e junta-las
        -- mandaria procurar no lugar errado:
        --   pedido_recusado -> o provedor recusou a NOSSA requisicao (400).
        --                      O defeito esta no que enviamos.
        --   erro_schema     -> a RESPOSTA do modelo nao bate com o contrato.
        --                      O defeito esta no que voltou.
        -- Foi exatamente essa confusao que as notas de API ja registraram uma
        -- vez, quando `max_tokens` estourado chegava disfarcado de "Invalid
        -- JSON at column 0" e mandava caçar erro de schema.
        CREATE TRIGGER categoria_de_parada_fechada
        BEFORE INSERT ON agent_event
        WHEN NEW.stop_category IS NOT NULL
             AND NEW.stop_category NOT IN (
                 'teto_atingido',
                 'pedido_recusado',
                 'erro_schema',
                 'max_tokens',
                 'erro_provedor',
                 'provedor_indisponivel',
                 'tier_nao_configurado',
                 'erro_interno'
             )
        BEGIN
            SELECT RAISE(ABORT,
                'categoria de parada fora da lista fechada');
        END;

        -- A simetria da migracao 8: o campo pertence ao evento que ele
        -- descreve, e a nenhum outro.
        CREATE TRIGGER motivo_de_parada_so_em_parada
        BEFORE INSERT ON agent_event
        WHEN NEW.kind <> 'parada'
             AND (NEW.stop_category IS NOT NULL OR NEW.stop_reason IS NOT NULL)
        BEGIN
            SELECT RAISE(ABORT,
                'categoria e motivo pertencem ao evento de parada');
        END;
        """,
    ),
]

# Estados em que um run bloqueia alteracao de configuracao.
ESTADOS_ATIVOS = ("executando", "pausado")
