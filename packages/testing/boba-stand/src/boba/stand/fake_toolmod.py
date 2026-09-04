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
from typing import Annotated, ClassVar, Final, Literal

from pydantic import BaseModel, Field, SecretStr

from boba.toolkit.entry import EntryFlag, ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.ports import Inbound, Outbound, RawInbound, RawOutbound
from boba.toolkit.result import TextResult, ToolResult, render_for_llm
from boba.toolkit.types import SecretRevealing


class FakeConfig(SecretRevealing):
    """Конфиг с секретом: проверяет доставку каналом injected и раскрытие SecretStr."""

    SECTION: ClassVar[str] = "tool.fake"

    token: SecretStr
    limit: int = Field(gt=0)


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
        msg = f"fake_echo({text!r}): fake backend is down"
        raise FakeUnavailableError(msg)

    if text == "crash":
        msg = f"fake_echo({text!r}): unexpected defect scripted by the stand"
        raise RuntimeError(msg)

    logging.getLogger("fake.tool").info("echo progress: %s", text)

    body = " ".join([text] * min(repeat, cfg.limit))
    artifact = TextResult(text=f"{body}|{cfg.token.get_secret_value()}")
    return render_for_llm(artifact), artifact


class FakeChunkHead(BaseModel):
    """Заголовок кадра потока: порядковый номер порции."""

    kind: Literal["chunk"] = "chunk"
    seq: int


class FakeDoneHead(BaseModel):
    """Заголовок последнего кадра: сколько порций прошло через тело."""

    kind: Literal["done"] = "done"
    total: int


@tool
async def fake_stream(
    prefix: Annotated[str, Field(description="Приставка к каждой порции")],
    cfg: Annotated[FakeConfig, Injected],
    feed: Annotated[Inbound[FakeChunkHead | FakeDoneHead], Injected],
    out: Annotated[Outbound[FakeChunkHead | FakeDoneHead], Injected],
) -> tuple[str, ToolResult]:
    """Отвечает кадром на каждый кадр входа: образец потокового инструмента."""
    total = 0
    for item in feed:
        total += 1
        body = prefix.encode("utf-8") + bytes(item.body)
        out.emit(FakeChunkHead(seq=total), body)

    out.emit(FakeDoneHead(total=total))

    artifact = TextResult(text=f"streamed {total}|{cfg.token.get_secret_value()}")
    return render_for_llm(artifact), artifact


class FakePidHead(BaseModel):
    """Заголовок кадра заложника: pid тела для убийства извне."""

    kind: Literal["pid"] = "pid"
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
    feed: Annotated[Inbound[FakePidHead], Injected],
    out: Annotated[Outbound[FakePidHead], Injected],
) -> tuple[str, ToolResult]:
    """Заложник: называет свой pid кадром и ждёт входа, которого не будет."""
    out.emit(FakePidHead(pid=os.getpid()))

    total = 0
    for _item in feed:
        total += 1

    artifact = TextResult(text=f"hostage got {total}|{cfg.token.get_secret_value()}")
    return render_for_llm(artifact), artifact


@tool
async def fake_garbage(
    cfg: Annotated[FakeConfig, Injected],
) -> tuple[str, ToolResult]:
    """Пишет мусор в канал кадров мимо кодека: читатель обязан увидеть обрыв.

    Номер канала берётся из полного sys.argv: флаги каналов ToolMain из
    argv тела вынимает, а вредителю нужен именно сырой дескриптор.
    """
    flag = EntryFlag.FD_FRAMES.value
    if flag in sys.argv:
        fd = int(sys.argv[sys.argv.index(flag) + 1])
        os.write(fd, b"\xff\xff\xff\xff not a frame at all")

    artifact = TextResult(text=f"garbage sent|{cfg.token.get_secret_value()}")
    return render_for_llm(artifact), artifact


@tool
async def fake_relay(
    cfg: Annotated[FakeConfig, Injected],
    feed: Annotated[RawInbound, Injected],
    out: Annotated[RawOutbound, Injected],
) -> tuple[str, ToolResult]:
    """Passthrough: переливает сырой поток со входа на выход без разбора."""
    total = 0
    for chunk in feed:
        total += len(chunk)
        out.write(chunk)

    artifact = TextResult(text=f"relayed {total}|{cfg.token.get_secret_value()}")
    return render_for_llm(artifact), artifact


EXPECTED: Mapping[type[Exception], FakeErrorKind] = {
    FakeUnavailableError: FakeErrorKind.UNAVAILABLE,
}

TOOLS: Final = ToolMain.toolset(
    fake_echo, fake_stream, fake_deaf, fake_hostage, fake_garbage, fake_relay
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
