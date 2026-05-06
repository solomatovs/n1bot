"""Ошибки indexing-специфичного слоя.

Generic ошибки processing-домена (`IndexingError`, `IncompatibleContentError`,
`SyncUnsupportedError`) живут в `boba.processing.errors`.
"""

from __future__ import annotations

from boba.processing.errors import IndexingError

__all__ = ["UnknownPipelineError"]


class UnknownPipelineError(IndexingError):
    """Pipeline-id не зарегистрирован в `PipelineRegistry`."""
