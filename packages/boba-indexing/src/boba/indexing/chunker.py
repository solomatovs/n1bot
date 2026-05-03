"""Chunker: Section → Chunk через StreamTransformer + Factory."""

from __future__ import annotations

from abc import ABC, abstractmethod

from boba.indexing.chunks import Chunk
from boba.indexing.context import IndexingContext
from boba.indexing.extension import IndexerExtensionContext
from boba.indexing.sections import Section
from boba.patterns import ContextItemProvider, StreamTransformer, StrId

__all__ = ["Chunker", "ChunkerFactory", "ChunkerId"]


class ChunkerId(StrId):
    """Идентификатор Chunker-реализации (например 'sliding', 'heading')."""


class Chunker(StreamTransformer[IndexingContext, Section, Chunk], ABC):
    """Преобразует поток Section в поток Chunk."""

    @abstractmethod
    def chunker_id(self) -> ChunkerId: ...


class ChunkerFactory(
    ContextItemProvider[IndexerExtensionContext, ChunkerId, Chunker],
    ABC,
):
    """Фабрика Chunker: AppConfig → готовый параметризованный Chunker."""

    @abstractmethod
    def id(self) -> ChunkerId: ...

    @abstractmethod
    def produce(self, ctx: IndexerExtensionContext) -> Chunker: ...
