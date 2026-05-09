"""
boba.indexing — абстракции для индексации документов
"""

from __future__ import annotations

from boba.indexing.chunk_id import (
    AnchorBasedChunkId,
    ChunkIdStrategy,
    DigestPrefix,
    FixedDigestPrefix,
    SourceBasedChunkId,
)
from boba.indexing.chunk_sink import ChunkSink, VectorStoreChunkSink
from boba.indexing.chunker import Chunker, ChunkerId
from boba.indexing.chunks import Chunk, ChunkId, ChunkLocation, ChunkSummary
from boba.indexing.cleanup import (
    CleanupContext,
    CleanupStrategy,
    FullCleanup,
    IncrementalCleanup,
    NoneCleanup,
)
from boba.indexing.content_hash import (
    BytesContentHash,
    ContentHash,
    IntContentHash,
    StringContentHash,
)
from boba.indexing.context import (
    CollectionId,
    NamespaceId,
    PipelineContext,
    PipelineId,
)
from boba.indexing.decoder import Decoder, DecoderId, PassThroughDecoder
from boba.indexing.embedder import Embedder
from boba.indexing.errors import (
    IncompatibleContentError,
    IndexingError,
    SyncUnsupportedError,
)
from boba.indexing.events import (
    BaseIndexEvent,
    BatchStarted,
    BatchUpserted,
    ChunksDeleted,
    CleanupStarted,
    CompletedItem,
    IndexEvent,
    PhaseTransition,
    RunFinished,
    RunId,
    RunStarted,
    Severity,
    SourceFailed,
    SourceIndexed,
    SourceSkippedUnchanged,
)
from boba.indexing.filter import (
    And,
    Eq,
    Filter,
    Gt,
    Gte,
    HasAllTags,
    HasAnyTag,
    HasTag,
    In,
    Lt,
    Lte,
    Ne,
    Not,
    NotIn,
    Or,
    UnsupportedFilterError,
)
from boba.indexing.index_views import (
    IndexQuery,
    IndexSink,
    ReconcileSummary,
    TrackingKeys,
)
from boba.indexing.indexer import Indexer, IndexerConfig
from boba.indexing.key_encoder import KeyEncoder, Sha256TextEncoder
from boba.indexing.metadata import (
    ChunkerKeys,
    Metadata,
    MetadataKey,
    ReaderKeys,
    TransportKeys,
)
from boba.indexing.namespaced_view import NamespacedView
from boba.indexing.raw_document import BinaryStream, RawDocument
from boba.indexing.reader import PlainTextReader, Reader, ReaderId
from boba.indexing.request import Request, RequestSource
from boba.indexing.section_chunker import SectionChunker
from boba.indexing.sections import Section, SourceId
from boba.indexing.splitter import (
    LengthFunction,
    RecursiveCharSplitter,
    SplitPiece,
    Splitter,
)
from boba.indexing.stats import IndexStats, IndexStatsBuilder
from boba.indexing.streaming_indexer import StreamingIndexer
from boba.indexing.transport import Transport
from boba.indexing.vector_store import (
    CollectionInfo,
    CollectionsAdminReader,
    CollectionsAdminWriter,
    SearchHit,
    VectorStoreReader,
    VectorStoreWriter,
)

__all__ = [
    "AnchorBasedChunkId",
    "And",
    "BaseIndexEvent",
    "BatchStarted",
    "BatchUpserted",
    "BinaryStream",
    "BytesContentHash",
    "Chunk",
    "ChunkId",
    "ChunkIdStrategy",
    "ChunkLocation",
    "ChunkSink",
    "ChunkSummary",
    "Chunker",
    "ChunkerId",
    "ChunkerKeys",
    "ChunksDeleted",
    "CleanupContext",
    "CleanupStarted",
    "CleanupStrategy",
    "CollectionId",
    "CollectionInfo",
    "CollectionsAdminReader",
    "CollectionsAdminWriter",
    "CompletedItem",
    "ContentHash",
    "Decoder",
    "DecoderId",
    "DigestPrefix",
    "Embedder",
    "Eq",
    "Filter",
    "FixedDigestPrefix",
    "FullCleanup",
    "Gt",
    "Gte",
    "HasAllTags",
    "HasAnyTag",
    "HasTag",
    "In",
    "IncompatibleContentError",
    "IncrementalCleanup",
    "IndexEvent",
    "IndexQuery",
    "IndexSink",
    "IndexStats",
    "IndexStatsBuilder",
    "Indexer",
    "IndexerConfig",
    "IndexingError",
    "IntContentHash",
    "KeyEncoder",
    "LengthFunction",
    "Lt",
    "Lte",
    "Metadata",
    "MetadataKey",
    "NamespaceId",
    "NamespacedView",
    "Ne",
    "NoneCleanup",
    "Not",
    "NotIn",
    "Or",
    "PassThroughDecoder",
    "PhaseTransition",
    "PipelineContext",
    "PipelineId",
    "PlainTextReader",
    "RawDocument",
    "Reader",
    "ReaderId",
    "ReaderKeys",
    "ReconcileSummary",
    "RecursiveCharSplitter",
    "Request",
    "RequestSource",
    "RunFinished",
    "RunId",
    "RunStarted",
    "SearchHit",
    "Section",
    "SectionChunker",
    "Severity",
    "Sha256TextEncoder",
    "SourceBasedChunkId",
    "SourceFailed",
    "SourceId",
    "SourceIndexed",
    "SourceSkippedUnchanged",
    "SplitPiece",
    "Splitter",
    "StreamingIndexer",
    "StringContentHash",
    "SyncUnsupportedError",
    "TrackingKeys",
    "Transport",
    "TransportKeys",
    "UnsupportedFilterError",
    "VectorStoreChunkSink",
    "VectorStoreReader",
    "VectorStoreWriter",
]
