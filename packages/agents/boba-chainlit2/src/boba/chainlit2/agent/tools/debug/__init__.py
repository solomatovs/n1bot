"""Отладочные инструменты: по одному на каждый вариант ToolResult."""

from boba.chainlit2.agent.tools.debug.tools import (
    debug_chart,
    debug_error,
    debug_json,
    debug_pg_copy,
    debug_table,
    debug_text,
)

__all__ = [
    "debug_chart",
    "debug_error",
    "debug_json",
    "debug_pg_copy",
    "debug_table",
    "debug_text",
]
