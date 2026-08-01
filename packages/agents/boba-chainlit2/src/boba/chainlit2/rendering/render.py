"""ToolResult -> контент для LLM; pack_result отдаёт (content, artifact)."""

from __future__ import annotations

import json
from typing import assert_never

from boba.chainlit2.rendering.tool_result import (
    ChartResult,
    ErrorResult,
    JsonResult,
    PgCopyTextResult,
    TableResult,
    TextResult,
    ToolResult,
)

__all__ = ["pack_result", "render_for_llm"]


def render_for_llm(result: ToolResult) -> str:
    content: str
    match result:
        case TextResult(text=t):
            content = t
        case JsonResult(payload=p):
            content = json.dumps(p, ensure_ascii=False)
        case TableResult(rows=r, note=n):
            body = json.dumps(r, ensure_ascii=False)
            content = body if n is None else f"{body}\n\n{n}"
        case PgCopyTextResult(text=t):
            content = t
        case ChartResult(title=title):
            content = f"[chart rendered: {title}]" if title else "[chart rendered]"
        case ErrorResult(message=m):
            content = m
        case _ as never:
            assert_never(never)
    return content


def pack_result(result: ToolResult) -> tuple[str, ToolResult]:
    return render_for_llm(result), result
