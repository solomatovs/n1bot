"""RawDocument — открытый handle + metadata от Transport; lifecycle handle'а контролирует Transport, не Reader."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from boba.indexing.metadata import Metadata
from boba.indexing.sections import SourceId

__all__ = ["BinaryStream", "RawDocument"]


class BinaryStream(Protocol):
    """Минимальный read-only handle: всё, что имеет read() -> bytes (BufferedReader, BytesIO, streaming-адаптер)."""

    def read(self, n: int = -1, /) -> bytes: ...


@dataclass(frozen=True)
class RawDocument:
    """Открытый handle + metadata; lifecycle handle'а — у Transport."""

    handle: BinaryStream
    """Уже открытый handle; Reader читает, закрытие — обязанность Transport'а."""

    source_id: SourceId
    """Identity документа; выводит и проставляет Transport (Transport.source_id)."""

    metadata: Metadata = field(default_factory=Metadata.empty)
    """Request.metadata + transport-specific keys; Reader/Chunker мержат свои ключи поверх."""
