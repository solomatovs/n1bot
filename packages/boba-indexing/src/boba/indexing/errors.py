"""Ошибки доменного слоя индексации."""

from __future__ import annotations

__all__ = [
    "IndexingError",
    "NoMatchingReaderError",
    "SyncUnsupportedError",
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


class SyncUnsupportedError(IndexingError):
    """Source не умеет перечислять source_id (бесконечный стрим)."""

    def __init__(self, source_factory_id: str) -> None:
        super().__init__(
            f"source {source_factory_id!r} does not support listing source_ids"
        )
        self.source_factory_id = source_factory_id
