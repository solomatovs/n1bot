-- Pure FTS retrieval SQL: top-K по `ts_rank_cd` без vector-канала.
-- Используется PostgresKnowledgeBase.fts_search().
--
-- Identifier-плейсхолдеры (psycopg sql.SQL.format):
--   {chunks_table} — fully-qualified имя таблицы chunks
--   {schema}       — имя schema (для immutable_unaccent)
--
-- Bind-параметры (%(name)s):
--   collections, query, snippet_chars, top_k
--
-- Multilang FTS: tsquery строится как `russian || english` — совпадает с
-- хранимым tsv из миграции 002_multilang_tsv.sql. Для смены набора языков
-- см. комментарий в `hybrid.sql`.
with q as (
    select
        plainto_tsquery('russian', {schema}.immutable_unaccent(%(query)s))
        || plainto_tsquery('english', {schema}.immutable_unaccent(%(query)s))
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
