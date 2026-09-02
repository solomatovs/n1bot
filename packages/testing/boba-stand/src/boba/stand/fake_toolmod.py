"""Фейковый модуль инструментов для контрактных тестов ToolMain.

Запускается настоящим subprocess'ом: `python -m fake_toolmod <имя> --флаги`
с PYTHONPATH на каталог тестов. Кроме образцовых тел здесь живут вредные:
глухое (не читает вход), заложник (виснет, назвав свой pid) и генератор
битого потока кадров — ими тесты надёжности валят вызов.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, ClassVar, Final

from pydantic import BaseModel, Field, SecretStr

from boba.toolkit.channels import ToolChannel
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.frames import ToolIo
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


class FakeFrameKind(StrEnum):
    """Прикладные kind'ы кадров потокового инструмента стенда."""

    CHUNK = "chunk"
    DONE = "done"


class FakeChunkHead(BaseModel):
    """Заголовок кадра потока: порядковый номер порции."""

    kind: str = FakeFrameKind.CHUNK.value
    seq: int


class FakeDoneHead(BaseModel):
    """Заголовок последнего кадра: сколько порций прошло через тело."""

    kind: str = FakeFrameKind.DONE.value
    total: int


@tool
async def fake_stream(
    prefix: Annotated[str, Field(description="Приставка к каждой порции")],
    cfg: Annotated[FakeConfig, Injected],
    io: Annotated[ToolIo, Injected],
) -> tuple[str, ToolResult]:
    """Отвечает кадром на каждый кадр входа: образец потокового инструмента."""
    total = 0
    for frame in io.inbound():
        total += 1
        body = prefix.encode("utf-8") + frame.body
        io.emit(FakeChunkHead(seq=total), body)

    io.emit(FakeDoneHead(total=total))

    artifact = TextResult(text=f"streamed {total}|{cfg.token.get_secret_value()}")
    return render_for_llm(artifact), artifact


class FakePidHead(BaseModel):
    """Заголовок кадра заложника: pid тела для убийства извне."""

    kind: str = "pid"
    pid: int


@tool
async def fake_deaf(
    sleep_sec: Annotated[float, Field(ge=0, description="Сколько спать")],
    cfg: Annotated[FakeConfig, Injected],
) -> tuple[str, ToolResult]:
    """Глухое тело: спит, не читая вход, — хост упирается в полный пайп."""
    time.sleep(sleep_sec)

    artifact = TextResult(text=f"deaf woke up|{cfg.token.get_secret_value()}")
    return render_for_llm(artifact), artifact


@tool
async def fake_hostage(
    cfg: Annotated[FakeConfig, Injected],
    io: Annotated[ToolIo, Injected],
) -> tuple[str, ToolResult]:
    """Заложник: называет свой pid кадром и ждёт входа, которого не будет."""
    io.emit(FakePidHead(pid=os.getpid()))

    total = 0
    for _frame in io.inbound():
        total += 1

    artifact = TextResult(text=f"hostage got {total}|{cfg.token.get_secret_value()}")
    return render_for_llm(artifact), artifact


@tool
async def fake_garbage(
    cfg: Annotated[FakeConfig, Injected],
) -> tuple[str, ToolResult]:
    """Пишет мусор в канал кадров мимо кодека: читатель обязан увидеть обрыв."""
    raw = os.environ.get(ToolChannel.FRAMES.env_name)
    if raw is not None:
        os.write(int(raw), b"\xff\xff\xff\xff not a frame at all")

    artifact = TextResult(text=f"garbage sent|{cfg.token.get_secret_value()}")
    return render_for_llm(artifact), artifact


EXPECTED: Mapping[type[Exception], FakeErrorKind] = {
    FakeUnavailableError: FakeErrorKind.UNAVAILABLE,
}

TOOLS: Final = ToolMain.toolset(
    fake_echo, fake_stream, fake_deaf, fake_hostage, fake_garbage
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
