"""Представление ToolResult: выбор формы, markdown-рендер, plotly-элемент."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, assert_never

from tabulate import tabulate

import chainlit as cl
from boba.toolkit.result import (
    AffectedSqlResult,
    ChartResult,
    CustomElementResult,
    DiagramResult,
    ErrorResult,
    JsonResult,
    PgCopyTextResult,
    TableResult,
    TextResult,
    ToolResult,
)
from chainlit.element import ElementDisplay

__all__ = [
    "ChartRendering",
    "CustomElementRendering",
    "DiagramRendering",
    "MarkdownRendering",
    "ToolResultMarkdown",
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

    def chat_element(self, *, display: ElementDisplay = "inline") -> cl.Plotly:
        """cl.Plotly из spec — единственное место, знающее про plotly."""
        from plotly import graph_objects as go  # noqa: PLC0415

        figure = go.Figure(dict(self.spec))
        return cl.Plotly(name=self.title or "chart", figure=figure, display=display)


@dataclass(frozen=True)
class CustomElementRendering:
    """Результат показывается кастомным jsx-компонентом public/elements/<element>.jsx."""

    element: str
    props: Mapping[str, Any]
    title: str | None

    def chat_element(self, *, display: ElementDisplay = "inline") -> cl.CustomElement:
        return cl.CustomElement(
            name=self.element, props=dict(self.props), display=display
        )


@dataclass(frozen=True)
class DiagramRendering:
    """Результат показывается отрисованной диаграммой mermaid.

    В ленте — компактная карточка того же CanvasView, что рисует панель;
    клик по ней показывает файл в канвасе.
    """

    ELEMENT: ClassVar[str] = "CanvasView"

    spec: str
    path: str
    title: str | None

    def chat_element(self, *, display: ElementDisplay = "inline") -> cl.CustomElement:
        label = self.title
        if not label:
            label = self.path

        props = {
            "kind": "mermaid",
            "path": self.path,
            "label": label,
            "text": self.spec,
            "preview": True,
        }
        return cl.CustomElement(name=self.ELEMENT, props=props, display=display)


ToolResultRendering = (
    MarkdownRendering | ChartRendering | CustomElementRendering | DiagramRendering
)


class ToolResultView:
    """Выбирает форму представления для одного ToolResult."""

    def __init__(self, result: ToolResult) -> None:
        self._result = result

    def render(self) -> ToolResultRendering:
        match self._result:
            case ChartResult(spec=spec, title=title):
                return ChartRendering(spec=spec, title=title)
            case CustomElementResult(element=element, props=props, title=title):
                return CustomElementRendering(element=element, props=props, title=title)
            case DiagramResult(spec=spec, path=path, title=title):
                return DiagramRendering(spec=spec, path=path, title=title)
            case (
                TextResult()
                | JsonResult()
                | TableResult()
                | PgCopyTextResult()
                | AffectedSqlResult()
                | ErrorResult()
            ):
                return MarkdownRendering(ToolResultMarkdown(self._result).render())
            case _ as never:
                assert_never(never)


class ToolResultMarkdown:
    """Оборачивает один ToolResult и рендерит его в markdown для Chainlit."""

    def __init__(self, result: ToolResult) -> None:
        self._result = result

    def render(self) -> str:  # noqa: PLR0911
        match self._result:
            case TextResult(text=t):
                return t
            case JsonResult(payload=p):
                return self._json_block(p)
            case TableResult(rows=rows, note=note):
                return self._table_block(rows, note)
            case PgCopyTextResult() as pg_text:
                return self._copy_text_block(pg_text)
            case AffectedSqlResult(affected_rows=n, status=s):
                return self._affected_block(n, s)
            case ChartResult(title=title):
                if title:
                    return f"_(график: {title})_"
                return "_(график)_"
            case CustomElementResult(title=title):
                if title:
                    return f"_(элемент: {title})_"
                return "_(элемент)_"
            case DiagramResult(path=path, title=title):
                if title:
                    return f"_(диаграмма: {title})_"
                return f"_(диаграмма: {path})_"
            case ErrorResult(message=m):
                if "\n" in m:
                    return f"**Error:**\n\n{m}"
                return f"**Error:** {m}"
            case _ as never:
                assert_never(never)

    @staticmethod
    def _affected_block(affected_rows: int | None, status: str | None) -> str:
        if affected_rows is None:
            if status is None:
                return "_запрос выполнен_"
            return f"_{status}_"

        counted = f"затронуто строк: {affected_rows}"
        if status is None:
            return f"_{counted}_"
        return f"_{counted} ({status})_"

    @staticmethod
    def _json_block(payload: Any) -> str:
        pretty = json.dumps(payload, ensure_ascii=False, indent=2)
        if "\n" not in pretty:
            return f"`{pretty}`"
        return f"\n```json\n{pretty}\n```\n"

    def _table_block(
        self,
        rows: Sequence[Mapping[str, Any]],
        note: str | None,
    ) -> str:
        body = self._render_rows(rows)
        if note:
            return f"\n{body}\n\n_{note}_"
        return f"\n{body}"

    def _copy_text_block(self, result: PgCopyTextResult) -> str:
        rows = list(result.iter_rows())
        if not rows:
            return "_(no rows)_"

        header = [self._flatten_cell(cell) for cell in rows[0]]
        data = [[self._flatten_cell(cell) for cell in row] for row in rows[1:]]
        return "\n" + tabulate(
            data,
            headers=header,
            tablefmt="github",
            disable_numparse=True,
        )

    @staticmethod
    def _flatten_cell(cell: str | None) -> str:
        if cell is None:
            return ""
        return cell.replace("\r\n", " ⏎ ").replace("\n", " ⏎ ").replace("\r", " ⏎ ")

    @classmethod
    def _cell(cls, value: Any) -> str:
        if value is None or isinstance(value, str):
            return cls._flatten_cell(value)
        if isinstance(value, (list, tuple, dict)):
            return cls._flatten_cell(json.dumps(value, ensure_ascii=False))
        return cls._flatten_cell(str(value))

    def _render_rows(self, rows: Sequence[Mapping[str, Any]]) -> str:
        if not rows:
            return "_(no rows)_"

        flat = [{k: self._cell(v) for k, v in row.items()} for row in rows]
        return tabulate(
            flat,
            headers="keys",
            tablefmt="github",
            disable_numparse=True,
        )
