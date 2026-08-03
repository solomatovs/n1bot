"""Стратегии вычисления стабильного ChunkId по обязательным полям Section[T]."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar, runtime_checkable

from boba.indexing.chunks import ChunkId
from boba.indexing.key_encoder import KeyEncoder
from boba.indexing.sections import Section

__all__ = [
    "ChunkIdGenerator",
    "DigestPrefix",
    "FixedDigestPrefix",
    "SourceBasedChunkId",
]

T = TypeVar("T")


@runtime_checkable
class DigestPrefix(Protocol):
    """Возвращает длину digest-префикса (в hex-символах) для ChunkId."""

    def length(self) -> int: ...


@dataclass(frozen=True)
class FixedDigestPrefix(DigestPrefix):
    """Фиксированная длина digest-префикса в hex-символах."""

    chars: int

    def length(self) -> int:
        return self.chars


class ChunkIdGenerator(Generic[T]):
    """Стратегия вычисления стабильного ChunkId по Section[T] и индексу."""

    @abstractmethod
    def compute(self, section: Section[T], chunk_index: int) -> ChunkId:
        """Вернуть стабильный ChunkId по Section[T] и порядковому индексу."""
        ...


class SourceBasedChunkId(ChunkIdGenerator[T]):
    """Генерирует ChunkId из source_id + chunk_index."""

    def __init__(
        self,
        encoder: KeyEncoder[str],
        prefix: DigestPrefix,
    ) -> None:
        self._encoder = encoder
        self._prefix = prefix

    def compute(self, section: Section[T], chunk_index: int) -> ChunkId:
        hash_obj = self._encoder.encode(section.source_id)
        return self.chunk_id_from_digest(
            hash_obj.to_wire(),
            chunk_index=chunk_index,
            prefix_length=self._prefix.length(),
        )

    @staticmethod
    def chunk_id_from_digest(
        digest: str,
        chunk_index: int,
        prefix_length: int,
    ) -> ChunkId:
        """Скомпоновать ChunkId из digest'а и индекса чанка."""
        return ChunkId(f"{digest[:prefix_length]}:{chunk_index}")
