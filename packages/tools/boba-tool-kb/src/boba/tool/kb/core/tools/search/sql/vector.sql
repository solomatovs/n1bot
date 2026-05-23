-- Pure vector retrieval SQL: top-K по cosine-distance (pgvector `<=>`).
-- Используется PostgresKnowledgeBase.vector_search().
--
-- Identifier-плейсхолдеры (psycopg sql.SQL.format):
--   {dim}          — размерность embedding
--   {chunks_table} — fully-qualified имя таблицы chunks
--
-- Bind-параметры (%(name)s):
--   collections, embedding, snippet_chars, top_k
SELECT c.chunk_id,
       c.source_id,
       c.chunk_index,
       c.content_hash,
       c.metadata,
       c.tags,
       LEFT(c.format_content, %(snippet_chars)s) AS snippet,
       (c.embedding::vector({dim})) <=> %(embedding)s::vector AS distance
FROM {chunks_table} c
WHERE c.collection = ANY(%(collections)s) AND c.embedding IS NOT NULL
ORDER BY distance ASC
LIMIT %(top_k)s
