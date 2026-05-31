"""
boba-tool-kb — плагин: KB-tools поверх postgres+pgvector + Confluence + FTS
"""

from __future__ import annotations

from boba.db.postgres import PostgresConnection
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.download import (
    ConfluenceDownloadConfig,
    confluence_download,
)
from boba.tool.kb.confluence.fetch import (
    ConfluenceFetchPageConfig,
    confluence_fetch_page,
)
from boba.tool.kb.confluence.ingest import (
    ConfluenceIngestConfig,
    confluence_ingest_cql,
    confluence_ingest_pages,
    confluence_ingest_spaces,
)
from boba.tool.kb.confluence.list_spaces import (
    ConfluenceListSpacesConfig,
    confluence_list_spaces,
)
from boba.tool.kb.confluence.search_cql import (
    ConfluenceSearchCqlConfig,
    confluence_search_cql,
)
from boba.tool.kb.confluence_doc.ingest import (
    ConfluenceDocIngestConfig,
    confluence_doc_ingest_paths,
)
from boba.tool.kb.core.chunking import ChunkerParams
from boba.tool.kb.core.embedding import EmbeddingModel
from boba.tool.kb.core.kb import PostgresKnowledgeBase, PostgresKnowledgeBaseConfig
from boba.tool.kb.core.postgres import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresStoreConfig,
    PostgresStoreSchema,
)
from boba.tool.kb.core.search import (
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
