"""Ошибки доменного слоя индексации."""

from __future__ import annotations

__all__ = [
    "IncompatibleContentError",
    "IndexingError",
    "SyncUnsupportedError",
    "UnknownPipelineError",
]


class IndexingError(Exception):
    """База ошибок indexing-домена."""


class IncompatibleContentError(IndexingError):
    """Reader получил RawDocument, который он не может распарсить.

    Сигнализирует ошибку сборки pipeline (transport отдал не тот content,
    или RequestSource генерит неверные URL'ы). Не fallback, не recovery —
    pipeline должен упасть, оператор должен исправить конфигурацию.
    """

    def __init__(self, reader_id: str, canonical_id: str, reason: str) -> None:
        super().__init__(
            f"reader {reader_id!r} cannot parse canonical_id={canonical_id!r}: {reason}"
        )
        self.reader_id = reader_id
        self.canonical_id = canonical_id
        self.reason = reason


class SyncUnsupportedError(IndexingError):
    """RequestSource не умеет перечислять canonical_id'ы (бесконечный стрим)."""

    def __init__(self, source_name: str) -> None:
        super().__init__(
            f"request source {source_name!r} does not support listing canonical_ids"
        )
        self.source_name = source_name


class UnknownPipelineError(IndexingError):
    """Pipeline-id не зарегистрирован в `PipelineRegistry`."""
