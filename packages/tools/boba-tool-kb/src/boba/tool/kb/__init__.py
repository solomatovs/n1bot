"""
boba-tool-kb — плагин: KB-tools поверх postgres+pgvector + Confluence + FTS
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
from boba.tool.kb.confluence_doc.tools.ingest import (
    ConfluenceDocIngestConfig,
    confluence_doc_ingest_paths,
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
from boba.tool.kb.core.tools.search.kb_search import (
    KbSearchConfig,
    kb_confluence_doc_search,
    kb_confluence_search,
)

__all__ = [
    "ChunkerParams",
    "ConfluenceConnection",
    "ConfluenceDocIngestConfig",
    "ConfluenceDownloadConfig",
    "ConfluenceFetchPageConfig",
    "ConfluenceIngestConfig",
    "ConfluenceListSpacesConfig",
    "ConfluenceSearchCqlConfig",
    "EmbeddingModel",
    "KbSearchConfig",
    "PostgresChunkStore",
    "PostgresCollectionsStore",
    "PostgresConnection",
    "PostgresKnowledgeBase",
    "PostgresKnowledgeBaseConfig",
    "PostgresStoreConfig",
    "PostgresStoreSchema",
    "confluence_doc_ingest_paths",
    "confluence_download",
    "confluence_fetch_page",
    "confluence_ingest_cql",
    "confluence_ingest_pages",
    "confluence_ingest_spaces",
    "confluence_list_spaces",
    "confluence_search_cql",
    "kb_confluence_doc_search",
    "kb_confluence_search",
]
