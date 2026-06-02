"""Bridge: tools-domain `ToolResult` → llm-domain `ToolResultMessage`.

Сознательно живёт **рядом** с `boba.llm.models`, но не **внутри** — чтобы
`models.py` не зависел от конкретных вариантов `ToolResult`. Все знания
о вариантах сосредоточены здесь и подкреплены `assert_never` — добавишь
новый вариант `ToolResult`, pyright потребует дописать ветку в этом
модуле, а не в models.py.
"""

from __future__ import annotations

import json
from typing import assert_never

from boba.llm.models import ToolResultMessage
from boba.tools.domain import (
    ChartResult,
    ErrorResult,
    JsonResult,
    PgCopyTextResult,
    TableResult,
    TextResult,
    ToolResult,
)

__all__ = ["tool_result_to_message"]


def tool_result_to_message(
    *,
    tool_call_id: str,
    result: ToolResult,
) -> ToolResultMessage:
    """`ToolResult` → `ToolResultMessage` со свежим id.

    Exhaustive match по дискриминатору `kind` — pyright проверит покрытие
    через `assert_never`. Для replay/тестов id можно перебить через
    `.set_id(...)` на результате.
    """
    content: str
    is_error: bool
    match result:
        case TextResult(text=t):
            content = t
            is_error = False
        case JsonResult(payload=p):
            content = json.dumps(p, ensure_ascii=False)
            is_error = False
        case TableResult(rows=r, note=n):
            body = json.dumps(r, ensure_ascii=False)
            content = body if n is None else f"{body}\n\n{n}"
            is_error = False
        case PgCopyTextResult(text=t):
            content = t
            is_error = False
        case ChartResult(title=title):
            # LLM не должна перечитывать собственный громоздкий Plotly-spec —
            # отдаём только подтверждение, что график отрисован.
            content = f"[chart rendered: {title}]" if title else "[chart rendered]"
            is_error = False
        case ErrorResult(message=m):
            content = m
            is_error = True
        case _ as never:
            assert_never(never)
    return ToolResultMessage(
        tool_call_id=tool_call_id,
        content=content,
        is_error=is_error,
    )
