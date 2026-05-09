"""
Chunker[T] - интерфейс для нарезки Section[T] на Chunk[T]
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from boba.indexing.chunks import Chunk
from boba.indexing.context import PipelineContext
from boba.indexing.sections import Section
from boba.patterns import StreamTransformer, StrId

__all__ = ["Chunker", "ChunkerId"]

T = TypeVar("T")


class ChunkerId(StrId):
    """Идентификатор Chunker-реализации (например 'sliding', 'heading')"""


class Chunker(
    StreamTransformer[PipelineContext, Section[T], Chunk[T]],
    ABC,
    Generic[T],
):
    """Преобразует поток Section[T] в поток Chunk[T] по модальности"""

    @abstractmethod
    def chunker_id(self) -> ChunkerId: ...
