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
]

# Estados em que um run bloqueia alteracao de configuracao.
ESTADOS_ATIVOS = ("executando", "pausado")
