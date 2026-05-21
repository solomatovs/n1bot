"""boba-tool-kb — плагин: KB-tools поверх postgres+pgvector + Confluence + FTS.

Структура по категориям:

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

**SQL** (ad-hoc read-only, см. подпакет `sql/`):
- `sql_list_tables(schema=None)`       — таблицы/view роли DSN.
- `sql_describe_table(table, schema)`  — колонки/типы.
- `sql_query(query, row_limit=20)`     — произвольный SELECT → markdown table.

**Конфиг-секции:**
- `[tool.kb]`              → `KbConfig` (collection, files_folder, embedder, RRF).
- `[tool.kb.postgres]`     → `PostgresConnectionConfig` (host/port/user/...).
- `[tool.kb.confluence]`   → `ConfluenceConnectionConfig` (base_url/auth/...).
- `[tool.kb.fts]`          → `FtsConfig` (одна whitelist-таблица для fts_search).
- `[tool.kb.sql]`          → `SqlConfig` (отдельный read-only DSN + safety-limits).

Плагин-уровневое включение — через `[tool.kb].enable` + `[tool.kb].tools`
allowlist (PluginConfigBase). Connection-конфиги (postgres/confluence)
загружаются framework'ом лениво — только если в allowlist есть хоть один
tool, использующий их через FromConfig.

Pipeline-граф:
- `PostgresConnectionConfig.to_pool_config()` → `PostgresPool.get(...)`
  с `register_vector`-hook'ом + `apply_bootstrap` (idempotent migrations).
- `PostgresPool` шарится между kb_search и fts_search (DSN-fallback в FtsConfig).
- Ingest-tools собирают `StreamingIndexer` inline:
  - FS:        `FsWalkRequestSource` + `FsTransport` + `DispatchReader` (md/html).
  - Confluence: `{Pages|Space}RequestSource` + `HttpTransport` +
                `decoders=[ConfluenceJsonDecoder]` + `ConfluenceReader`.
- Download-tools пишут в `ProjectWorkspaceShell`; формат HTML/Markdown
  выбирается параметром `as_markdown` (через `markdownify`, ATX-заголовки).
"""

from __future__ import annotations

from boba.tool.kb.config import KbConfig
from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.list_spaces import confluence_list_spaces
from boba.tool.kb.confluence.page_download import confluence_page_download
from boba.tool.kb.confluence.page_ingest import confluence_page_ingest
from boba.tool.kb.confluence.search import confluence_search
from boba.tool.kb.confluence.space_download import confluence_space_download
from boba.tool.kb.confluence.space_ingest import confluence_space_ingest
from boba.tool.kb.files_ingest import files_ingest
from boba.tool.kb.fts.config import FtsConfig
from boba.tool.kb.fts.fts_search import fts_search
from boba.tool.kb.fts.providers import provide_fts_kb
from boba.tool.kb.kb import PostgresKnowledgeBase
from boba.tool.kb.kb_search import kb_search
from boba.tool.kb.postgres_config import PostgresConnectionConfig
from boba.tool.kb.providers import (
    provide_chunker,
    provide_dispatch_reader,
    provide_embedder,
    provide_html_reader,
    provide_kbdoc_reader,
    provide_knowledge_base,
    provide_postgres_pool,
    provide_vector_store,
)
from boba.tool.kb.sql.config import SqlConfig
from boba.tool.kb.sql.providers import provide_sql_executor
from boba.tool.kb.sql.sql_describe_table import sql_describe_table
from boba.tool.kb.sql.sql_list_tables import sql_list_tables
from boba.tool.kb.sql.sql_query import sql_query
from boba.tool.kb.vector_search import vector_search
from boba.tool.kb.vector_store import PostgresVectorStore

__all__ = [
    "ConfluenceConnectionConfig",
    "FtsConfig",
    "KbConfig",
    "PostgresConnectionConfig",
    "PostgresKnowledgeBase",
    "PostgresVectorStore",
    "SqlConfig",
    "confluence_list_spaces",
    "confluence_page_download",
    "confluence_page_ingest",
    "confluence_search",
    "confluence_space_download",
    "confluence_space_ingest",
    "files_ingest",
    "fts_search",
    "kb_search",
    "provide_chunker",
    "provide_dispatch_reader",
    "provide_embedder",
    "provide_fts_kb",
    "provide_html_reader",
    "provide_kbdoc_reader",
    "provide_knowledge_base",
    "provide_postgres_pool",
    "provide_sql_executor",
    "provide_vector_store",
    "sql_describe_table",
    "sql_list_tables",
    "sql_query",
    "vector_search",
]
