-- KB-схема: kb_chunks + kb_collections.
--
-- ВАЖНО про идемпотентность: миграция выполняется при каждом старте
-- плагина (см. migrations.apply_bootstrap). Все DDL должны быть
-- IF NOT EXISTS / IF EXISTS. Структурные изменения вносятся отдельными
-- 00N_*.sql миграциями (порядок по имени файла).
--
-- ВАЖНО про embedding-dim: тип `embedding` — `vector` БЕЗ фиксированной
-- размерности. Это позволяет хранить вектора разной dim в одной таблице
-- (но HNSW-индексу нужна конкретная dim). Per-collection HNSW-индексы
-- создаются partial-индексами с приведением `(embedding::vector(N))`
-- через `ensure_vector_index(collection, dim)` из app-кода — это
-- единственный способ заставить pgvector индексировать смешанную
-- таблицу. Single-model deployments — один индекс на всё.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- unaccent сам по себе STABLE, а Postgres требует IMMUTABLE-выражение в
-- GENERATED-колонке. Стандартный workaround — IMMUTABLE-обёртка: словарь
-- зашит в SQL-теле, нельзя поменять снаружи → expression становится
-- детерминированным для оптимизатора. Без этого `ALTER TABLE ... ADD
-- COLUMN tsv tsvector GENERATED ...` падает с
-- "generation expression is not immutable".
CREATE OR REPLACE FUNCTION immutable_unaccent(text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT unaccent('unaccent', $1) $$;

-- Table-level metadata коллекций (description для kb_list_collections).
-- Логическое существование коллекции — наличие в этом каталоге; чанки
-- могут отсутствовать (пустая коллекция = ensure_collection прошёл,
-- ingest ещё нет).
CREATE TABLE IF NOT EXISTS kb_collections (
    name        text PRIMARY KEY,
    description text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Чанки всех коллекций живут в одной таблице; коллекция = значение
-- колонки `collection`. Это упрощает admin (один HNSW + GIN), но
-- требует фильтра `WHERE collection = $1` в каждом запросе.
CREATE TABLE IF NOT EXISTS kb_chunks (
    chunk_id       text PRIMARY KEY,
    collection     text NOT NULL,
    source_id      text NOT NULL,
    chunk_index    int  NOT NULL,
    content_hash   text NOT NULL,
    raw_content    text NOT NULL,
    format_content text NOT NULL,
    embedding      vector,
    metadata       jsonb NOT NULL DEFAULT '{}'::jsonb,
    tags           text[] NOT NULL DEFAULT '{}',
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- FTS-вектор хранимым GENERATED-полем: пересчитывается только при изменении
-- format_content. `unaccent` снимает диакритику до tsvector'а — иначе
-- `café` и `cafe` не сматчатся. Конфигурация `russian` нанесена жёстко
-- (как в config.toml — fts_language); для смены требуется DROP COLUMN +
-- ADD COLUMN (новая миграция).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kb_chunks' AND column_name = 'tsv'
    ) THEN
        ALTER TABLE kb_chunks
            ADD COLUMN tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector(
                    'russian',
                    immutable_unaccent(coalesce(format_content, ''))
                )
            ) STORED;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS kb_chunks_tsv_gin
    ON kb_chunks USING gin (tsv);

CREATE INDEX IF NOT EXISTS kb_chunks_collection
    ON kb_chunks (collection);

CREATE INDEX IF NOT EXISTS kb_chunks_collection_source
    ON kb_chunks (collection, source_id);

-- HNSW по `embedding` создаётся отдельно для конкретной dim — pgvector
-- не индексирует `vector` без указания размерности. См. ensure_vector_index
-- в `vector_store.py` (вызывается при первом upsert в коллекцию).
