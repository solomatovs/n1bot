"""Boba rendering: ToolResult subtypes + Visitor protocol для double-dispatch."""

from __future__ import annotations

from boba.rendering.errors import RenderingError, UnsupportedResultTypeError
from boba.rendering.result import (
    JsonResult,
    TextResult,
    ToolResult,
    ToolResultVisitor,
)

__all__ = [
    "JsonResult",
    "RenderingError",
    "TextResult",
    "ToolResult",
    "ToolResultVisitor",
    "UnsupportedResultTypeError",
]
