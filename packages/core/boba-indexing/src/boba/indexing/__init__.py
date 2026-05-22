"""
boba.indexing — абстракции для индексации документов
"""

from __future__ import annotations

from boba.indexing.chunk_id import (
    ChunkIdGenerator,
    DigestPrefix,
    FixedDigestPrefix,
    SourceBasedChunkId,
)
from boba.indexing.chunk_sink import ChunkSink, VectorStoreChunkSink
from boba.indexing.chunk_store import (
    ChunkStore,
    CollectionInfo,
    CollectionsStore,
    HashDiff,
)
from boba.indexing.chunker import Chunker, ChunkerId
from boba.indexing.chunks import (
    Chunk,
    ChunkId,
    ChunkKeys,
    ChunkLocation,
    ChunkSummary,
    EmbeddedChunk,
)
from boba.indexing.cleanup import (
    CleanupContext,
    CleanupStrategy,
    FullCleanup,
    IncrementalCleanup,
    NoneCleanup,
)
from boba.indexing.collection_scoped_view import CollectionScopedView
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
from boba.indexing.dispatch_reader import DispatchReader
from boba.indexing.embedder import Embedder
from boba.indexing.errors import (
    IncompatibleContentError,
    IndexingError,
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
from boba.indexing.format_plan import FormatBlock, FormatPlan
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
from boba.indexing.reader import Reader, ReaderId
from boba.indexing.request import Request, RequestSource
from boba.indexing.runtime_pipeline import RuntimePipeline
from boba.indexing.sections import (
    HeadingSection,
    ParagraphSection,
    Section,
    SectionKeys,
    SourceId,
)
from boba.indexing.splitter import (
    LengthFunction,
    SplitPiece,
    Splitter,
)
from boba.indexing.stats import IndexStats, IndexStatsBuilder
from boba.indexing.streaming_indexer import StreamingIndexer
from boba.indexing.transport import Transport

__all__ = [
    "And",
    "BaseIndexEvent",
    "BatchStarted",
    "BatchUpserted",
    "BinaryStream",
    "BytesContentHash",
    "Chunk",
    "ChunkId",
    "ChunkIdGenerator",
    "ChunkKeys",
    "ChunkLocation",
    "ChunkSink",
    "ChunkStore",
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
    "CollectionScopedView",
    "CollectionsStore",
    "CompletedItem",
    "ContentHash",
    "Decoder",
    "DecoderId",
    "DigestPrefix",
    "DispatchReader",
    "EmbeddedChunk",
    "Embedder",
    "Eq",
    "Filter",
    "FixedDigestPrefix",
    "FormatBlock",
    "FormatPlan",
    "FullCleanup",
    "Gt",
    "Gte",
    "HasAllTags",
    "HasAnyTag",
    "HasTag",
    "HashDiff",
    "HeadingSection",
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
    "ParagraphSection",
    "PassThroughDecoder",
    "PhaseTransition",
    "PipelineContext",
    "PipelineId",
    "RawDocument",
    "Reader",
    "ReaderId",
    "ReaderKeys",
    "ReconcileSummary",
    "Request",
    "RequestSource",
    "RunFinished",
    "RunId",
    "RunStarted",
    "RuntimePipeline",
    "Section",
    "SectionKeys",
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
    "TrackingKeys",
    "Transport",
    "TransportKeys",
    "UnsupportedFilterError",
    "VectorStoreChunkSink",
]
