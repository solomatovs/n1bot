"""
boba.indexing — абстракции для индексации документов
"""

from __future__ import annotations

from boba.indexing.chunk_id import (
    AnchorBasedChunkId,
    ChunkIdStrategy,
    DigestPrefix,
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
    CompletedItem,
    IndexEvent,
    PhaseTransition,
    RunId,
    Severity,
)
from boba.indexing.indexer import Indexer, IndexerConfig
from boba.indexing.key_encoder import KeyEncoder
from boba.indexing.metadata import (
    ChunkerKeys,
    Metadata,
    MetadataKey,
    ReaderKeys,
    TransportKeys,
)
from boba.indexing.raw_document import BinaryStream, RawDocument
from boba.indexing.reader import Reader, ReaderId
from boba.indexing.records import (
    ListKeysQuery,
    RecordEntry,
    RecordManagerReader,
    RecordManagerWriter,
    RecordsAdmin,
)
from boba.indexing.request import Request, RequestSource
from boba.indexing.section_chunker import SectionChunker
from boba.indexing.sections import Section, SourceId
from boba.indexing.splitter import LengthFunction, SplitPiece, Splitter
from boba.indexing.stats import IndexStats, IndexStatsBuilder
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
    "BaseIndexEvent",
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
    "CleanupContext",
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
    "FullCleanup",
    "IncompatibleContentError",
    "IncrementalCleanup",
    "IndexEvent",
    "IndexStats",
    "IndexStatsBuilder",
    "Indexer",
    "IndexerConfig",
    "IndexingError",
    "IntContentHash",
    "KeyEncoder",
    "LengthFunction",
    "ListKeysQuery",
    "Metadata",
    "MetadataKey",
    "NamespaceId",
    "NoneCleanup",
    "PassThroughDecoder",
    "PhaseTransition",
    "PipelineContext",
    "PipelineId",
    "RawDocument",
    "Reader",
    "ReaderId",
    "ReaderKeys",
    "RecordEntry",
    "RecordManagerReader",
    "RecordManagerWriter",
    "RecordsAdmin",
    "Request",
    "RequestSource",
    "RunId",
    "SearchHit",
    "Section",
    "SectionChunker",
    "Severity",
    "SourceBasedChunkId",
    "SourceId",
    "SplitPiece",
    "Splitter",
    "StringContentHash",
    "SyncUnsupportedError",
    "Transport",
    "TransportKeys",
    "VectorStoreChunkSink",
    "VectorStoreReader",
    "VectorStoreWriter",
]
