"""Domain-исключения ConfluenceSearchPipeline.

Контракт ошибок для caller'ов (tool, CLI): всё, что может пойти не так
в `pipeline.run()`, оборачивается в `ConfluenceSearchPipelineError` или
один из его подклассов. Никаких «голых» `httpx.HTTPError` или
`KeyError` наружу не пробрасывается.
"""

from __future__ import annotations

__all__ = [
    "ConfluenceSearchHttpError",
    "ConfluenceSearchPipelineError",
    "ConfluenceSearchResponseError",
]


class ConfluenceSearchPipelineError(Exception):
    """База всех ошибок ConfluenceSearchPipeline."""


class ConfluenceSearchHttpError(ConfluenceSearchPipelineError):
    """HTTP-уровень: connect/timeout/non-2xx от Confluence REST."""


class ConfluenceSearchResponseError(ConfluenceSearchPipelineError):
    """Response не соответствует ожидаемой схеме (отсутствуют поля, не JSON)."""
