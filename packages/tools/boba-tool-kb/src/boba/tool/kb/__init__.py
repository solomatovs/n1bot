"""boba-tool-kb — плагин: KB-tools поверх postgres+pgvector + Confluence + FTS.

Физическая структура (domain-first, tools/ внутри каждого домена):

- `core/`           — общая инфраструктура: kb-фасад, vector_store, providers,
                      migrations, config/models/errors, _markdown helper.
  - `core/tools/`   — tools, не привязанные к одному внешнему домену:
                      `kb_search`, `vector_search`, `files_ingest`.
- `core/cli/`      — операторские CLI-runner'ы (`ingest_files`), не tools.
- `confluence/`     — Confluence-домен: api_models, auth, connection, decoder,
                      keys, parse, reader, search_reader, request_sources/,
                      _download_common, _ingest_common.
  - `confluence/tools/` — `confluence_search`, `confluence_list_spaces`,
                          `confluence_page_download`, `confluence_space_download`,
                          `confluence_page_ingest`, `confluence_space_ingest`.
  - `confluence/cli/`   — операторские CLI-runner'ы (`ingest_all_spaces`).
- `fts/`            — FTS-инфра: db, models, providers, config.
  - `fts/tools/`    — `fts_search`.
- `sql/`            — ad-hoc SQL-инфра: executor, providers, config.
  - `sql/tools/`    — `sql_query` (`query.py`), `sql_list_tables`, `sql_describe_table`.

Tools по назначению (что LLM реально вызывает):

**Ingest** (наполнение нашей KB → `kb_chunks`, pinned collection):
- `files_ingest`            — FS-папка (`[tool.kb].files_folder`).
- `confluence_space_ingest` — один или несколько spaces (LLM передаёт `space_keys`).
- `confluence_page_ingest`  — явный список page_ids (LLM выбирает).

**Search** (read-only):
- `kb_search`               — hybrid (vector + FTS, RRF) по `[tool.kb].collection`.
- `vector_search`           — pure vector (cosine) по `[tool.kb].collection`.
- `fts_search`              — pure FTS по таблице из `[tool.kb.fts].index`.
- `confluence_search`       — online CQL по реальному Confluence.
- `confluence_list_spaces`  — list of spaces (markdown). Помогает LLM с space_keys.

**Confluence work** (online + workspace):
- `confluence_page_download(page_ids, dest_dir, as_markdown=False)`
- `confluence_space_download(space_key, dest_dir, as_markdown=False)`

**SQL** (ad-hoc read-only):
- `sql_list_tables(schema=None)`       — таблицы/view роли DSN.
- `sql_describe_table(table, schema)`  — колонки/типы.
- `sql_query(query, row_limit=20)`     — произвольный SELECT → markdown table.

**Конфиг-секции:**
- `[tool.kb]`              → `KbConfig` (collection, files_folder, RRF, chunker).
- `[tool.kb.postgres]`     → `PostgresConnectionConfig` (host/port/user/...).
- `[tool.kb.confluence]`   → `ConfluenceConnectionConfig` (base_url/auth/...).
- `[tool.kb.embedding]`    → `EmbeddingConfig` (model/base_url/api_key).
- `[tool.kb.fts]`          → `FtsConfig` (одна whitelist-таблица для fts_search).
- `[tool.kb.sql]`          → `SqlConfig` (отдельный read-only DSN + safety-limits).

Плагин-уровневое включение — через `[tool.kb].enable` + `[tool.kb].tools`
allowlist (PluginConfigBase). Connection-конфиги (postgres/confluence)
загружаются framework'ом лениво — только если в allowlist есть хоть один
tool, использующий их через FromConfig.

Pipeline-граф:
- `PostgresConnectionConfig.to_pool_config()` → `PostgresPool.get(...)`
  с `register_vector`-hook'ом. Схема БД (миграции + HNSW-индекс) — это
  отдельный операторский шаг через CLI `boba.tool.kb.core.cli.bootstrap`;
  runtime DDL в провайдере не делает.
- `PostgresPool` шарится между kb_search и fts_search (DSN-fallback в FtsConfig).
- Ingest-tools собирают `StreamingIndexer` inline:
  - FS:        `FsWalkRequestSource` + `FsTransport` + `DispatchReader` (md/html).
  - Confluence: `{Pages|Space}RequestSource` + `HttpTransport` +
                `decoders=[ConfluenceJsonDecoder]` + `ConfluenceReader`.
- Download-tools пишут в `ProjectWorkspaceShell`; формат HTML/Markdown
  выбирается параметром `as_markdown` (через `markdownify`, ATX-заголовки).
"""

from __future__ import annotations

from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.tools.list_spaces import confluence_list_spaces
from boba.tool.kb.confluence.tools.page_download import confluence_page_download
from boba.tool.kb.confluence.tools.page_ingest import confluence_page_ingest
from boba.tool.kb.confluence.tools.search import confluence_search
from boba.tool.kb.confluence.tools.space_download import confluence_space_download
from boba.tool.kb.confluence.tools.space_ingest import confluence_space_ingest
from boba.tool.kb.core.chunk_store import PostgresChunkStore
from boba.tool.kb.core.collections_store import PostgresCollectionsStore
from boba.tool.kb.core.config import KbConfig
from boba.tool.kb.core.embedding_config import EmbeddingConfig
from boba.tool.kb.core.kb import PostgresKnowledgeBase
from boba.tool.kb.core.postgres_config import PostgresConnectionConfig
from boba.tool.kb.core.providers import (
    provide_chunk_store,
    provide_chunker,
    provide_collections_store,
    provide_dispatch_reader,
    provide_embedder,
    provide_html_reader,
    provide_kbdoc_reader,
    provide_knowledge_base,
    provide_postgres_pool,
)
from boba.tool.kb.core.tools.files_ingest import files_ingest
from boba.tool.kb.core.tools.kb_search import kb_search
from boba.tool.kb.core.tools.vector_search import vector_search
from boba.tool.kb.core.vector_store_config import VectorStoreSchemaConfig
from boba.tool.kb.fts.config import FtsConfig
from boba.tool.kb.fts.providers import provide_fts_kb
from boba.tool.kb.fts.tools.fts_search import fts_search
from boba.tool.kb.sql.config import SqlConfig
from boba.tool.kb.sql.providers import provide_sql_executor
from boba.tool.kb.sql.tools.describe_table import sql_describe_table
from boba.tool.kb.sql.tools.list_tables import sql_list_tables
from boba.tool.kb.sql.tools.query import sql_query

__all__ = [
    "ConfluenceConnectionConfig",
    "EmbeddingConfig",
    "FtsConfig",
    "KbConfig",
    "PostgresChunkStore",
    "PostgresCollectionsStore",
    "PostgresConnectionConfig",
    "PostgresKnowledgeBase",
    "SqlConfig",
    "VectorStoreSchemaConfig",
    "confluence_list_spaces",
    "confluence_page_download",
    "confluence_page_ingest",
    "confluence_search",
    "confluence_space_download",
    "confluence_space_ingest",
    "files_ingest",
    "fts_search",
    "kb_search",
    "provide_chunk_store",
    "provide_chunker",
    "provide_collections_store",
    "provide_dispatch_reader",
    "provide_embedder",
    "provide_fts_kb",
    "provide_html_reader",
    "provide_kbdoc_reader",
    "provide_knowledge_base",
    "provide_postgres_pool",
    "provide_sql_executor",
    "sql_describe_table",
    "sql_list_tables",
    "sql_query",
    "vector_search",
]
