"""Встроенный узел dev_null: явный слив каналов группы в никуда.

План группы отказывает каналу без читателей, а модели иногда нужен ровно
такой канал — брак, который некуда класть, выход узла, который сейчас не
интересен. dev_null — читатель для этого случая: модель называет каналы в
feeds, узел принимает любой поток, сырой и кадровый, и дочитывает его в
/dev/null силами ядра, считая байты. Процесса у узла нет: его «вызов»
живёт на хосте (DevNullCall) и отдаёт группе пайпы, как обычный вызов —
дескрипторами; правила группы (EOF после успеха писателя, «все или никто»)
действуют как для всех.

Ошибки:
PayloadFailureError — узел вызван вне группы связанных вызовов либо группа
    сорвалась; ошибка уже упакована в результат.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Annotated, ClassVar

from pydantic import Field

from boba.toolkit.chain import PipelineSlot
from boba.toolkit.facade import PayloadTool, tool
from boba.toolkit.frames import ToolFrame
from boba.toolkit.launcher import (
    CallGate,
    CallInputPort,
    PayloadFailureError,
    RunResult,
    ToolCall,
    ToolOutcome,
)
from boba.toolkit.ports import PortDecl, PortDirection, StreamSpec, ToolStreamSpecs
from boba.toolkit.protocol import ReplyError, ReplyOk
from boba.toolkit.pump import PipePlumbing
from boba.toolkit.result import ErrorResult, MarkdownResult

__all__ = ["DevNullTool"]


class _DrainGate(CallGate):
    """Реализация CallGate узла без тела: барьер узлу не нужен, он не
    фиксирует результата — все ответы безвредно глотаются."""

    def claim(self, on_ready: Callable[[], None]) -> None:
        return

    def release(self) -> None:
        return

    def refuse(self) -> None:
        return


class DevNullCall(ToolCall):
    """Реализация ToolCall без процесса: слив входных каналов в /dev/null.

    Создаёт пайп на каждый канал, write-концы отдаёт группе (в них пишет
    раздача), read-концы дочитывает своими потоками в /dev/null со счётом
    байт. EOF приходит от группы по её правилам; close при срыве группы
    обрывает слив. wait отдаёт счёт байт по каналам.
    """

    SPLICE_BYTES: ClassVar[int] = 1 << 20

    def __init__(self, channels: Sequence[str], pipe_bytes: Sequence[int]) -> None:
        self._channels = tuple(channels)
        self._reads: list[int] = []
        self._writes: list[int] = []
        for size, channel in zip(pipe_bytes, self._channels, strict=True):
            read_fd, write_fd = os.pipe()
            PipePlumbing.require(write_fd, size, channel)
            self._reads.append(read_fd)
            self._writes.append(write_fd)

        self._moved = [0] * len(self._channels)
        self._lock = threading.Lock()
        self._closed = False
        self._gate = _DrainGate()
        self._threads: list[threading.Thread] = []
        for index in range(len(self._channels)):
            worker = threading.Thread(
                target=self._drain,
                args=(index,),
                name=f"dev-null:{self._channels[index]}",
                daemon=True,
            )
            self._threads.append(worker)
            worker.start()

    def take_writes(self) -> tuple[int, ...]:
        """Write-концы каналов для группы; владение уходит с дескрипторами."""
        taken = tuple(self._writes)
        self._writes = []

        return taken

    def wait(self) -> dict[str, int]:
        """Дождаться EOF всех каналов; счёт слитых байт по именам каналов."""
        for worker in self._threads:
            worker.join()

        counted: dict[str, int] = {}
        for channel, moved in zip(self._channels, self._moved, strict=True):
            counted[channel] = counted.get(channel, 0) + moved

        return counted

    def _drain(self, index: int) -> None:
        sink = os.open(os.devnull, os.O_WRONLY)
        try:
            while True:
                moved = os.splice(self._reads[index], sink, self.SPLICE_BYTES)
                if moved == 0:
                    return

                self._moved[index] += moved
        except OSError:
            return
        finally:
            os.close(sink)
            with self._lock:
                read_fd = self._reads[index]
                self._reads[index] = -1

            if read_fd >= 0:
                os.close(read_fd)

    def inputs(self) -> Sequence[CallInputPort]:
        return ()

    def done_sending(self) -> None:
        return

    def gate(self) -> CallGate:
        return self._gate

    def frames(self) -> Iterator[ToolFrame]:
        return iter(())

    def result(self) -> ToolOutcome:
        counted = self.wait()
        listed = ", ".join(f"{name}: {size}" for name, size in counted.items())
        body = MarkdownResult(text=f"discarded bytes by channel: {listed}")

        return ToolOutcome(
            reply=ReplyOk(content=body.llm_view(), artifact=body),
            run=RunResult(
                exit_code=0, stdout="", stderr="", duration_ms=0, timed_out=False
            ),
            diagnostic="",
        )

    def close(self) -> None:
        """Срыв группы: оборвать слив; повтор безвреден."""
        with self._lock:
            if self._closed:
                return

            self._closed = True
            reads = list(self._reads)
            for index in range(len(self._reads)):
                self._reads[index] = -1

        for fd in reads:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    continue

        for fd in self._writes:
            try:
                os.close(fd)
            except OSError:
                continue

        self._writes = []


class DevNullTool:
    """Сборка встроенного инструмента dev_null и его потоковой декларации.

    Инструмент собирает загрузчик реестра рядом с плагинами; тело живёт на
    хосте и исполняется только в группе связанных вызовов: вне её каналам
    не с кем соединиться.
    """

    NAME: ClassVar[str] = "dev_null"
    PORT: ClassVar[str] = "feeds"

    UNPLANNED: ClassVar[str] = "stream_call_unplanned"

    @classmethod
    def build(cls) -> PayloadTool:
        built = cls._tool()
        ToolStreamSpecs.register(built.name, cls.spec())

        return built

    @classmethod
    def spec(cls) -> StreamSpec:
        """Декларация: один всеядный входной порт-список."""
        port = PortDecl(
            name=cls.PORT,
            direction=PortDirection.INBOUND,
            kinds=(),
            raw=False,
            many=True,
            omnivore=True,
        )
        return StreamSpec(ports=(port,))

    @classmethod
    def _tool(cls) -> PayloadTool:
        @tool
        async def dev_null(
            feeds: Annotated[
                list[str],
                Field(
                    min_length=1,
                    description=(
                        "Имена каналов, которые некому читать, списком:\n"
                        "   - каждый байт канала отбрасывается без разбора\n"
                        "   - принимает любой поток, сырой и кадровый\n"
                        "   - в ответ — сколько байт слито по каждому каналу\n"
                    ),
                ),
            ],
        ) -> MarkdownResult:
            """Слив каналов группы в никуда: явный приёмник ненужных потоков."""
            return await asyncio.to_thread(cls._run, feeds)

        return dev_null

    @classmethod
    def _run(cls, feeds: Sequence[str]) -> MarkdownResult:
        started = time.monotonic()

        slot = PipelineSlot.get()
        if slot is None:
            failure = ErrorResult(
                message=(
                    f"tool {cls.NAME!r} drains stream channels and runs only in "
                    "a group of calls of one model response"
                ),
                error_kind=cls.UNPLANNED,
            )
            raise PayloadFailureError(failure)

        sizes = slot.input_sizes()
        call = DevNullCall(feeds, sizes)
        try:
            slot.attach(call, (), call.take_writes())
            counted = call.wait()
        except BaseException as exc:
            call.close()
            verdict = slot.settle_error(exc)
            if verdict.stopped:
                raise

            raise PayloadFailureError(verdict.failure_of(slot.key)) from exc

        outcome = call.result()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        run = outcome.run
        timed = ToolOutcome(
            reply=outcome.reply,
            run=RunResult(
                exit_code=run.exit_code,
                stdout=run.stdout,
                stderr=run.stderr,
                duration_ms=elapsed_ms,
                timed_out=run.timed_out,
            ),
            diagnostic=outcome.diagnostic,
        )

        settled = slot.settle(timed)
        reply = settled.reply
        if isinstance(reply, ReplyError):
            raise PayloadFailureError(reply.failure)

        listed = ", ".join(f"{name}: {size}" for name, size in counted.items())
        return MarkdownResult(text=f"discarded bytes by channel: {listed}")
