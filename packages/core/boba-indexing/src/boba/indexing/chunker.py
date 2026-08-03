"""Chunker[T] — интерфейс нарезки Section[T] на Chunk[T]."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Generic, NewType, TypeVar

from boba.indexing.chunks import Chunk
from boba.indexing.sections import Section

__all__ = ["Chunker", "ChunkerId"]

T = TypeVar("T")


ChunkerId = NewType("ChunkerId", str)
"""Идентификатор Chunker-реализации (например 'sliding', 'heading')."""


class Chunker(ABC, Generic[T]):
    """Преобразует поток Section[T] в поток Chunk[T].

    chunk_id детерминирован (re-index), chunk_index сквозной по source_id, content_hash обязан заполнить сам Chunker.
    """

    @abstractmethod
    def chunker_id(self) -> ChunkerId: ...

    @abstractmethod
    def chunk(self, sections: Iterable[Section[T]]) -> Iterable[Chunk[T]]: ...
