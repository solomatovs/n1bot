"""boba-tool-postgres — плагин: postgres + pgvector knowledge-base tools.

Tools:
- `kb_search`            — hybrid (vector + FTS, RRF) поиск по коллекции
                           (FromDI PostgresKnowledgeBase).
- `kb_list_collections`  — список коллекций.
- `kb_ingest`            — индексация заранее настроенной папки в коллекцию.
                           Поддерживает `.md` (один файл = один pre-chunked
                           чанк) и `.html/.htm` (auto-split по H1/H2).

Все шарят `PostgresPluginConfig` (`[tool.postgres]`,
`BOBA_TOOL__POSTGRES__*`); `enable=true` включает их пакетом, allowlist
`tools` сужает.

Реальные ресурсы (`psycopg_pool.ConnectionPool`, `Embedder[str]`,
`PostgresVectorStore`, `FolderIndexer`) собираются Dishka-провайдерами
из [providers.py](providers.py); все Scope.APP, синглтоны на lifetime
агента. `provide_postgres_pool` — generator-provider: pool гарантированно
закрывается на `Agent.close()`.

Schema: kb_chunks (все коллекции, разделены колонкой `collection`) +
kb_collections (table-level metadata). Бутстрап миграций — в
`migrations.apply_bootstrap`, вызывается при создании пула.
"""

from __future__ import annotations

from boba.tool.postgres.config import PostgresPluginConfig
from boba.tool.postgres.folder_indexer import FolderIndexer
from boba.tool.postgres.html_chunk import HtmlChunkParser
from boba.tool.postgres.kb_ingest import kb_ingest
from boba.tool.postgres.kb_list_collections import kb_list_collections
from boba.tool.postgres.kb_search import kb_search
from boba.tool.postgres.md_chunk import MdChunkParser
from boba.tool.postgres.providers import (
    provide_embedder,
    provide_embedder_factory,
    provide_folder_indexer,
    provide_html_chunk_parser,
    provide_knowledge_base,
    provide_md_chunk_parser,
    provide_postgres_pool,
    provide_vector_store,
)
from boba.tool.postgres.vector_store import PostgresVectorStore

__all__ = [
    "FolderIndexer",
    "HtmlChunkParser",
    "MdChunkParser",
    "PostgresPluginConfig",
    "PostgresVectorStore",
    "kb_ingest",
    "kb_list_collections",
    "kb_search",
    "provide_embedder",
    "provide_embedder_factory",
    "provide_folder_indexer",
    "provide_html_chunk_parser",
    "provide_knowledge_base",
    "provide_md_chunk_parser",
    "provide_postgres_pool",
    "provide_vector_store",
]
