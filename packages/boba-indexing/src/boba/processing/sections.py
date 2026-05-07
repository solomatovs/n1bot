"""Section: что Reader выдаёт следующей стадии (Chunker / Sink / Collector)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = ["Section"]


@dataclass(frozen=True)
class Section:
    """Логический фрагмент документа (раздел/heading/абзац).

    `anchor` — стабильный якорь внутри документа (heading-id, page-section).
    None — у плоских Reader'ов (txt). Heading-aware Chunker использует anchor,
    чтобы не пересекать границы секций.

    `order` — порядок в исходном документе; нужен для детерминизма chunk_id.

    `content_hash` для skip-if-unchanged живёт в `RawDocument.metadata` и
    проставляется Pipeline'ом в Chunk перед Store — Reader/Chunker про него
    не знают.
    """

    source_id: str
    text: str
    anchor: str | None = None
    order: int = 0
    metadata: Mapping[str, str] = field(default_factory=dict)
