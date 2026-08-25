"""Единая точка отмены: ход обрывает кнопка Stop, и только она."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest
from conftest import make_context

from boba.cancellation import (
    StopReason,
    ToolStopped,
    current_cancellation,
)
from boba.chainlit.chat.turn import ChatTurn
from boba.chainlit.domain.run import LiveStream, RunPort, RunRegistry
from boba.toolkit.channels import CallOutcome

THREAD = "thread-1"


class FakeTurn(RunPort):
    """Ход под тест: контексту достаточно порта с шагом ответа."""

    answer_step_id = "answer-step"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    "остановка хода не зависит от сессии chainlit"


@pytest.fixture(autouse=True)
def clean_contexts() -> None:
    "чистый реестр на тест: ходы не должны протекать между сценариями"
    RunRegistry.reset()


class TestRegistry:
    """Ход адресуется thread_id — иначе до него не дотянуться снаружи."""

    def test_turn_is_addressable_while_open(self) -> None:
        if RunRegistry.active(THREAD) is not None:
            raise AssertionError("RunRegistry.active(THREAD) is None")
        with RunRegistry.open(make_context(THREAD), FakeTurn()) as context:
            if RunRegistry.active(THREAD) is not context:
                raise AssertionError("RunRegistry.active(THREAD) is context")
        if RunRegistry.active(THREAD) is not None:
            raise AssertionError("RunRegistry.active(THREAD) is None")

    def test_stop_cancels_the_open_turn(self) -> None:
        with RunRegistry.open(make_context(THREAD), FakeTurn()) as context:
            if RunRegistry.stop(THREAD, StopReason.USER_STOP) is not True:
                raise AssertionError("RunRegistry.stop(THREAD, StopReason.USER_STOP) …")
            if context.cancellation.cancelled is not True:
                raise AssertionError("context.cancellation.cancelled is True")
            if context.cancellation.reason is not StopReason.USER_STOP:
                raise AssertionError("context.cancellation.reason is StopReason.USER_…")

    def test_stop_without_turn_is_reported(self) -> None:
        if RunRegistry.stop(THREAD, StopReason.USER_STOP) is not False:
            raise AssertionError("RunRegistry.stop(THREAD, StopReason.USER_STOP) is F…")

    def test_stop_reaches_the_context_of_the_turn(self) -> None:
        """Инструменты читают отмену из контекста: снаружи и изнутри один объект."""
        with RunRegistry.open(make_context(THREAD), FakeTurn()):
            RunRegistry.stop(THREAD, StopReason.USER_STOP)
            if current_cancellation().cancelled is not True:
                raise AssertionError("current_cancellation().cancelled is True")
            with pytest.raises(ToolStopped):
                current_cancellation().raise_if_cancelled()

    def test_stop_reaches_worker_threads(self) -> None:
        """Синхронные инструменты живут в тред-пуле: флаг обязан доезжать и туда."""
        with RunRegistry.open(make_context(THREAD), FakeTurn()):
            ctx = copy_context()
            RunRegistry.stop(THREAD, StopReason.USER_STOP)
            with ThreadPoolExecutor(1) as pool:
                seen = pool.submit(ctx.run, lambda: current_cancellation().cancelled)
                if seen.result() is not True:
                    raise AssertionError("seen.result() is True")

    def test_stop_is_thread_safe(self) -> None:
        """Кнопку жмут из обработчика сокета — это чужой поток."""
        with RunRegistry.open(make_context(THREAD), FakeTurn()) as context:
            stopper = threading.Thread(
                target=RunRegistry.stop, args=(THREAD, StopReason.USER_STOP)
            )
            stopper.start()
            stopper.join()
            if context.cancellation.cancelled is not True:
                raise AssertionError("context.cancellation.cancelled is True")

    def test_new_turn_supersedes_the_stale_one(self) -> None:
        """Второй ход того же треда обрывает забытый первый, а не копится рядом."""
        with (
            RunRegistry.open(make_context(THREAD), FakeTurn()) as first,
            RunRegistry.open(make_context(THREAD), FakeTurn()) as second,
        ):
            if first.cancellation.cancelled is not True:
                raise AssertionError("first.cancellation.cancelled is True")
            if first.cancellation.reason is not StopReason.SUPERSEDED:
                raise AssertionError("first.cancellation.reason is StopReason.SUPERSE…")
            if second.cancellation.cancelled is not False:
                raise AssertionError("second.cancellation.cancelled is False")

    def test_release_keeps_the_newer_turn(self) -> None:
        """Выход из старого хода не должен снимать с учёта новый."""
        with RunRegistry.open(make_context(THREAD), FakeTurn()):
            with RunRegistry.open(make_context(THREAD), FakeTurn()) as second:
                pass
            if RunRegistry.active(THREAD) is second:
                raise AssertionError("RunRegistry.active(THREAD) is not second")

    def test_tools_reach_the_turn_of_the_thread(self) -> None:
        """Инструменты находят ход по thread_id — им нужен шаг ответа."""
        turn = FakeTurn()
        with RunRegistry.open(make_context(THREAD), turn):
            if RunRegistry.port_of(THREAD) is not turn:
                raise AssertionError("RunRegistry.port_of(THREAD) is turn")
        if RunRegistry.port_of(THREAD) is not None:
            raise AssertionError("RunRegistry.port_of(THREAD) is None")


class TestLiveArtifacts:
    """Живые журналы и насос гаснут вместе с контекстом, файлы — нет."""

    class FakeStream(LiveStream):
        def __init__(self) -> None:
            self.note: str | None = None

        @property
        def closed(self) -> bool:
            return self.note is not None

        @property
        def call_prefix(self) -> str:
            return f"{THREAD}/call-1."

        def close(self, note: str) -> None:
            self.note = note

    def test_streams_close_when_the_context_does(self) -> None:
        stream = self.FakeStream()
        with RunRegistry.open(make_context(THREAD), FakeTurn()) as context:
            context.add_stream("call-1", stream)
            if context.stream("call-1") is not stream:
                raise AssertionError('context.stream("call-1") is stream')
            if RunRegistry.live_scopes() != frozenset({THREAD}):
                raise AssertionError("RunRegistry.live_scopes() == frozenset({THREAD…")

        if stream.note != CallOutcome.STOPPED.value:
            raise AssertionError("stream.note == CallOutcome.STOPPED.value")
        if RunRegistry.live_scopes() != frozenset():
            raise AssertionError("RunRegistry.live_scopes() == frozenset()")

    def test_thread_without_streams_is_not_live(self) -> None:
        with RunRegistry.open(make_context(THREAD), FakeTurn()):
            if RunRegistry.live_scopes() != frozenset():
                raise AssertionError("RunRegistry.live_scopes() == frozenset()")


class TestAsyncTurn:
    """Обрыв корутины хода — прерыватель, который владелец подключает сам."""

    def test_cancel_interrupts_awaiting_turn(self) -> None:
        started = asyncio.Event()

        async def scenario() -> str:
            async def turn() -> str:
                context = make_context(THREAD)
                with (
                    RunRegistry.open(context, FakeTurn()),
                    RunRegistry.task_abort(context.cancellation),
                ):
                    started.set()
                    try:
                        await asyncio.sleep(30)
                    except asyncio.CancelledError:
                        return "cancelled"
                return "finished"

            task = asyncio.create_task(turn())
            await started.wait()
            RunRegistry.stop(THREAD, StopReason.USER_STOP)
            return await task

        if asyncio.run(scenario()) != "cancelled":
            raise AssertionError('asyncio.run(scenario()) == "cancelled"')

    def test_turn_is_unregistered_after_cancel(self) -> None:
        async def scenario() -> bool:
            started = asyncio.Event()

            async def turn() -> None:
                context = make_context(THREAD)
                with (
                    RunRegistry.open(context, FakeTurn()),
                    RunRegistry.task_abort(context.cancellation),
                ):
                    started.set()
                    await asyncio.sleep(30)

            task = asyncio.create_task(turn())
            await started.wait()
            RunRegistry.stop(THREAD, StopReason.USER_STOP)
            with pytest.raises(asyncio.CancelledError):
                await task
            return RunRegistry.active(THREAD) is None

        if asyncio.run(scenario()) is not True:
            raise AssertionError("asyncio.run(scenario()) is True")


class TestStopButton:
    """Кнопка Stop — единственный способ оборвать ход."""

    def test_button_stops_the_open_turn(self) -> None:
        async def scenario() -> StopReason | None:
            with RunRegistry.open(make_context(THREAD), FakeTurn()) as context:
                if ChatTurn.stop(THREAD) is not True:
                    raise AssertionError("ChatTurn.stop(THREAD) is True")
                return context.cancellation.reason

        if asyncio.run(scenario()) is not StopReason.USER_STOP:
            raise AssertionError("asyncio.run(scenario()) is StopReason.USER_STOP")

    def test_button_without_turn_stops_nothing(self) -> None:
        if ChatTurn.stop(THREAD) is not False:
            raise AssertionError("ChatTurn.stop(THREAD) is False")

    def test_turn_survives_when_nobody_pressed_stop(self) -> None:
        """Разрыв связи сам по себе ход не трогает: он доигрывает до конца."""

        async def scenario() -> bool:
            with RunRegistry.open(make_context(THREAD), FakeTurn()) as context:
                await asyncio.sleep(0)
                return context.cancellation.cancelled

        if asyncio.run(scenario()) is not False:
            raise AssertionError("asyncio.run(scenario()) is False")
