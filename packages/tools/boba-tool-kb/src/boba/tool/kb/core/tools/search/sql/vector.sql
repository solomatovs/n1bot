-- Pure vector retrieval SQL: top-K по cosine-distance (pgvector `<=>`).
-- Используется PostgresKnowledgeBase.vector_search().
--
-- Identifier-плейсхолдеры (psycopg sql.SQL.format):
--   {dim}          — размерность embedding
--   {chunks_table} — fully-qualified имя таблицы chunks
--
-- Bind-параметры (psycopg named-style):
--   collections, embedding, snippet_chars, top_k
select
    c.chunk_id,
    c.source_id,
    c.chunk_index,
    c.content_hash,
    c.metadata,
    c.tags,
    left(c.format_content, %(snippet_chars)s) AS snippet,
    (c.embedding::vector({dim})) <=> %(embedding)s::vector AS distance
from
    {chunks_table} c
where 1=1
    and c.collection = any(%(collections)s)
    and c.embedding is not null
order by
    distance asc
limit
    %(top_k)s
