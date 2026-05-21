"""boba-tool-postgres — плагин: postgres + pgvector knowledge-base tools.

Tools:
- `kb_search`            — hybrid (vector + FTS, RRF) поиск по коллекции
                           (FromDI PostgresKnowledgeBase).
- `kb_list_collections`  — список коллекций.
- `kb_ingest`            — индексация заранее настроенной папки в коллекцию
                           через `StreamingIndexer` из `boba.indexing`.
                           Поддерживает `.md` (KbDocReader + MarkdownReader)
                           и `.html/.htm` (HtmlReader).

Все шарят `PostgresPluginConfig` (`[tool.postgres]`,
`BOBA_TOOL__POSTGRES__*`); `enable=true` включает их пакетом, allowlist
`tools` сужает.

Pipeline-граф:
- `PostgresPool` (boba-db-postgres, configure=register_vector) собран
  Dishka-провайдером в [providers.py](providers.py); все Scope.APP,
  синглтоны на lifetime агента. `provide_postgres_pool` — generator-provider:
  pool гарантированно закрывается на `Agent.close()`.
- `FsWalkRequestSource` + `FsTransport` (boba-transport-fs) + DispatchReader
  (boba-indexing) + Reader'ы (`boba-kbdoc` + `boba-markdown` + `boba-html`) +
  `StructuralChunker` + `OverlapCharSplitter` (boba-text) + `CollectionScopedView`
  собираются inline в `kb_ingest` tool.

Schema: kb_chunks (все коллекции, разделены колонкой `collection`) +
kb_collections (table-level metadata). Бутстрап миграций — в
`migrations.apply_bootstrap`, вызывается при создании пула.
"""

from __future__ import annotations

from boba.tool.postgres.config import PostgresPluginConfig
from boba.tool.postgres.kb import PostgresKnowledgeBase
from boba.tool.postgres.kb_ingest import kb_ingest
from boba.tool.postgres.kb_list_collections import kb_list_collections
from boba.tool.postgres.kb_search import kb_search
from boba.tool.postgres.providers import (
    provide_chunker,
    provide_dispatch_reader,
    provide_embedder,
    provide_html_reader,
    provide_kbdoc_reader,
    provide_knowledge_base,
    provide_postgres_pool,
    provide_vector_store,
)
from boba.tool.postgres.vector_store import PostgresVectorStore

__all__ = [
    "PostgresKnowledgeBase",
    "PostgresPluginConfig",
    "PostgresVectorStore",
    "kb_ingest",
    "kb_list_collections",
    "kb_search",
    "provide_chunker",
    "provide_dispatch_reader",
    "provide_embedder",
    "provide_html_reader",
    "provide_kbdoc_reader",
    "provide_knowledge_base",
    "provide_postgres_pool",
    "provide_vector_store",
]
