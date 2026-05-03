"""Ошибки доменного слоя индексации."""

from __future__ import annotations

__all__ = [
    "IndexingError",
    "NoMatchingReaderError",
]


class IndexingError(Exception):
    """База ошибок indexing-домена."""


class NoMatchingReaderError(IndexingError):
    """Ни один Reader не принимает SourceItem (по content_hint)."""

    def __init__(self, source_id: str, content_hint: str) -> None:
        super().__init__(
            f"no Reader accepts source_id={source_id!r} hint={content_hint!r}"
        )
        self.source_id = source_id
        self.content_hint = content_hint
