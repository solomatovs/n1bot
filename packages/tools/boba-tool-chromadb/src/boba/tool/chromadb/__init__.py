"""boba-tool-chromadb — v2 плагин: ChromaDB knowledge-base tools.

Tools:
- `kb_search`            — semantic search по коллекции (FromDI ChromaKnowledgeBase).
- `kb_list_collections`  — список коллекций.
- `kb_ingest`            — индексация заранее настроенной папки .md → коллекция.

Все шарят `ChromadbPluginConfig` (`[tool.chromadb]`,
`BOBA_TOOL__CHROMADB__*`); `enable=true` включает их пакетом,
allowlist `tools` сужает.

Реальные ресурсы (`chromadb.PersistentClient`, `Embedder[str]`,
`ChromaVectorStore`, `MdFolderIndexer`) собираются Dishka-провайдерами
из [providers.py](providers.py); все Scope.APP, синглтоны на lifetime
агента. `provide_chroma_client` — generator-provider: PersistentClient
гарантированно отпускается на `Agent.close()`.
"""

from __future__ import annotations

from boba.tool.chromadb.config import ChromadbPluginConfig
from boba.tool.chromadb.kb_ingest import KbIngestTool
from boba.tool.chromadb.kb_list_collections import KbListCollectionsTool
from boba.tool.chromadb.kb_search import KbSearchTool
from boba.tool.chromadb.providers import (
    provide_chroma_client,
    provide_embedder,
    provide_embedder_factory,
    provide_knowledge_base,
    provide_md_chunk_parser,
    provide_md_folder_indexer,
    provide_vector_store,
)
from boba.tool.chromadb.vector_store import ChromaVectorStore

__all__ = [
    "ChromaVectorStore",
    "ChromadbPluginConfig",
    "KbIngestTool",
    "KbListCollectionsTool",
    "KbSearchTool",
    "provide_chroma_client",
    "provide_embedder",
    "provide_embedder_factory",
    "provide_knowledge_base",
    "provide_md_chunk_parser",
    "provide_md_folder_indexer",
    "provide_vector_store",
]
