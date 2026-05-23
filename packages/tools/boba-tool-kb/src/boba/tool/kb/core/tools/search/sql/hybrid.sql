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
WITH vec AS (
    SELECT chunk_id,
           row_number() OVER (
               ORDER BY (embedding::vector({dim})) <=> %(embedding)s::vector
           ) AS rk
    FROM {chunks_table}
    WHERE collection = ANY(%(collections)s) AND embedding IS NOT NULL
    ORDER BY (embedding::vector({dim})) <=> %(embedding)s::vector
    LIMIT %(rrf_pool)s
),
fts AS (
    SELECT chunk_id,
           row_number() OVER (
               ORDER BY ts_rank_cd(tsv, q) DESC
           ) AS rk
    FROM {chunks_table},
         plainto_tsquery(%(lang)s::regconfig,
         {schema}.immutable_unaccent(%(query)s)) q
    WHERE collection = ANY(%(collections)s) AND tsv @@ q
    ORDER BY ts_rank_cd(tsv, q) DESC
    LIMIT %(rrf_pool)s
),
fused AS (
    SELECT
        COALESCE(v.chunk_id, f.chunk_id) AS chunk_id,
        (CASE WHEN v.rk IS NULL THEN 0.0
              ELSE 1.0 / (%(rrf_k)s + v.rk) END)
        + (CASE WHEN f.rk IS NULL THEN 0.0
                ELSE 1.0 / (%(rrf_k)s + f.rk) END) AS rrf
    FROM vec v
    FULL OUTER JOIN fts f USING (chunk_id)
)
SELECT c.chunk_id,
       c.source_id,
       c.chunk_index,
       c.content_hash,
       c.metadata,
       c.tags,
       LEFT(c.format_content, %(snippet_chars)s) AS snippet,
       fused.rrf AS rrf
FROM fused
JOIN {chunks_table} c USING (chunk_id)
WHERE c.collection = ANY(%(collections)s)
ORDER BY fused.rrf DESC
LIMIT %(top_k)s
