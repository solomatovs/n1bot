"""ChunkIdStrategy: стратегия вычисления стабильного chunk_id для Section.

Strategy для DI в SectionChunker — отдельный класс на каждый способ построения
id (с anchor/без, по source_id/по hash содержимого, и т.п.).
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod

from boba.processing import Section

__all__ = [
    "AnchorBasedChunkId",
    "ChunkIdStrategy",
    "SourceBasedChunkId",
]


class ChunkIdStrategy(ABC):
    """Стратегия вычисления стабильного chunk_id."""

    @abstractmethod
    def compute(self, section: Section, chunk_index: int) -> str:
        """Вернуть стабильный id чанка по Section и его порядковому индексу."""
        ...


class SourceBasedChunkId(ChunkIdStrategy):
    """Id по source_id + chunk_index. Не учитывает anchor."""

    _DIGEST_PREFIX_LEN = 16

    def compute(self, section: Section, chunk_index: int) -> str:
        digest = hashlib.sha1(
            section.source_id.encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()
        return f"{digest[:self._DIGEST_PREFIX_LEN]}:{chunk_index}"


class AnchorBasedChunkId(ChunkIdStrategy):
    """Id по (source_id, anchor) + chunk_index. Сохраняет deep-link стабильным."""

    _DIGEST_PREFIX_LEN = 16

    def compute(self, section: Section, chunk_index: int) -> str:
        key = f"{section.source_id}#{section.anchor or ''}".encode()
        digest = hashlib.sha1(key, usedforsecurity=False).hexdigest()
        return f"{digest[:self._DIGEST_PREFIX_LEN]}:{chunk_index}"
