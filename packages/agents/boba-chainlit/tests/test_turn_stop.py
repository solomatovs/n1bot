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
    TurnRegistry,
    current_cancellation,
)
from boba.chainlit.chat.turn import ChatTurn

THREAD = "thread-1"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    "остановка хода не зависит от сессии chainlit"


@pytest.fixture(autouse=True)
def registry() -> TurnRegistry:
    "чистый реестр на тест: ходы не должны протекать между сценариями"
    TurnRegistry._INSTANCE = TurnRegistry()
    return TurnRegistry.instance()


class TestRegistry:
    """Ход адресуется thread_id — иначе до него не дотянуться снаружи."""

    def test_turn_is_addressable_while_open(self, registry: TurnRegistry) -> None:
        assert registry.active(THREAD) is None
        with registry.open(THREAD) as cancellation:
            assert registry.active(THREAD) is cancellation
        assert registry.active(THREAD) is None

    def test_stop_cancels_the_open_turn(self, registry: TurnRegistry) -> None:
        with registry.open(THREAD) as cancellation:
            assert registry.stop(THREAD, StopReason.USER_STOP) is True
            assert cancellation.cancelled is True
            assert cancellation.reason is StopReason.USER_STOP

    def test_stop_without_turn_is_reported(self, registry: TurnRegistry) -> None:
        assert registry.stop(THREAD, StopReason.USER_STOP) is False

    def test_stop_reaches_the_context_of_the_turn(self, registry: TurnRegistry) -> None:
        """Инструменты читают отмену из контекста: снаружи и изнутри один объект."""
        with registry.open(THREAD):
            registry.stop(THREAD, StopReason.USER_STOP)
            assert current_cancellation().cancelled is True
            with pytest.raises(ToolStopped):
                current_cancellation().raise_if_cancelled()

    def test_stop_reaches_worker_threads(self, registry: TurnRegistry) -> None:
        """Синхронные инструменты живут в тред-пуле: флаг обязан доезжать и туда."""
        with registry.open(THREAD):
            ctx = copy_context()
            registry.stop(THREAD, StopReason.USER_STOP)
            with ThreadPoolExecutor(1) as pool:
                seen = pool.submit(ctx.run, lambda: current_cancellation().cancelled)
                assert seen.result() is True

    def test_stop_is_thread_safe(self, registry: TurnRegistry) -> None:
        """Кнопку жмут из обработчика сокета — это чужой поток."""
        with registry.open(THREAD) as cancellation:
            stopper = threading.Thread(
                target=registry.stop, args=(THREAD, StopReason.USER_STOP)
            )
            stopper.start()
            stopper.join()
            assert cancellation.cancelled is True

    def test_new_turn_supersedes_the_stale_one(self, registry: TurnRegistry) -> None:
        """Второй ход того же треда обрывает забытый первый, а не копится рядом."""
        with registry.open(THREAD) as first, registry.open(THREAD) as second:
            assert first.cancelled is True
            assert first.reason is StopReason.SUPERSEDED
            assert second.cancelled is False

    def test_release_keeps_the_newer_turn(self, registry: TurnRegistry) -> None:
        """Выход из старого хода не должен снимать с учёта новый."""
        with registry.open(THREAD):
            with registry.open(THREAD) as second:
                pass
            assert registry.active(THREAD) is not second


class TestAsyncTurn:
    """Асинхронный мир обрывается отменой задачи хода — это тоже прерыватель."""

    def test_cancel_interrupts_awaiting_turn(self) -> None:
        started = asyncio.Event()

        async def scenario() -> str:
            registry = TurnRegistry.instance()

            async def turn() -> str:
                with registry.open(THREAD):
                    started.set()
                    try:
                        await asyncio.sleep(30)
                    except asyncio.CancelledError:
                        return "cancelled"
                return "finished"

            task = asyncio.create_task(turn())
            await started.wait()
            registry.stop(THREAD, StopReason.USER_STOP)
            return await task

        assert asyncio.run(scenario()) == "cancelled"

    def test_turn_is_unregistered_after_cancel(self) -> None:
        async def scenario() -> bool:
            registry = TurnRegistry.instance()
            started = asyncio.Event()

            async def turn() -> None:
                with registry.open(THREAD):
                    started.set()
                    await asyncio.sleep(30)

            task = asyncio.create_task(turn())
            await started.wait()
            registry.stop(THREAD, StopReason.USER_STOP)
            with pytest.raises(asyncio.CancelledError):
                await task
            return registry.active(THREAD) is None

        assert asyncio.run(scenario()) is True


class TestStopButton:
    """Кнопка Stop — единственный способ оборвать ход."""

    def test_button_stops_the_open_turn(self) -> None:
        async def scenario() -> StopReason | None:
            registry = TurnRegistry.instance()
            with registry.open(THREAD) as cancellation:
                assert ChatTurn.stop(THREAD) is True
                return cancellation.reason

        assert asyncio.run(scenario()) is StopReason.USER_STOP

    def test_button_without_turn_stops_nothing(self) -> None:
        assert ChatTurn.stop(THREAD) is False

    def test_turn_survives_when_nobody_pressed_stop(self) -> None:
        """Разрыв связи сам по себе ход не трогает: он доигрывает до конца."""

        async def scenario() -> bool:
            registry = TurnRegistry.instance()
            with registry.open(THREAD) as cancellation:
                await asyncio.sleep(0)
                return cancellation.cancelled

        assert asyncio.run(scenario()) is False
