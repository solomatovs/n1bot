-- KB-схема: chunks-table + collections-table.
--
-- ВНИМАНИЕ: имена `{{chunks_table}}` / `{{collections_table}}` / `{{schema_name}}`
-- — это плейсхолдеры `psycopg.sql.SQL(...).format(...)`. Реальные значения
-- подставляются в `migrations.apply_bootstrap` из `ChunkStoreSchemaConfig`
-- ([tool.kb.chunk_store]). Все литералы `{{}}` (jsonb default, array default)
-- удвоены до `{{{{}}}}` — это требование psycopg.sql.SQL'овского format'а.
--
-- ВАЖНО про идемпотентность: миграция выполняется при каждом запуске
-- bootstrap-CLI. Все DDL — IF NOT EXISTS / IF EXISTS. Структурные
-- изменения вносятся отдельными 00N_*.sql миграциями (порядок по имени файла).
--
-- ВАЖНО про embedding-dim: тип `embedding` — `vector` БЕЗ фиксированной
-- размерности. Это позволяет хранить вектора разной dim в одной таблице
-- (но HNSW-индексу нужна конкретная dim). HNSW-индекс создаётся
-- `ensure_vector_index(..., dim=N)` отдельно после миграций.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- unaccent сам по себе STABLE, а Postgres требует IMMUTABLE-выражение в
-- GENERATED-колонке. Стандартный workaround — IMMUTABLE-обёртка: словарь
-- зашит в SQL-теле, нельзя поменять снаружи -> expression становится
-- детерминированным для оптимизатора. Без этого `ALTER TABLE ... ADD
-- COLUMN tsv tsvector GENERATED ...` падает с
-- "generation expression is not immutable".
--
-- Функция создаётся в `{schema}`-схеме (schema-qualified имя), чтобы
-- GENERATED-выражение в `{chunks_table}` ссылалось на конкретный экземпляр
-- (search_path при INSERT может оказаться другим, тогда unqualified-имя
-- не разрезолвится).
CREATE OR REPLACE FUNCTION {schema}.immutable_unaccent(text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT unaccent('unaccent', $1) $$;

-- Table-level metadata коллекций (description для kb_list_collections).
-- Логическое существование коллекции — наличие в этом каталоге; чанки
-- могут отсутствовать (пустая коллекция = ensure_collection прошёл,
-- ingest ещё нет).
CREATE TABLE IF NOT EXISTS {collections_table} (
    name        text PRIMARY KEY,
    description text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Чанки всех коллекций живут в одной таблице; коллекция = значение
-- колонки `collection`. Это упрощает admin (один HNSW + GIN), но
-- требует фильтра `WHERE collection = $1` в каждом запросе.
CREATE TABLE IF NOT EXISTS {chunks_table} (
    chunk_id       text PRIMARY KEY,
    collection     text NOT NULL,
    source_id      text NOT NULL,
    chunk_index    int  NOT NULL,
    content_hash   text NOT NULL,
    raw_content    text NOT NULL,
    format_content text NOT NULL,
    embedding      vector,
    metadata       jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    tags           text[] NOT NULL DEFAULT '{{}}',
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- FTS-вектор хранимым GENERATED-полем: пересчитывается только при изменении
-- format_content. `unaccent` снимает диакритику до tsvector'а — иначе
-- `café` и `cafe` не сматчатся. Здесь `russian`-only — multilang (russian
-- || english) накатывается отдельно в `002_multilang_tsv.sql`.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = {schema_name_lit}
          AND table_name = {chunks_name_lit}
          AND column_name = 'tsv'
    ) THEN
        ALTER TABLE {chunks_table}
            ADD COLUMN tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector(
                    'russian',
                    {schema}.immutable_unaccent(coalesce(format_content, ''))
                )
            ) STORED;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS {chunks_tsv_gin_name}
    ON {chunks_table} USING gin (tsv);

CREATE INDEX IF NOT EXISTS {chunks_collection_idx_name}
    ON {chunks_table} (collection);

CREATE INDEX IF NOT EXISTS {chunks_collection_source_idx_name}
    ON {chunks_table} (collection, source_id);

-- HNSW по `embedding` создаётся отдельно для конкретной dim — pgvector
-- не индексирует `vector` без указания размерности. См. ensure_vector_index
-- в `migrations.py` (вызывается после apply_bootstrap в bootstrap-CLI).
