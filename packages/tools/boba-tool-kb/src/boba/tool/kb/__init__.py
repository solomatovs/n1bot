"""boba-tool-kb — плагин: KB-tools поверх postgres+pgvector + Confluence + FTS.

Архитектура **tool-centric**: каждый tool имеет свой self-contained
конфиг (`BobaFlatSettings` с собственной `config_path`), внутри которого
лежат все нужные ему базовые `BaseModel`-блоки (connection / tables /
embedding / chunker / confluence / ...). Сервисы (`PostgresChunkStore`,
`PostgresKnowledgeBase`, ...) **не публикуются в DI**: каждый tool строит
их inline через factory-helpers (`open_kb_pool`, `build_embedder`,
`build_chunker`). Pool singleton'ится по DSN через `PostgresPool.get(...)`,
поэтому tools с одним и тем же connection делят один pool.

Базовые `BaseModel`-блоки (переиспользуемые во всех tool-конфигах):
- `PostgresConnection`      — host/port/user/.../pool_*/connect_timeout_sec.
- `PostgresStoreSchema`     — schema/chunks_table/collections_table/batch_size.
- `EmbeddingModel`          — endpoint/api_key/model (OpenAI-совместимый).
- `ChunkerParams`           — chunk_size/chunk_overlap.
- `ConfluenceConnection`    — base_url/auth_*/timeout/ssl/body_format.
- `IndexSpec`               — whitelist-таблица для FTS-поиска.
- `PostgresStoreConfig`     — composite (connection + tables) для KB-store.
- `PostgresKnowledgeBaseConfig` — composite (connection + tables + embedding
                                   + RRF/FTS-params) для KB read-side.

Tools (что LLM реально вызывает) — каждый со своей TOML-секцией:

**Ingest** (наполнение `kb_chunks`):
- `files_ingest`            → `[tool.kb.files_ingest]`
- `confluence_space_ingest` → `[tool.kb.confluence.ingest.space]`
- `confluence_page_ingest`  → `[tool.kb.confluence.ingest.page]`
- `confluence_cql_ingest`   → `[tool.kb.confluence.ingest.cql]`

**Search**:
- `kb_search`               → `[tool.kb.search_in_kb]`
- `vector_search`           → `[tool.kb.search.vector]`
- `fts_search`              → `[tool.kb.search.fts]`
- `confluence_cql_search`   → `[tool.kb.confluence.search.cql]`
- `confluence_list_spaces`  → `[tool.kb.confluence.list_spaces]`

**Download** (Confluence → workspace):
- `confluence_page_download`  → `[tool.kb.confluence.download.page]`
- `confluence_space_download` → `[tool.kb.confluence.download.space]`

**SQL** (ad-hoc read-only):
- `sql_query`               → `[tool.kb.sql_query]`
- `sql_list_tables`         → `[tool.kb.sql_list_tables]`
- `sql_describe_table`      → `[tool.kb.sql_describe_table]`

**CLI runners** (не tool'ы, операторские скрипты — читают `[cli.kb.*]`):
- `cli/bootstrap`               — миграции + HNSW-индекс (`[cli.kb.bootstrap]`).
- `cli/files_ingest`            — wrapper над `files_ingest` (`[cli.kb.files_ingest]`).
- `cli/ingest_confluence_spaces` — bulk-discovery + per-space ingest
                                   (`[cli.kb.confluence_spaces]` +
                                   CLI-флаги; per-space ingest читает
                                   `[tool.kb.confluence.ingest.space]`).

DI остаётся только для stateless `DispatchReader[str]` / `KbDocReader` /
`HtmlReader` — они шарятся ingest-tool'ами без зависимости от конфигов.
"""

from __future__ import annotations

from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.tools.cql_ingest import (
    ConfluenceCqlIngestConfig,
    confluence_cql_ingest,
)
from boba.tool.kb.confluence.tools.cql_search import (
    ConfluenceCqlSearchConfig,
    confluence_cql_search,
)
from boba.tool.kb.confluence.tools.list_spaces import (
    ConfluenceListSpacesConfig,
    confluence_list_spaces,
)
from boba.tool.kb.confluence.tools.page_download import (
    ConfluencePageDownloadConfig,
    confluence_page_download,
)
from boba.tool.kb.confluence.tools.page_ingest import (
    ConfluencePageIngestConfig,
    confluence_page_ingest,
)
from boba.tool.kb.confluence.tools.space_download import (
    ConfluenceSpaceDownloadConfig,
    confluence_space_download,
)
from boba.tool.kb.confluence.tools.space_ingest import (
    ConfluenceSpaceIngestConfig,
    confluence_space_ingest,
)
from boba.tool.kb.core.chunker_params import ChunkerParams
from boba.tool.kb.core.embedding_model import EmbeddingModel
from boba.tool.kb.core.kb import PostgresKnowledgeBase, PostgresKnowledgeBaseConfig
from boba.tool.kb.core.postgres_connection import PostgresConnection
from boba.tool.kb.core.postgres_store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresStoreConfig,
)
from boba.tool.kb.core.postgres_store_schema import PostgresStoreSchema
from boba.tool.kb.core.providers import (
    provide_dispatch_reader,
    provide_html_reader,
    provide_kbdoc_reader,
)
from boba.tool.kb.core.tools.files_ingest import FilesIngestConfig, files_ingest
from boba.tool.kb.core.tools.kb_search import SearchInKbConfig, kb_search
from boba.tool.kb.core.tools.vector_search import VectorSearchConfig, vector_search
from boba.tool.kb.fts.db import FtsSearchConfig
from boba.tool.kb.fts.models import IndexSpec
from boba.tool.kb.fts.tools.fts_search import fts_search
from boba.tool.kb.sql.executor import SqlExecutor, SqlExecutorConfig
from boba.tool.kb.sql.tools.describe_table import (
    SqlDescribeTableConfig,
    sql_describe_table,
)
from boba.tool.kb.sql.tools.list_tables import SqlListTablesConfig, sql_list_tables
from boba.tool.kb.sql.tools.query import SqlQueryConfig, sql_query

__all__ = [
    "ChunkerParams",
    "ConfluenceConnection",
    "ConfluenceCqlIngestConfig",
    "ConfluenceCqlSearchConfig",
    "ConfluenceListSpacesConfig",
    "ConfluencePageDownloadConfig",
    "ConfluencePageIngestConfig",
    "ConfluenceSpaceDownloadConfig",
    "ConfluenceSpaceIngestConfig",
    "EmbeddingModel",
    "FilesIngestConfig",
    "FtsSearchConfig",
    "IndexSpec",
    "PostgresChunkStore",
    "PostgresCollectionsStore",
    "PostgresConnection",
    "PostgresKnowledgeBase",
    "PostgresKnowledgeBaseConfig",
    "PostgresStoreConfig",
    "PostgresStoreSchema",
    "SearchInKbConfig",
    "SqlDescribeTableConfig",
    "SqlExecutor",
    "SqlExecutorConfig",
    "SqlListTablesConfig",
    "SqlQueryConfig",
    "VectorSearchConfig",
    "confluence_cql_ingest",
    "confluence_cql_search",
    "confluence_list_spaces",
    "confluence_page_download",
    "confluence_page_ingest",
    "confluence_space_download",
    "confluence_space_ingest",
    "files_ingest",
    "fts_search",
    "kb_search",
    "provide_dispatch_reader",
    "provide_html_reader",
    "provide_kbdoc_reader",
    "sql_describe_table",
    "sql_list_tables",
    "sql_query",
    "vector_search",
]
