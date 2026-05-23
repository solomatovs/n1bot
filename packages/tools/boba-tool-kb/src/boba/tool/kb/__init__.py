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

**Соглашение об именовании**: tool-функции, конфиг-секции и файлы tools/CLI
следуют шаблону `<scope>_<verb>_<target>` (snake_case в коде) и зеркалят
дотированный путь конфига `<scope>.<verb>.<target>` через одноимённую
file-system иерархию подкаталогов.

Tools (что LLM реально вызывает) — каждый со своей TOML-секцией:

**Ingest** (наполнение `kb_chunks`) — три tool'а, общая секция
`[tool.kb.confluence.ingest]`:
- `confluence_ingest_spaces`  → индексировать все страницы spaces.
- `confluence_ingest_pages`   → индексировать явный список page_id.
- `confluence_ingest_cql`     → индексировать страницы по CQL-запросу.

**Search**:
- `kb_search_hybrid`        → `[tool.kb.search.hybrid]`
- `kb_search_vector`        → `[tool.kb.search.vector]`
- `kb_search_fts`           → `[tool.kb.search.fts]`
- `confluence_search_cql`   → `[tool.kb.confluence.search.cql]`

**List/Discovery**:
- `confluence_list_spaces`  → `[tool.kb.confluence.list.spaces]`

**Download** (Confluence → workspace):
- `confluence_download`     → `[tool.kb.confluence.download]`
   unified: LLM выбирает режим через `space_keys` / `page_ids`.

**Fetch** (Confluence → строка-контент прямо LLM, без ФС):
- `confluence_fetch_page`   → `[tool.kb.confluence.fetch]`
   одна страница по `page_id`, без attachment'ов.

SQL- и FTS-tools переехали в отдельный плагин `boba-tool-postgres` (секции
`[tool.pg.query]` / `[tool.pg.list_tables]` / `[tool.pg.describe_table]` /
`[tool.pg.fts_search]`).

**CLI runners** (не tool'ы, операторские скрипты — читают `[cli.kb.*]`):
- `cli/kb_bootstrap`                  — миграции + HNSW-индекс
                                        (`[cli.kb.bootstrap]`).
- `cli/kbdoc/ingest`                  — индексация папки KbDoc-файлов
                                        (`[cli.kb.kbdoc.ingest]`).
- `cli/confluence/ingest/folder`      — индексация уже-скачанных
                                        confluence-файлов (.html →
                                        ConfluenceReader, .md →
                                        MarkdownReader); two-step workflow
                                        после `confluence/download/http`
                                        (`[cli.kb.confluence.ingest.folder]`).
- `cli/confluence/ingest/http`        — unified HTTP-ingest: bulk-discovery /
                                        `only`-spaces / явный `page_ids`
                                        (`[cli.kb.confluence.ingest]`).
- `cli/confluence/download/http`      — unified HTTP-download: те же три
                                        режима, что и у ingest
                                        (`[cli.kb.confluence.download]`).
- `cli/search/render`                 — рендер kb_search_* SQL-шаблона со
                                        всеми bind'ами инлайнено для ad-hoc
                                        прогона в psql (`[cli.kb.search.render]`).

CLI-runner'ы не используют DI: каждый инстанцирует свой `BobaFlatSettings`-
конфиг напрямую (`cfg = Config()`) и зовёт factory-helpers/tool-функции
явно. DI-провайдеров в плагине нет — Reader'ы создаются inline (`KbDocReader()`,
`ConfluenceReader()`, `MarkdownReader()`).
"""

from __future__ import annotations

from boba.db.postgres import PostgresConnection
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.tools.download import (
    ConfluenceDownloadConfig,
    confluence_download,
)
from boba.tool.kb.confluence.tools.fetch import (
    ConfluenceFetchPageConfig,
    confluence_fetch_page,
)
from boba.tool.kb.confluence.tools.ingest import (
    ConfluenceIngestConfig,
    confluence_ingest_cql,
    confluence_ingest_pages,
    confluence_ingest_spaces,
)
from boba.tool.kb.confluence.tools.list.spaces import (
    ConfluenceListSpacesConfig,
    confluence_list_spaces,
)
from boba.tool.kb.confluence.tools.search.cql import (
    ConfluenceSearchCqlConfig,
    confluence_search_cql,
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
from boba.tool.kb.core.tools.search.fts import (
    KbSearchFtsConfig,
    kb_search_fts,
)
from boba.tool.kb.core.tools.search.hybrid import (
    KbSearchHybridConfig,
    kb_search_hybrid,
)
from boba.tool.kb.core.tools.search.vector import (
    KbSearchVectorConfig,
    kb_search_vector,
)

__all__ = [
    "ChunkerParams",
    "ConfluenceConnection",
    "ConfluenceDownloadConfig",
    "ConfluenceFetchPageConfig",
    "ConfluenceIngestConfig",
    "ConfluenceListSpacesConfig",
    "ConfluenceSearchCqlConfig",
    "EmbeddingModel",
    "KbSearchFtsConfig",
    "KbSearchHybridConfig",
    "KbSearchVectorConfig",
    "PostgresChunkStore",
    "PostgresCollectionsStore",
    "PostgresConnection",
    "PostgresKnowledgeBase",
    "PostgresKnowledgeBaseConfig",
    "PostgresStoreConfig",
    "PostgresStoreSchema",
    "confluence_download",
    "confluence_fetch_page",
    "confluence_ingest_cql",
    "confluence_ingest_pages",
    "confluence_ingest_spaces",
    "confluence_list_spaces",
    "confluence_search_cql",
    "kb_search_fts",
    "kb_search_hybrid",
    "kb_search_vector",
]
