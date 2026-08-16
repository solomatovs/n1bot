"""Единая точка отмены: ход обрывает кнопка Stop, и только она."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from boba.cancellation import (
    StopReason,
    ToolStopped,
    current_cancellation,
)
from boba.chainlit.chat.turn import ChatTurn
from boba.chainlit.domain.turn import TurnContext

THREAD = "thread-1"


class FakeTurn:
    """Ход под тест: контексту достаточно порта с шагом ответа."""

    answer_step_id = "answer-step"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    "остановка хода не зависит от сессии chainlit"


@pytest.fixture(autouse=True)
def clean_contexts() -> None:
    "чистый реестр на тест: ходы не должны протекать между сценариями"
    TurnContext.reset()


class TestRegistry:
    """Ход адресуется thread_id — иначе до него не дотянуться снаружи."""

    def test_turn_is_addressable_while_open(self) -> None:
        assert TurnContext.active(THREAD) is None
        with TurnContext.open(THREAD, FakeTurn()) as context:
            assert TurnContext.active(THREAD) is context
        assert TurnContext.active(THREAD) is None

    def test_stop_cancels_the_open_turn(self) -> None:
        with TurnContext.open(THREAD, FakeTurn()) as context:
            assert TurnContext.stop(THREAD, StopReason.USER_STOP) is True
            assert context.cancellation.cancelled is True
            assert context.cancellation.reason is StopReason.USER_STOP

    def test_stop_without_turn_is_reported(self) -> None:
        assert TurnContext.stop(THREAD, StopReason.USER_STOP) is False

    def test_stop_reaches_the_context_of_the_turn(self) -> None:
        """Инструменты читают отмену из контекста: снаружи и изнутри один объект."""
        with TurnContext.open(THREAD, FakeTurn()):
            TurnContext.stop(THREAD, StopReason.USER_STOP)
            assert current_cancellation().cancelled is True
            with pytest.raises(ToolStopped):
                current_cancellation().raise_if_cancelled()

    def test_stop_reaches_worker_threads(self) -> None:
        """Синхронные инструменты живут в тред-пуле: флаг обязан доезжать и туда."""
        with TurnContext.open(THREAD, FakeTurn()):
            ctx = copy_context()
            TurnContext.stop(THREAD, StopReason.USER_STOP)
            with ThreadPoolExecutor(1) as pool:
                seen = pool.submit(ctx.run, lambda: current_cancellation().cancelled)
                assert seen.result() is True

    def test_stop_is_thread_safe(self) -> None:
        """Кнопку жмут из обработчика сокета — это чужой поток."""
        with TurnContext.open(THREAD, FakeTurn()) as context:
            stopper = threading.Thread(
                target=TurnContext.stop, args=(THREAD, StopReason.USER_STOP)
            )
            stopper.start()
            stopper.join()
            assert context.cancellation.cancelled is True

    def test_new_turn_supersedes_the_stale_one(self) -> None:
        """Второй ход того же треда обрывает забытый первый, а не копится рядом."""
        with (
            TurnContext.open(THREAD, FakeTurn()) as first,
            TurnContext.open(THREAD, FakeTurn()) as second,
        ):
            assert first.cancellation.cancelled is True
            assert first.cancellation.reason is StopReason.SUPERSEDED
            assert second.cancellation.cancelled is False

    def test_release_keeps_the_newer_turn(self) -> None:
        """Выход из старого хода не должен снимать с учёта новый."""
        with TurnContext.open(THREAD, FakeTurn()):
            with TurnContext.open(THREAD, FakeTurn()) as second:
                pass
            assert TurnContext.active(THREAD) is not second

    def test_tools_reach_the_turn_of_the_thread(self) -> None:
        """Инструменты находят ход по thread_id — им нужен шаг ответа."""
        turn = FakeTurn()
        with TurnContext.open(THREAD, turn):
            assert TurnContext.turn_of(THREAD) is turn
        assert TurnContext.turn_of(THREAD) is None


class TestLiveArtifacts:
    """Живые журналы и насос гаснут вместе с контекстом, файлы — нет."""

    class FakeStream:
        def __init__(self) -> None:
            self.note: str | None = None

        @property
        def closed(self) -> bool:
            return self.note is not None

        def close(self, note: str) -> None:
            self.note = note

    def test_streams_close_when_the_context_does(self) -> None:
        stream = self.FakeStream()
        with TurnContext.open(THREAD, FakeTurn()) as context:
            context.add_stream("call-1", stream)
            assert context.stream("call-1") is stream
            assert TurnContext.live_threads() == frozenset({THREAD})

        assert stream.note == TurnContext.STREAM_STOP_NOTE
        assert TurnContext.live_threads() == frozenset()

    def test_thread_without_streams_is_not_live(self) -> None:
        with TurnContext.open(THREAD, FakeTurn()):
            assert TurnContext.live_threads() == frozenset()

    def test_pump_is_cancelled_on_close(self) -> None:
        async def scenario() -> bool:
            with TurnContext.open(THREAD, FakeTurn()) as context:
                pump = asyncio.create_task(asyncio.sleep(30))
                context.attach_pump(pump)

            await asyncio.sleep(0)
            return pump.cancelled()

        assert asyncio.run(scenario()) is True

    def test_new_pump_replaces_the_old_one(self) -> None:
        async def scenario() -> bool:
            with TurnContext.open(THREAD, FakeTurn()) as context:
                first = asyncio.create_task(asyncio.sleep(30))
                context.attach_pump(first)
                second = asyncio.create_task(asyncio.sleep(30))
                context.attach_pump(second)

                await asyncio.sleep(0)
                return first.cancelled() and not second.cancelled()

        assert asyncio.run(scenario()) is True


class TestAsyncTurn:
    """Асинхронный мир обрывается отменой задачи хода — это тоже прерыватель."""

    def test_cancel_interrupts_awaiting_turn(self) -> None:
        started = asyncio.Event()

        async def scenario() -> str:
            async def turn() -> str:
                with TurnContext.open(THREAD, FakeTurn()):
                    started.set()
                    try:
                        await asyncio.sleep(30)
                    except asyncio.CancelledError:
                        return "cancelled"
                return "finished"

            task = asyncio.create_task(turn())
            await started.wait()
            TurnContext.stop(THREAD, StopReason.USER_STOP)
            return await task

        assert asyncio.run(scenario()) == "cancelled"

    def test_turn_is_unregistered_after_cancel(self) -> None:
        async def scenario() -> bool:
            started = asyncio.Event()

            async def turn() -> None:
                with TurnContext.open(THREAD, FakeTurn()):
                    started.set()
                    await asyncio.sleep(30)

            task = asyncio.create_task(turn())
            await started.wait()
            TurnContext.stop(THREAD, StopReason.USER_STOP)
            with pytest.raises(asyncio.CancelledError):
                await task
            return TurnContext.active(THREAD) is None

        assert asyncio.run(scenario()) is True


class TestStopButton:
    """Кнопка Stop — единственный способ оборвать ход."""

    def test_button_stops_the_open_turn(self) -> None:
        async def scenario() -> StopReason | None:
            with TurnContext.open(THREAD, FakeTurn()) as context:
                assert ChatTurn.stop(THREAD) is True
                return context.cancellation.reason

        assert asyncio.run(scenario()) is StopReason.USER_STOP

    def test_button_without_turn_stops_nothing(self) -> None:
        assert ChatTurn.stop(THREAD) is False

    def test_turn_survives_when_nobody_pressed_stop(self) -> None:
        """Разрыв связи сам по себе ход не трогает: он доигрывает до конца."""

        async def scenario() -> bool:
            with TurnContext.open(THREAD, FakeTurn()) as context:
                await asyncio.sleep(0)
                return context.cancellation.cancelled

        assert asyncio.run(scenario()) is False
