-- Hybrid retrieval SQL: vector top-K + FTS top-K, склейка через
-- Reciprocal Rank Fusion (RRF). Используется PostgresKnowledgeBase.search().
--
-- Identifier-плейсхолдеры (psycopg sql.SQL.format):
--   {dim}            — размерность embedding
--   {chunks_table}   — fully-qualified имя таблицы chunks
--   {schema}         — имя schema (для immutable_unaccent)
--
-- Bind-параметры (%(name)s):
--   collections, embedding, query, lang, rrf_k, rrf_pool, snippet_chars, top_k
with vec as (
    select
        chunk_id,
        row_number() over (order by (embedding::vector({dim})) <=> %(embedding)s::vector) as rk
    from
        {chunks_table}
    where 1=1
        and collection = any(%(collections)s)
        and embedding is not null
    order by
        (embedding::vector({dim})) <=> %(embedding)s::vector
    limit
        %(rrf_pool)s
),
fts as (
    select
        chunk_id,
        row_number() over (order by ts_rank_cd(tsv, q) desc) as rk
    from
        {chunks_table},
        plainto_tsquery(%(lang)s::regconfig,
        {schema}.immutable_unaccent(%(query)s)) q
    where 1=1
        and collection = ANY(%(collections)s)
        and tsv @@ q
    order by
        ts_rank_cd(tsv, q) desc
    limit
        %(rrf_pool)s
),
fused as (
    select
        coalesce(v.chunk_id, f.chunk_id) as chunk_id,
        (
            case when v.rk is null then 0.0
            else 1.0 / (%(rrf_k)s + v.rk)
            end
        )
        + (
            case when f.rk is null then 0.0
            else 1.0 / (%(rrf_k)s + f.rk)
            end
        ) as rrf
    from
        vec v
        full join fts f using (chunk_id)
)
select
    c.chunk_id,
    c.source_id,
    c.chunk_index,
    c.content_hash,
    c.metadata,
    c.tags,
    left(c.format_content, %(snippet_chars)s) as snippet,
    fused.rrf as rrf
from
    fused
    join {chunks_table} c using (chunk_id)
where
    c.collection = any(%(collections)s)
order by
    fused.rrf desc
limit
    %(top_k)s
