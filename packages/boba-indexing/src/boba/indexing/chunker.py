"""Chunker: Section → Chunk через StreamTransformer."""

from __future__ import annotations

from abc import ABC, abstractmethod

from boba.indexing.chunks import Chunk
from boba.indexing.context import IndexingContext
from boba.indexing.sections import Section
from boba.patterns import StreamTransformer, StrId

__all__ = ["Chunker", "ChunkerId"]


class ChunkerId(StrId):
    """Идентификатор Chunker-реализации (например 'sliding', 'heading')."""


class Chunker(StreamTransformer[IndexingContext, Section, Chunk], ABC):
    """Преобразует поток Section в поток Chunk."""

    @abstractmethod
    def chunker_id(self) -> ChunkerId: ...
