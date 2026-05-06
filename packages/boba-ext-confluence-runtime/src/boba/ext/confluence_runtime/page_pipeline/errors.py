"""Domain-исключения ConfluencePagePipeline.

Контракт ошибок: всё, что может пойти не так в `pipeline.fetch()`,
оборачивается в `ConfluencePagePipelineError` или один из подклассов.
Никаких голых `httpx.HTTPError` / `KeyError` наружу.
"""

from __future__ import annotations

__all__ = [
    "ConfluencePageHttpError",
    "ConfluencePageNotFoundError",
    "ConfluencePagePipelineError",
    "ConfluencePageResponseError",
]


class ConfluencePagePipelineError(Exception):
    """База всех ошибок ConfluencePagePipeline."""


class ConfluencePageHttpError(ConfluencePagePipelineError):
    """HTTP-уровень: connect/timeout/non-2xx (кроме 404 — выделено)."""


class ConfluencePageNotFoundError(ConfluencePagePipelineError):
    """Confluence вернул 404 — страницы с этим page_id нет."""


class ConfluencePageResponseError(ConfluencePagePipelineError):
    """Response не соответствует ожидаемой схеме (нет body/title/version)."""
