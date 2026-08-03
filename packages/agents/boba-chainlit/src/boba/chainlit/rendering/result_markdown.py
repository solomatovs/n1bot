"""Markdown-рендер ToolResult-вариантов для Chainlit Step."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, assert_never

from tabulate import tabulate

from boba.toolkit.result import (
    ChartResult,
    ErrorResult,
    JsonResult,
    PgCopyTextResult,
    TableResult,
    TextResult,
    ToolResult,
)

__all__ = ["ToolResultMarkdown"]


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
            case ChartResult(title=title):
                return f"_(график: {title})_" if title else "_(график)_"
            case ErrorResult(message=m):
                if "\n" in m:
                    return f"**Error:**\n\n{m}"
                return f"**Error:** {m}"
            case _ as never:
                assert_never(never)

    @staticmethod
    def _json_block(payload: Any) -> str:
        pretty = json.dumps(payload, ensure_ascii=False, indent=2)
        if "\n" not in pretty:
            return f"`{pretty}`"
        return f"\n```json\n{pretty}\n```\n"

    def _table_block(
        self, rows: Sequence[Mapping[str, Any]], note: str | None,
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
            data, headers=header, tablefmt="github", disable_numparse=True,
        )

    @staticmethod
    def _flatten_cell(cell: str | None) -> str:
        if cell is None:
            return ""
        return (
            cell.replace("\r\n", " ⏎ ").replace("\n", " ⏎ ").replace("\r", " ⏎ ")
        )

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
            flat, headers="keys", tablefmt="github", disable_numparse=True,
        )
