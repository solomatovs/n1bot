-- Pure FTS retrieval SQL: top-K по `ts_rank_cd` без vector-канала.
-- Используется PostgresKnowledgeBase.fts_search().
--
-- Identifier-плейсхолдеры (psycopg sql.SQL.format):
--   {chunks_table} — fully-qualified имя таблицы chunks
--   {schema}       — имя schema (для immutable_unaccent)
--
-- Bind-параметры (psycopg named-style):
--   collections, query, snippet_chars, top_k
--
-- Multilang FTS: tsquery строится как `russian || english` — совпадает с
-- хранимым tsv из миграции 002_multilang_tsv.sql. Набор языков должен быть
-- синхронен с DDL tsv-колонки (migrations/002_multilang_tsv.sql).
--
-- `websearch_to_tsquery` (а не `plainto_tsquery`): пробел = AND, но LLM может
-- сама управлять — `OR`, `"фраза"`, `-исключение`. `plainto` форсил AND по всем
-- словам, из-за чего многословный запрос часто давал ноль. Функция тотальная —
-- на любом вводе не падает, экранировать ничего не нужно.
with q as (
    select websearch_to_tsquery('russian', {schema}.immutable_unaccent(%(query)s))
        || websearch_to_tsquery('english', {schema}.immutable_unaccent(%(query)s))
            as tsq
)
select
    c.chunk_id,
    c.source_id,
    c.chunk_index,
    c.content_hash,
    c.metadata,
    c.tags,
    left(c.format_content, %(snippet_chars)s) as snippet,
    ts_rank_cd(c.tsv, q.tsq) as rank
from
    {chunks_table} c,
    q
where 1=1
    and c.collection = any(%(collections)s)
    and c.tsv @@ q.tsq
order by
    rank desc
limit
    %(top_k)s
