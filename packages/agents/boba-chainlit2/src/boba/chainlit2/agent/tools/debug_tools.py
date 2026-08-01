"""Отладочные инструменты: по одному на каждый ToolResult-класс."""

from __future__ import annotations

from langchain.tools import tool

from boba.chainlit2.rendering.render import pack_result
from boba.chainlit2.rendering.tool_result import (
    ChartResult,
    ErrorResult,
    JsonResult,
    PgCopyTextResult,
    TableResult,
    TextResult,
    ToolResult,
)

__all__ = [
    "debug_chart",
    "debug_error",
    "debug_json",
    "debug_pg_copy",
    "debug_table",
    "debug_text",
]


@tool(response_format="content_and_artifact")
def debug_text() -> tuple[str, ToolResult]:
    """Отладочный tool: возвращает TextResult (обычный текст)."""
    return pack_result(
        TextResult(text="Простой текстовый результат от debug_text.\n\nВторая строка.")
    )


@tool(response_format="content_and_artifact")
def debug_json() -> tuple[str, ToolResult]:
    """Отладочный tool: возвращает JsonResult (JSON payload)."""
    return pack_result(
        JsonResult(
            payload={
                "name": "example",
                "values": [1, 2, 3],
                "nested": {"ok": True, "note": "вложенный объект"},
            }
        )
    )


@tool(response_format="content_and_artifact")
def debug_table() -> tuple[str, ToolResult]:
    """Отладочный tool: возвращает TableResult (markdown-таблица)."""
    return pack_result(
        TableResult(
            rows=[
                {"name": "alpha", "count": 3, "ok": True},
                {"name": "beta", "count": 7, "ok": False},
                {"name": "gamma", "count": 11, "ok": True},
            ],
            note="показано 3 строки из 10",
        )
    )


@tool(response_format="content_and_artifact")
def debug_pg_copy() -> tuple[str, ToolResult]:
    """Отладочный tool: возвращает PgCopyTextResult (COPY TEXT дамп)."""
    return pack_result(
        PgCopyTextResult(
            text="id\tname\tamount\n1\talpha\t10.5\n2\tbeta\t\\N\n3\tgamma\t3.25\n"
        )
    )


@tool(response_format="content_and_artifact")
def debug_error() -> tuple[str, ToolResult]:
    """Отладочный tool: возвращает ErrorResult (ошибка инструмента)."""
    return pack_result(
        ErrorResult(message="Инструмент не смог выполнить операцию", error_kind="test")
    )


@tool(response_format="content_and_artifact")
def debug_chart() -> tuple[str, ToolResult]:
    """Отладочный tool: возвращает ChartResult (интерактивный график)."""
    return pack_result(
        ChartResult(
            spec={
                "data": [
                    {"type": "bar", "x": ["янв", "фев", "мар"], "y": [4, 7, 2]},
                ],
                "layout": {"title": "Отладочный график"},
            },
            title="Отладочный график",
        )
    )
