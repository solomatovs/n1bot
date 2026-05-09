"""
Section[T] - логический фрагмент документа (раздел/heading/абзац) с content'ом типа T
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

from boba.indexing.metadata import Metadata
from boba.patterns import StrId

__all__ = ["Section", "SourceId"]

T = TypeVar("T")


class SourceId(StrId):
    """Стабильный canonical id документа-источника (URL, fs-path, doc-key)."""


@dataclass(frozen=True)
class Section(Generic[T]):
    """Логический фрагмент документа (раздел/heading/абзац).

    `anchor` — якорь внутри документа - heading-id, page-section и т.п.
        None — у плоских Reader'ов.
        Heading-aware Chunker использует anchor, чтобы не пересекать границы секций.

    `order` — порядок в исходном документе; нужен для детерминизма chunk_id.
    """

    source_id: SourceId
    content: T
    anchor: str | None = None
    order: int = 0
    metadata: Metadata = field(default_factory=Metadata.empty)
