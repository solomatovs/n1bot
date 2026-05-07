"""boba-indexing: Chunker/Chunk/Store/IndexPipeline + декларативная фабрика.

Generic-стадии (RequestSource/Transport/Reader/Decoder/Section/RawDocument/
IndexingContext/PipelineId/AuthApplier/IndexingError/IncompatibleContentError/
SyncUnsupportedError) живут в `boba.processing` — этот пакет от него зависит.
"""

from __future__ import annotations

from boba.indexing.chunker import Chunker, ChunkerId
from boba.indexing.chunks import Chunk, ChunkSummary
from boba.indexing.collections import CollectionInfo
from boba.indexing.pipeline import IndexPipeline
from boba.indexing.stats import IndexStats, IndexStatsBuilder
from boba.indexing.store import SearchHit, Store, StoreId

__all__ = [
    "Chunk",
    "ChunkSummary",
    "Chunker",
    "ChunkerId",
    "CollectionInfo",
    "IndexPipeline",
    "IndexStats",
    "IndexStatsBuilder",
    "SearchHit",
    "Store",
    "StoreId",
]
