"""Рендер ToolResult -> контент для LLM; помощник pack_result.

Порт boba.llm.tool_result_render.tool_result_to_message для langchain-стека.
Инструмент возвращает (content, artifact): content — строка, которую видит
LLM (ToolMessage.content), artifact — сам ToolResult для UI-рендера.
"""

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
    """ToolResult -> строка, которую видит LLM.

    Exhaustive match по дискриминатору kind. ChartResult отдаёт только
    подтверждение — сырой Plotly-spec громоздок и бесполезен для рассуждения.
    """
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
    """ToolResult -> (content для LLM, artifact для UI).

    Единая точка, которую зовёт каждый инструмент в конце: контент для LLM
    и структурный результат для рендера в chainlit-интерфейсе.
    """
    return render_for_llm(result), result
