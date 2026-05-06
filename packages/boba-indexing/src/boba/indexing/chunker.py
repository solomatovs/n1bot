"""Chunker: Section → Chunk через StreamTransformer."""

from __future__ import annotations

from abc import ABC, abstractmethod

from boba.indexing.chunks import Chunk
from boba.patterns import StreamTransformer, StrId
from boba.processing import IndexingContext, Section

__all__ = ["Chunker", "ChunkerId"]


class ChunkerId(StrId):
    """Идентификатор Chunker-реализации (например 'sliding', 'heading')."""


class Chunker(StreamTransformer[IndexingContext, Section, Chunk], ABC):
    """Преобразует поток Section в поток Chunk."""

    @abstractmethod
    def chunker_id(self) -> ChunkerId: ...
