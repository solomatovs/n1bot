-- KB-схема: chunks-table + collections-table.
--
-- ВНИМАНИЕ: имена `{{chunks_table}}` / `{{collections_table}}` / `{{schema_name}}`
-- — это плейсхолдеры `psycopg.sql.SQL(...).format(...)`. Реальные значения
-- подставляются в `migrations.apply_bootstrap` из `ChunkStoreSchemaConfig`
-- ([tool.kb.chunk_store]). Все литералы `{{}}` (jsonb default, array default)
-- удвоены до `{{{{}}}}` — это требование psycopg.sql.SQL'овского format'а.
--
-- ВАЖНО про идемпотентность: миграция выполняется при каждом запуске
-- bootstrap-CLI. Все DDL — if not exists / if exists. Структурные
-- изменения вносятся отдельными 00N_*.sql миграциями (порядок по имени файла).
--
-- ВАЖНО про embedding-dim: тип `embedding` — `vector` БЕЗ фиксированной
-- размерности. Это позволяет хранить вектора разной dim в одной таблице
-- (но HNSW-индексу нужна конкретная dim). HNSW-индекс создаётся
-- `ensure_vector_index(..., dim=N)` отдельно после миграций.

create extension if not exists vector;
create extension if not exists pg_trgm;
create extension if not exists unaccent;

-- unaccent сам по себе stable, а Postgres требует immutable-выражение в
-- generated-колонке. Стандартный workaround — immutable-обёртка: словарь
-- зашит в SQL-теле, нельзя поменять снаружи -> expression становится
-- детерминированным для оптимизатора. Без этого `alter table ... add
-- column tsv tsvector generated ...` падает с
-- "generation expression is not immutable".
--
-- Функция создаётся в `{schema}`-схеме (schema-qualified имя), чтобы
-- generated-выражение в `{chunks_table}` ссылалось на конкретный экземпляр
-- (search_path при insert может оказаться другим, тогда unqualified-имя
-- не разрезолвится).
create or replace function {schema}.immutable_unaccent(text)
returns text
language sql immutable parallel safe strict
as $$
    select
        unaccent('unaccent', $1)
$$;

-- Table-level metadata коллекций (description для kb_list_collections).
-- Логическое существование коллекции — наличие в этом каталоге; чанки
-- могут отсутствовать (пустая коллекция = ensure_collection прошёл,
-- ingest ещё нет).
create table if not exists {collections_table} (
    name        text primary key,
    description text not null default '',
    created_at  timestamptz not null default now()
);

-- Чанки всех коллекций живут в одной таблице; коллекция = значение
-- колонки `collection`. Это упрощает admin (один HNSW + GIN), но
-- требует фильтра `where collection = $1` в каждом запросе.
create table if not exists {chunks_table} (
    chunk_id       text primary key,
    collection     text not null,
    source_id      text not null,
    chunk_index    int  not null,
    content_hash   text not null,
    raw_content    text not null,
    format_content text not null,
    embedding      vector,
    metadata       jsonb not null default '{{}}'::jsonb,
    tags           text[] not null default '{{}}',
    updated_at     timestamptz not null default now()
);

-- FTS-вектор хранимым generated-полем: пересчитывается только при изменении
-- format_content. `unaccent` снимает диакритику до tsvector'а — иначе
-- `café` и `cafe` не сматчатся. Здесь `russian`-only — multilang (russian
-- || english) накатывается отдельно в `002_multilang_tsv.sql`.
do $$
begin
    if not exists (
        select
            1
        from
            information_schema.columns
        where
            table_schema = {schema_name_lit}
            and table_name = {chunks_name_lit}
            and column_name = 'tsv'
    ) then
        alter table {chunks_table}
            add column tsv tsvector
            generated always as (
                to_tsvector(
                    'russian',
                    {schema}.immutable_unaccent(coalesce(format_content, ''))
                )
            ) stored;
    end if;
end $$;

create index if not exists {chunks_tsv_gin_name}
    on {chunks_table} using gin (tsv);

create index if not exists {chunks_collection_idx_name}
    on {chunks_table} (collection);

create index if not exists {chunks_collection_source_idx_name}
    on {chunks_table} (collection, source_id);

-- HNSW по `embedding` создаётся отдельно для конкретной dim — pgvector
-- не индексирует `vector` без указания размерности. См. ensure_vector_index
-- в `migrations.py` (вызывается после apply_bootstrap в bootstrap-CLI).
