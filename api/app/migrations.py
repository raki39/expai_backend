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
]

# Estados em que um run bloqueia alteracao de configuracao.
ESTADOS_ATIVOS = ("executando", "pausado")
