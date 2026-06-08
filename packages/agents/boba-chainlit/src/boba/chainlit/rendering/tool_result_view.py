"""Маппинг ToolResult -> форма представления в UI (ToolResultRendering).

ToolResult теперь имеет не одну форму отображения: текстовые варианты
рендерятся markdown'ом, ChartResult — интерактивным графиком. Эта развилка
выражена sealed-union'ом ToolResultRendering, а ToolResultView.render() —
exhaustive match по вариантам ToolResult (новый вариант -> pyright потребует
решить, как его показывать). Dispatcher диспатчит по ToolResultRendering, не
зная о конкретных ToolResult-вариантах.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, assert_never

from boba.chainlit.rendering.result_markdown import ToolResultMarkdown
from boba.tools.domain import (
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
