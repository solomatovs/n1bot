"""boba-tool-kb — плагин: KB-tools поверх postgres+pgvector + Confluence-tools.

Knowledge-base tools (наша KB, в `kb_chunks`):
- `kb_search`            — hybrid (vector + FTS, RRF) поиск по коллекции.
- `kb_list_collections`  — список коллекций нашей KB.
- `kb_ingest`            — индексация заранее настроенной папки в коллекцию.
                           Поддерживает `.md` (KbDocReader + MarkdownReader)
                           и `.html/.htm` (HtmlReader).
- `kb_ingest_confluence` — индексация Confluence-источника (space/cql/pages)
                           в коллекцию через тот же `StreamingIndexer`
                           pipeline (HttpTransport + ConfluenceJsonDecoder
                           в `decoders=[…]` + ConfluenceReader).

Confluence read-side tools (см. подпакет `confluence/`):
- `confluence_search`                  — CQL-поиск по тексту.
- `confluence_page_outline`            — структура заголовков страницы.
- `confluence_page_section`            — текст одной секции по page_id+anchor.
- `confluence_page_download`           — скачать страницы как HTML.
- `confluence_page_download_markdown`  — скачать страницы как Markdown.

External FTS tools — read-only по чужим таблицам оператора
(см. подпакет `external_fts/`):
- `fts_search`           — websearch_to_tsquery по одному whitelist-индексу.
- `fts_list_indexes`     — список whitelist-индексов с описаниями.

Все шарят плагин-уровневое включение через `KbPluginConfig`
(`[tool.kb]`, `BOBA_TOOL__KB__*`); `enable=true` подключает их пакетом,
allowlist `tools` сужает. Confluence-tools дополнительно требуют
connection-секцию `[tool.kb.confluence]` (`ConfluencePluginConfig`);
`kb_ingest_confluence` — ещё и `[tool.kb.confluence_ingest]`
(`ConfluenceIngestConfig`). External-FTS-tools — секцию
`[tool.kb.external_fts]` (`ExternalFtsConfig`); DSN там опционален —
пустой = переиспользуется `[tool.kb].dsn` через тот же `PostgresPool`
из DI (один pool на оба канала, см. `external_fts.providers`).

Pipeline-граф (KB-tools):
- `PostgresPool` (boba-db-postgres, configure=register_vector) собран
  Dishka-провайдером в [providers.py](providers.py); все Scope.APP,
  синглтоны на lifetime агента. `provide_postgres_pool` — generator-provider:
  pool гарантированно закрывается на `Agent.close()`. `external_fts`
  переиспользует тот же `PostgresPool` через DI (FromDI).
- FS-ingest: `FsWalkRequestSource` + `FsTransport` (boba-transport-fs) +
  DispatchReader (boba-indexing) + Reader'ы (`boba-kbdoc` + `boba-markdown` +
  `boba-html`) + `StructuralChunker` + `OverlapCharSplitter` (boba-text) +
  `CollectionScopedView` собираются inline в `kb_ingest`.
- Confluence-ingest: `{Pages|Cql|Space}RequestSource` + `HttpTransport` +
  `ConfluenceJsonDecoder` (`decoders=[…]`) + `ConfluenceReader` + тот же
  chunker/view, что и FS — inline в `kb_ingest_confluence`.
- External-FTS: `PgFtsKnowledgeBase` поверх whitelist-`IndexSpec` —
  один SQL на `websearch_to_tsquery` + `ts_rank_cd` + `ts_headline`,
  identifier'ы из конфига подставляются через `psycopg.sql`.

Schema: kb_chunks (все коллекции, разделены колонкой `collection`) +
kb_collections (table-level metadata) — для kb_search/kb_ingest.
External-FTS работает по таблицам оператора (чужая схема).
Бутстрап миграций — в `migrations.apply_bootstrap`, вызывается при
создании пула.
"""

from __future__ import annotations

from boba.tool.kb.config import KbPluginConfig
from boba.tool.kb.confluence import (
    ConfluencePluginConfig,
    confluence_page_download,
    confluence_page_download_markdown,
    confluence_page_outline,
    confluence_page_section,
    confluence_search,
)
from boba.tool.kb.confluence_ingest_config import ConfluenceIngestConfig
from boba.tool.kb.external_fts import (
    ExternalFtsConfig,
    fts_list_indexes,
    fts_search,
    provide_external_fts_kb,
)
from boba.tool.kb.kb import PostgresKnowledgeBase
from boba.tool.kb.kb_ingest import kb_ingest
from boba.tool.kb.kb_ingest_confluence import kb_ingest_confluence
from boba.tool.kb.kb_list_collections import kb_list_collections
from boba.tool.kb.kb_search import kb_search
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
from boba.tool.kb.vector_store import PostgresVectorStore

__all__ = [
    "ConfluenceIngestConfig",
    "ConfluencePluginConfig",
    "ExternalFtsConfig",
    "KbPluginConfig",
    "PostgresKnowledgeBase",
    "PostgresVectorStore",
    "confluence_page_download",
    "confluence_page_download_markdown",
    "confluence_page_outline",
    "confluence_page_section",
    "confluence_search",
    "fts_list_indexes",
    "fts_search",
    "kb_ingest",
    "kb_ingest_confluence",
    "kb_list_collections",
    "kb_search",
    "provide_chunker",
    "provide_dispatch_reader",
    "provide_embedder",
    "provide_external_fts_kb",
    "provide_html_reader",
    "provide_kbdoc_reader",
    "provide_knowledge_base",
    "provide_postgres_pool",
    "provide_vector_store",
]
