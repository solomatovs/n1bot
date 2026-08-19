"""Фейковый модуль инструментов для контрактных тестов ToolMain.

Запускается настоящим subprocess'ом: `python -m fake_toolmod <имя> --флаги`
с PYTHONPATH на каталог тестов.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, ClassVar, Final

from pydantic import BaseModel, Field, SecretStr

from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import TextResult, ToolResult, render_for_llm


class FakeConfig(BaseModel):
    """Конфиг с секретом: проверяет stdin-доставку и revealed()."""

    SECTION: ClassVar[str] = "tool.fake"

    token: SecretStr
    limit: int = Field(gt=0)

    def revealed(self) -> dict[str, object]:
        return {"token": self.token.get_secret_value(), "limit": self.limit}


class FakeUnavailableError(Exception):
    """Ожидаемый отказ фейкового инструмента."""


class FakeErrorKind(StrEnum):
    UNAVAILABLE = "fake_unavailable"


@tool
async def fake_echo(
    text: Annotated[str, Field(min_length=1, description="Что вернуть")],
    repeat: Annotated[int, Field(ge=1, description="Сколько раз")],
    cfg: Annotated[FakeConfig, Injected],
) -> tuple[str, ToolResult]:
    """Повторяет текст, приправив секретом из конфига."""
    if text == "boom":
        msg = "fake backend is down"
        raise FakeUnavailableError(msg)

    if text == "crash":
        msg = "unexpected defect"
        raise RuntimeError(msg)

    logging.getLogger("fake.tool").info("echo progress: %s", text)

    body = " ".join([text] * min(repeat, cfg.limit))
    artifact = TextResult(text=f"{body}|{cfg.token.get_secret_value()}")
    return render_for_llm(artifact), artifact


EXPECTED: Mapping[type[Exception], FakeErrorKind] = {
    FakeUnavailableError: FakeErrorKind.UNAVAILABLE,
}

TOOLS: Final = ToolMain.toolset(fake_echo)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
