"""boba-indexing: контракты Source/Reader/Chunker/Store + IndexPipeline."""

from __future__ import annotations

from boba.indexing.chunker import Chunker, ChunkerFactory, ChunkerId
from boba.indexing.chunks import Chunk, ChunkSummary
from boba.indexing.collections import CollectionInfo
from boba.indexing.context import IndexingContext, PipelineId
from boba.indexing.errors import (
    IndexingError,
    NoMatchingReaderError,
    SyncUnsupportedError,
)
from boba.indexing.extension import IndexerExtensionContext
from boba.indexing.items import SourceItem
from boba.indexing.pipeline import IndexPipeline
from boba.indexing.reader import Reader, ReaderDispatcher, ReaderId
from boba.indexing.registry import (
    ChunkerRegistry,
    ReaderProvider,
    ReaderRegistry,
    SourceRegistry,
    StoreRegistry,
)
from boba.indexing.sections import Section
from boba.indexing.source import Source, SourceFactory, SourceId
from boba.indexing.stats import IndexStats, IndexStatsBuilder
from boba.indexing.store import Store, StoreFactory, StoreId

__all__ = [
    "Chunk",
    "ChunkSummary",
    "Chunker",
    "ChunkerFactory",
    "ChunkerId",
    "ChunkerRegistry",
    "CollectionInfo",
    "IndexPipeline",
    "IndexStats",
    "IndexStatsBuilder",
    "IndexerExtensionContext",
    "IndexingContext",
    "IndexingError",
    "NoMatchingReaderError",
    "PipelineId",
    "Reader",
    "ReaderDispatcher",
    "ReaderId",
    "ReaderProvider",
    "ReaderRegistry",
    "Section",
    "Source",
    "SourceFactory",
    "SourceId",
    "SourceItem",
    "SourceRegistry",
    "Store",
    "StoreFactory",
    "StoreId",
    "StoreRegistry",
    "SyncUnsupportedError",
]
