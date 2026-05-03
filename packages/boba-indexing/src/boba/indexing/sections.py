"""Section: что Reader выдаёт в Chunker."""

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

    `content_hash` — копия `SourceItem.content_hash`, протекает через всю
    цепочку до Store для skip-if-unchanged логики Pipeline.
    """

    source_id: str
    text: str
    anchor: str | None = None
    order: int = 0
    content_hash: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)
