"""boba-indexing: контракты RequestSource/Transport/Reader/Chunker/Store + Pipeline."""

from __future__ import annotations

from boba.indexing.auth import AuthApplier
from boba.indexing.chunker import Chunker, ChunkerId
from boba.indexing.chunks import Chunk, ChunkSummary
from boba.indexing.collections import CollectionInfo
from boba.indexing.context import IndexingContext, PipelineId
from boba.indexing.decoder import Decoder, DecoderId, IdentityDecoder
from boba.indexing.errors import (
    IncompatibleContentError,
    IndexingError,
    SyncUnsupportedError,
    UnknownPipelineError,
)
from boba.indexing.extension import IndexerExtensionContext
from boba.indexing.pipeline import IndexPipeline
from boba.indexing.pipeline_factory import PipelineRegistry, PipelineSpec
from boba.indexing.raw_document import BinaryStream, RawDocument
from boba.indexing.reader import Reader, ReaderId
from boba.indexing.request import Request
from boba.indexing.request_source import RequestSource
from boba.indexing.sections import Section
from boba.indexing.stats import IndexStats, IndexStatsBuilder
from boba.indexing.store import Store, StoreId
from boba.indexing.transport import Transport

__all__ = [
    "AuthApplier",
    "BinaryStream",
    "Chunk",
    "ChunkSummary",
    "Chunker",
    "ChunkerId",
    "CollectionInfo",
    "Decoder",
    "DecoderId",
    "IdentityDecoder",
    "IncompatibleContentError",
    "IndexPipeline",
    "IndexStats",
    "IndexStatsBuilder",
    "IndexerExtensionContext",
    "IndexingContext",
    "IndexingError",
    "PipelineId",
    "PipelineRegistry",
    "PipelineSpec",
    "RawDocument",
    "Reader",
    "ReaderId",
    "Request",
    "RequestSource",
    "Section",
    "Store",
    "StoreId",
    "SyncUnsupportedError",
    "Transport",
    "UnknownPipelineError",
]
