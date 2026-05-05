"""SlidingChunkerConfig — DTO с параметрами chunker'а."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "SlidingChunkerConfig",
]


DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 200


@dataclass(frozen=True)
class SlidingChunkerConfig:
    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
