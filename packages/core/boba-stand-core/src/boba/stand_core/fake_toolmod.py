"""Фейковый модуль инструментов для контрактных тестов ToolMain.

Запускается настоящим subprocess'ом: `python -m fake_toolmod <имя> --флаги`
с PYTHONPATH на каталог тестов. Кроме образцовых тел здесь живут вредные:
глухое (не читает вход), заложник (виснет, назвав свой pid) и генератор
битого потока кадров — ими тесты надёжности валят вызов. fake_merge —
потребитель нескольких потоков: все входы приходят ему разом. fake_emit,
fake_collect и fake_head — источник, приёмник с барьером группы и читатель
первого кадра: из них тесты собирают группы связанных вызовов.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, ClassVar, Final, Literal

from pydantic import BaseModel, Field, SecretStr

from boba.toolkit.entry import EntryFlag, ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.ports import (
    Inbound,
    Outbound,
    RawInbound,
    RawOutbound,
    StreamGroup,
)
from boba.toolkit.result import MarkdownResult
from boba.toolkit.types import SecretRevealing


class FakeConfig(SecretRevealing):
    """Конфиг с секретом: проверяет доставку каналом injected и раскрытие SecretStr."""

    SECTION: ClassVar[str] = "tool.fake"

    token: SecretStr
    limit: int = Field(gt=0)


class FakeUnavailableError(Exception):
    """Ожидаемый отказ фейкового инструмента."""


@tool
async def fake_echo(
    text: Annotated[str, Field(min_length=1, description="Что вернуть")],
    repeat: Annotated[int, Field(ge=1, description="Сколько раз")],
    cfg: Annotated[FakeConfig, Injected],
) -> MarkdownResult:
    """Повторяет текст, приправив секретом из конфига."""
    if text == "boom":
        msg = f"fake_echo({text!r}): fake backend is down"
        raise FakeUnavailableError(msg)

    if text == "crash":
        msg = f"fake_echo({text!r}): unexpected defect scripted by the stand"
        raise RuntimeError(msg)

    logging.getLogger("fake.tool").info("echo progress: %s", text)

    body = " ".join([text] * min(repeat, cfg.limit))
    return MarkdownResult(text=f"{body}|{cfg.token.get_secret_value()}")


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
) -> MarkdownResult:
    """Отвечает кадром на каждый кадр входа: образец потокового инструмента."""
    total = 0
    for item in feed:
        total += 1
        body = prefix.encode("utf-8") + bytes(item.body)
        out.emit(FakeChunkHead(seq=total), body)

    out.emit(FakeDoneHead(total=total))

    return MarkdownResult(text=f"streamed {total}|{cfg.token.get_secret_value()}")


class FakePidHead(BaseModel):
    """Заголовок кадра заложника: pid тела для убийства извне."""

    kind: Literal["pid"] = "pid"
    pid: int


@tool
async def fake_deaf(
    sleep_sec: Annotated[float, Field(ge=0, description="Сколько спать")],
    cfg: Annotated[FakeConfig, Injected],
    feed: Annotated[Inbound[FakeChunkHead | FakeDoneHead | FakePidHead], Injected],
) -> MarkdownResult:
    """Глухое тело: спит, не читая вход, — хост упирается в полный пайп."""
    time.sleep(sleep_sec)

    return MarkdownResult(text=f"deaf woke up|{cfg.token.get_secret_value()}")


@tool
async def fake_hostage(
    cfg: Annotated[FakeConfig, Injected],
    feed: Annotated[Inbound[FakePidHead], Injected],
    out: Annotated[Outbound[FakePidHead], Injected],
) -> MarkdownResult:
    """Заложник: называет свой pid кадром и ждёт входа, которого не будет."""
    out.emit(FakePidHead(pid=os.getpid()))

    total = 0
    for _item in feed:
        total += 1

    return MarkdownResult(text=f"hostage got {total}|{cfg.token.get_secret_value()}")


@tool
async def fake_garbage(
    cfg: Annotated[FakeConfig, Injected],
) -> MarkdownResult:
    """Пишет мусор в канал кадров мимо кодека: читатель обязан увидеть обрыв.

    Номер канала берётся из полного sys.argv: флаги каналов ToolMain из
    argv тела вынимает, а вредителю нужен именно сырой дескриптор.
    """
    flag = EntryFlag.FD_FRAMES.value
    if flag in sys.argv:
        fd = int(sys.argv[sys.argv.index(flag) + 1])
        os.write(fd, b"\xff\xff\xff\xff not a frame at all")

    return MarkdownResult(text=f"garbage sent|{cfg.token.get_secret_value()}")


RELAY_CHUNK_BYTES = 65536


@tool
async def fake_relay(
    cfg: Annotated[FakeConfig, Injected],
    feed: Annotated[RawInbound, Injected],
    out: Annotated[RawOutbound, Injected],
) -> MarkdownResult:
    """Passthrough: переливает сырой поток со входа на выход без разбора."""
    total = 0
    async for chunk in feed.blocks(RELAY_CHUNK_BYTES):
        total += len(chunk)
        await out.send(chunk)

    return MarkdownResult(text=f"relayed {total}|{cfg.token.get_secret_value()}")


class FakeFeedTally:
    """Сводка одного входа fake_merge: склеенные тела его кадров."""

    SEPARATOR: ClassVar[str] = ";"

    def of(self, feed: Inbound[FakeChunkHead | FakeDoneHead]) -> str:
        bodies = bytearray()
        for item in feed:
            bodies.extend(item.body)

        return bodies.decode("utf-8")

    async def all_of(
        self, feeds: Sequence[Inbound[FakeChunkHead | FakeDoneHead]]
    ) -> str:
        """Все входы читаются одновременно, каждый своим потоком."""
        reads: list[asyncio.Future[str]] = []
        for feed in feeds:
            reads.append(asyncio.ensure_future(asyncio.to_thread(self.of, feed)))

        tallies = await asyncio.gather(*reads)
        return self.SEPARATOR.join(tallies)


@tool
async def fake_merge(
    cfg: Annotated[FakeConfig, Injected],
    feeds: Annotated[Sequence[Inbound[FakeChunkHead | FakeDoneHead]], Injected],
) -> MarkdownResult:
    """Потребитель нескольких потоков: читает все входы разом, каждый своим
    потоком, и возвращает тела каждого входа через `;` в порядке входов."""
    merged = await FakeFeedTally().all_of(feeds)

    return MarkdownResult(
        text=f"merged {len(feeds)}:{merged}|{cfg.token.get_secret_value()}"
    )


class FakeMidwayError(Exception):
    """Сбой источника посреди потока, заказанный тестом."""


@tool
async def fake_emit(  # noqa: PLR0913
    prefix: Annotated[str, Field(description="Приставка тела кадра")],
    count: Annotated[int, Field(ge=0, description="Сколько кадров выдать")],
    size: Annotated[int, Field(ge=0, description="Сколько байт добавить к телу")],
    fail_midway: Annotated[bool, Field(description="Упасть после половины кадров")],
    cfg: Annotated[FakeConfig, Injected],
    out: Annotated[Outbound[FakeChunkHead | FakeDoneHead], Injected],
) -> MarkdownResult:
    """Источник: count кадров с телом `<prefix><номер>` и хвостом из size байт."""
    tail = b"x" * size
    for seq in range(count):
        if fail_midway and seq == count // 2:
            msg = f"fake_emit({prefix!r}): scripted failure after {seq} frames"
            raise FakeMidwayError(msg)

        await asyncio.to_thread(
            out.emit, FakeChunkHead(seq=seq), f"{prefix}{seq}".encode() + tail
        )

    return MarkdownResult(text=f"emitted {count}|{cfg.token.get_secret_value()}")


class FakeCollectedSink:
    """Сводка принятого потока fake_collect: число кадров, байт и sha256."""

    def __init__(self) -> None:
        self.frames = 0
        self.bytes = 0
        self._digest = hashlib.sha256()

    def take(self, feed: Inbound[FakeChunkHead | FakeDoneHead]) -> None:
        for item in feed:
            self.frames += 1
            self.bytes += len(item.body)
            self._digest.update(item.body)

    def text(self) -> str:
        digest = self._digest.hexdigest()[:16]
        return f"collected {self.frames} frames {self.bytes} bytes {digest}"


@tool
async def fake_collect(  # noqa: PLR0913
    marker: Annotated[str, Field(min_length=1, description="Файл фиксации")],
    fail: Annotated[bool, Field(description="Упасть, дочитав поток")],
    gated: Annotated[bool, Field(description="Ждать барьер группы перед фиксацией")],
    cfg: Annotated[FakeConfig, Injected],
    feed: Annotated[Inbound[FakeChunkHead | FakeDoneHead], Injected],
    group: Annotated[StreamGroup, Injected],
) -> MarkdownResult:
    """Приёмник: дочитывает поток и «фиксирует» результат — пишет сводку в
    файл marker; gated — перед фиксацией ждёт барьер группы."""
    sink = FakeCollectedSink()
    await asyncio.to_thread(sink.take, feed)

    if fail:
        msg = f"fake_collect({marker!r}): scripted failure after {sink.text()}"
        raise FakeMidwayError(msg)

    if gated:
        await group.ready()

    Path(marker).write_text(sink.text(), encoding="utf-8")

    return MarkdownResult(text=f"{sink.text()}|{cfg.token.get_secret_value()}")


@tool
async def fake_head(
    cfg: Annotated[FakeConfig, Injected],
    feed: Annotated[Inbound[FakeChunkHead | FakeDoneHead], Injected],
) -> MarkdownResult:
    """Читатель первого кадра: берёт его и выходит, не дочитав поток."""
    first = await asyncio.to_thread(next, iter(feed), None)

    body = b""
    if first is not None:
        body = bytes(first.body)

    return MarkdownResult(text=f"head {body!r}|{cfg.token.get_secret_value()}")


TOOLS: Final = ToolMain.toolset(
    fake_echo,
    fake_stream,
    fake_deaf,
    fake_hostage,
    fake_garbage,
    fake_relay,
    fake_merge,
    fake_emit,
    fake_collect,
    fake_head,
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
