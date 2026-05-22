"""`core` — общая инфраструктура boba-tool-kb.

Содержит cross-domain код, переиспользуемый всеми доменами (confluence/fts/sql):

- `kb.py`              — `PostgresKnowledgeBase` (главный фасад над pgvector + FTS).
- `vector_store.py`    — `PostgresVectorStore` (запись chunks + индексы).
- `providers.py`       — DI-providers (pool, embedder, chunker, reader, kb).
- `migrations.py`      — bootstrap-loader для SQL-миграций из `core/migrations/`.
- `postgres_config.py` — `PostgresConnectionConfig` (общий для kb + fts).
- `config.py`          — `KbConfig` (`[tool.kb]`).
- `models.py`          — общие dataclass-ы (`SearchHit`).
- `errors.py`          — общий `KnowledgeBaseError`.
- `_markdown.py`       — `format_markdown_table` (используется всеми tools,
                          форматирующими результат в markdown).

Tools, опирающиеся на этот общий код (kb_search, vector_search,
files_ingest), лежат в `core/tools/`.
"""
