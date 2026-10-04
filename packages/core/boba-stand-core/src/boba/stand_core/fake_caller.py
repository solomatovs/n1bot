"""Фейковый модуль инструментов с параметром контекста вызова.

Запускается настоящим subprocess'ом, как fake_toolmod. Тело получает
субъекта вызова injected-параметром и возвращает, кого увидело: тесты
проверяют, что контекст, поставленный входом (ход чата, запрос сервиса),
доезжает до тела. fake_sleep называет свой pid файлом и спит — им тесты
проверяют отмену вызова и предел одновременных запусков.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Annotated, Final

from pydantic import Field

from boba.identity.context import Scope, Subject
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import MarkdownResult


@tool
async def fake_whoami(
    subject: Annotated[Subject, Injected],
) -> MarkdownResult:
    """Называет логин и роли того, от чьего имени идёт вызов."""
    roles = ",".join(sorted(subject.roles))

    return MarkdownResult(text=f"{subject.login}|{roles}|{subject.profile}")


@tool
async def fake_scope(
    scope: Annotated[Scope, Injected],
) -> MarkdownResult:
    """Называет область, в которой идёт вызов."""
    return MarkdownResult(text=scope.id)


@tool
async def fake_sleep(
    seconds: Annotated[float, Field(ge=0, description="Сколько секунд спать")],
    marker: Annotated[str, Field(min_length=1, description="Файл с pid тела")],
) -> MarkdownResult:
    """Записывает свой pid в файл и спит: тесты отмены и предела запусков."""
    Path(marker).write_text(str(os.getpid()), encoding="utf-8")
    await asyncio.sleep(seconds)

    return MarkdownResult(text=f"slept {seconds}")


TOOLS: Final = ToolMain.toolset(fake_whoami, fake_scope, fake_sleep)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
