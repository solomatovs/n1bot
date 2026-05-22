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
- `PostgresStoreConfig`     — composite (connection + tables) для KB-store.
- `PostgresKnowledgeBaseConfig` — composite (connection + tables + embedding
                                   + RRF/FTS-params) для KB read-side.

Tools (что LLM реально вызывает) — каждый со своей TOML-секцией:

**Ingest** (наполнение `kb_chunks`):
- `confluence_space_ingest` → `[tool.kb.confluence.ingest.space]`
- `confluence_page_ingest`  → `[tool.kb.confluence.ingest.page]`
- `confluence_cql_ingest`   → `[tool.kb.confluence.ingest.cql]`

**Search**:
- `kb_search`               → `[tool.kb.search_in_kb]`
- `vector_search`           → `[tool.kb.search.vector]`
- `confluence_cql_search`   → `[tool.kb.confluence.search.cql]`
- `confluence_spaces_list`  → `[tool.kb.confluence.spaces_list]`

**Download** (Confluence → workspace):
- `confluence_page_download`  → `[tool.kb.confluence.download.page]`
- `confluence_space_download` → `[tool.kb.confluence.download.space]`

SQL- и FTS-tools переехали в отдельный плагин `boba-tool-postgres` (секции
`[tool.pg.query]` / `[tool.pg.list_tables]` / `[tool.pg.describe_table]` /
`[tool.pg.fts_search]`).

**CLI runners** (не tool'ы, операторские скрипты — читают `[cli.kb.*]`):
- `cli/bootstrap`                 — миграции + HNSW-индекс (`[cli.kb.bootstrap]`).
- `cli/kbdoc_ingest`              — индексация папки KbDoc-файлов
                                    (`[cli.kb.kbdoc_ingest]`).
- `cli/ingest_confluence_spaces`  — bulk-discovery + per-space ingest
                                    (`[cli.kb.confluence.ingest.spaces]` +
                                    CLI-флаги; per-space ingest читает
                                    `[tool.kb.confluence.ingest.space]`).
- `cli/confluence_space_download` — скачать весь space на ФС
                                    (`[cli.kb.confluence.download.space]` +
                                    --space-key/--as-markdown).

DI остаётся только для stateless `KbDocReader` — он шарится ingest-tool'ами
без зависимости от конфигов.
"""

from __future__ import annotations

from boba.db.postgres import PostgresConnection
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.tools.cql_ingest import (
    ConfluenceCqlIngestConfig,
    confluence_cql_ingest,
)
from boba.tool.kb.confluence.tools.cql_search import (
    ConfluenceCqlSearchConfig,
    confluence_cql_search,
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
from boba.tool.kb.confluence.tools.spaces_list import (
    ConfluenceSpacesListConfig,
    confluence_spaces_list,
)
from boba.tool.kb.core.chunker_params import ChunkerParams
from boba.tool.kb.core.embedding_model import EmbeddingModel
from boba.tool.kb.core.kb import PostgresKnowledgeBase, PostgresKnowledgeBaseConfig
from boba.tool.kb.core.postgres_store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresStoreConfig,
)
from boba.tool.kb.core.postgres_store_schema import PostgresStoreSchema
from boba.tool.kb.core.providers import provide_kbdoc_reader
from boba.tool.kb.core.tools.kb_search import SearchInKbConfig, kb_search
from boba.tool.kb.core.tools.vector_search import VectorSearchConfig, vector_search

__all__ = [
    "ChunkerParams",
    "ConfluenceConnection",
    "ConfluenceCqlIngestConfig",
    "ConfluenceCqlSearchConfig",
    "ConfluencePageDownloadConfig",
    "ConfluencePageIngestConfig",
    "ConfluenceSpaceDownloadConfig",
    "ConfluenceSpaceIngestConfig",
    "ConfluenceSpacesListConfig",
    "EmbeddingModel",
    "PostgresChunkStore",
    "PostgresCollectionsStore",
    "PostgresConnection",
    "PostgresKnowledgeBase",
    "PostgresKnowledgeBaseConfig",
    "PostgresStoreConfig",
    "PostgresStoreSchema",
    "SearchInKbConfig",
    "VectorSearchConfig",
    "confluence_cql_ingest",
    "confluence_cql_search",
    "confluence_page_download",
    "confluence_page_ingest",
    "confluence_space_download",
    "confluence_space_ingest",
    "confluence_spaces_list",
    "kb_search",
    "provide_kbdoc_reader",
    "vector_search",
]
