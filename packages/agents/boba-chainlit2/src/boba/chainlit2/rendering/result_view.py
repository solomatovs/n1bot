"""ToolResult -> форма представления в UI: markdown или график."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, assert_never

from boba.chainlit2.rendering.result_markdown import ToolResultMarkdown
from boba.toolkit.result import (
    ChartResult,
    ErrorResult,
    JsonResult,
    PgCopyTextResult,
    TableResult,
    TextResult,
    ToolResult,
)

__all__ = [
    "ChartRendering",
    "MarkdownRendering",
    "ToolResultRendering",
    "ToolResultView",
]


@dataclass(frozen=True)
class MarkdownRendering:
    """Результат показывается markdown-строкой (текст/json/таблица/ошибка)."""

    markdown: str


@dataclass(frozen=True)
class ChartRendering:
    """Результат показывается интерактивным Plotly-графиком."""

    spec: Mapping[str, Any]
    title: str | None


ToolResultRendering = MarkdownRendering | ChartRendering


class ToolResultView:
    """Выбирает форму представления для одного ToolResult."""

    def __init__(self, result: ToolResult) -> None:
        self._result = result

    def render(self) -> ToolResultRendering:
        match self._result:
            case ChartResult(spec=spec, title=title):
                return ChartRendering(spec=spec, title=title)
            case (
                TextResult()
                | JsonResult()
                | TableResult()
                | PgCopyTextResult()
                | ErrorResult()
            ):
                return MarkdownRendering(ToolResultMarkdown(self._result).render())
            case _ as never:
                assert_never(never)
