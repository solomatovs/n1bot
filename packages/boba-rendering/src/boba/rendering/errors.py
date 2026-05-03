"""Ошибки rendering-домена."""

from __future__ import annotations

__all__ = ["RenderingError", "UnsupportedResultTypeError"]


class RenderingError(Exception):
    """База ошибок rendering-домена."""


class UnsupportedResultTypeError(RenderingError):
    """Renderer не умеет отрисовать ToolResult-подтип."""

    def __init__(self, renderer_id: str, result_type: str) -> None:
        super().__init__(
            f"renderer {renderer_id!r} cannot render {result_type!r}"
        )
        self.renderer_id = renderer_id
        self.result_type = result_type
