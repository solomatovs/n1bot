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

from boba.llm.models import (
    TextBlock,
    ToolResultContentBlock,
    ToolResultMessage,
    new_message_id,
)
from boba.tools.domain import ErrorResult, JsonResult, TextResult, ToolResult

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
    blocks: tuple[ToolResultContentBlock, ...]
    is_error: bool
    match result:
        case TextResult(text=t):
            blocks = (TextBlock(content=t),)
            is_error = False
        case JsonResult(payload=p):
            blocks = (TextBlock(content=json.dumps(p, ensure_ascii=False)),)
            is_error = False
        case ErrorResult(message=m):
            blocks = (TextBlock(content=m),)
            is_error = True
        case _ as never:
            assert_never(never)
    return ToolResultMessage(
        id=new_message_id(),
        tool_call_id=tool_call_id,
        blocks=blocks,
        is_error=is_error,
    )
