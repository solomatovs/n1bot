"""Единая точка отмены: ход обрывает кнопка Stop, и только она."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path

import pytest
from chainlit_stand import FakeTurn

from boba.cancellation import (
    StopReason,
    ToolStopped,
    current_cancellation,
)
from boba.runtime.journal import DirVault, StreamJournal
from boba.stand.refs import StandRefs
from boba.stand_core.context import CallStand
from boba.toolkit.channels import CallOutcome, ToolChannel
from boba.toolrun.streams import CallJournals

THREAD = "thread-1"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    "остановка хода не зависит от сессии chainlit"


class TestRegistry:
    """Ход адресуется thread_id — иначе до него не дотянуться снаружи."""

    def test_turn_is_addressable_while_open(
        self, runtime_stand: StandRefs, call_stand: CallStand
    ) -> None:
        if runtime_stand.runs.active(THREAD) is not None:
            raise AssertionError("runtime_stand.runs.active(THREAD) is None")
        with runtime_stand.runs.open(call_stand.context(THREAD), FakeTurn()) as context:
            if runtime_stand.runs.active(THREAD) is not context:
                raise AssertionError("runtime_stand.runs.active(THREAD) is context")
        if runtime_stand.runs.active(THREAD) is not None:
            raise AssertionError("runtime_stand.runs.active(THREAD) is None")

    def test_stop_cancels_the_open_turn(
        self, runtime_stand: StandRefs, call_stand: CallStand
    ) -> None:
        with runtime_stand.runs.open(call_stand.context(THREAD), FakeTurn()) as context:
            if runtime_stand.runs.stop(THREAD, StopReason.USER_STOP) is not True:
                raise AssertionError(
                    "runtime_stand.runs.stop(THREAD, StopReason.USER_STOP) …"
                )
            if context.cancellation.cancelled is not True:
                raise AssertionError("context.cancellation.cancelled is True")
            if context.cancellation.reason is not StopReason.USER_STOP:
                raise AssertionError("context.cancellation.reason is StopReason.USER_…")

    def test_stop_without_turn_is_reported(self, runtime_stand: StandRefs) -> None:
        if runtime_stand.runs.stop(THREAD, StopReason.USER_STOP) is not False:
            raise AssertionError(
                "runtime_stand.runs.stop(THREAD, StopReason.USER_STOP) is F…"
            )

    def test_stop_reaches_the_context_of_the_turn(
        self, runtime_stand: StandRefs, call_stand: CallStand
    ) -> None:
        """Инструменты читают отмену из контекста: снаружи и изнутри один объект."""
        with runtime_stand.runs.open(call_stand.context(THREAD), FakeTurn()):
            runtime_stand.runs.stop(THREAD, StopReason.USER_STOP)
            if current_cancellation().cancelled is not True:
                raise AssertionError("current_cancellation().cancelled is True")
            with pytest.raises(ToolStopped):
                current_cancellation().raise_if_cancelled()

    def test_stop_reaches_worker_threads(
        self, runtime_stand: StandRefs, call_stand: CallStand
    ) -> None:
        """Синхронные инструменты живут в тред-пуле: флаг обязан доезжать и туда."""
        with runtime_stand.runs.open(call_stand.context(THREAD), FakeTurn()):
            ctx = copy_context()
            runtime_stand.runs.stop(THREAD, StopReason.USER_STOP)
            with ThreadPoolExecutor(1) as pool:
                seen = pool.submit(ctx.run, lambda: current_cancellation().cancelled)
                if seen.result() is not True:
                    raise AssertionError("seen.result() is True")

    def test_stop_is_thread_safe(
        self, runtime_stand: StandRefs, call_stand: CallStand
    ) -> None:
        """Кнопку жмут из обработчика сокета — это чужой поток."""
        with runtime_stand.runs.open(call_stand.context(THREAD), FakeTurn()) as context:
            stopper = threading.Thread(
                target=runtime_stand.runs.stop, args=(THREAD, StopReason.USER_STOP)
            )
            stopper.start()
            stopper.join()
            if context.cancellation.cancelled is not True:
                raise AssertionError("context.cancellation.cancelled is True")

    def test_new_turn_supersedes_the_stale_one(
        self, runtime_stand: StandRefs, call_stand: CallStand
    ) -> None:
        """Второй ход того же треда обрывает забытый первый, а не копится рядом."""
        with (
            runtime_stand.runs.open(call_stand.context(THREAD), FakeTurn()) as first,
            runtime_stand.runs.open(call_stand.context(THREAD), FakeTurn()) as second,
        ):
            if first.cancellation.cancelled is not True:
                raise AssertionError("first.cancellation.cancelled is True")
            if first.cancellation.reason is not StopReason.SUPERSEDED:
                raise AssertionError("first.cancellation.reason is StopReason.SUPERSE…")
            if second.cancellation.cancelled is not False:
                raise AssertionError("second.cancellation.cancelled is False")

    def test_release_keeps_the_newer_turn(
        self, runtime_stand: StandRefs, call_stand: CallStand
    ) -> None:
        """Выход из старого хода не должен снимать с учёта новый."""
        with runtime_stand.runs.open(call_stand.context(THREAD), FakeTurn()):
            with runtime_stand.runs.open(
                call_stand.context(THREAD), FakeTurn()
            ) as second:
                pass
            if runtime_stand.runs.active(THREAD) is second:
                raise AssertionError("runtime_stand.runs.active(THREAD) is not second")

    def test_tools_reach_the_turn_of_the_thread(
        self, runtime_stand: StandRefs, call_stand: CallStand
    ) -> None:
        """Инструменты находят ход по thread_id — им нужен шаг ответа."""
        turn = FakeTurn()
        with runtime_stand.runs.open(call_stand.context(THREAD), turn):
            if runtime_stand.runs.port_of(THREAD) is not turn:
                raise AssertionError("runtime_stand.runs.port_of(THREAD) is turn")
        if runtime_stand.runs.port_of(THREAD) is not None:
            raise AssertionError("runtime_stand.runs.port_of(THREAD) is None")


class TestLiveArtifacts:
    """Живые журналы гаснут вместе с запуском, файлы — нет."""

    @staticmethod
    def _journals(runtime_stand: StandRefs, tmp_path: Path) -> CallJournals:
        store = StreamJournal(DirVault(str(tmp_path / "journal")), reserve_bytes=0)
        journals = CallJournals(store, runtime_stand.runs)
        journals.mark_streamable(["shell"])

        return journals

    def test_streams_close_when_the_run_does(
        self, runtime_stand: StandRefs, call_stand: CallStand, tmp_path: Path
    ) -> None:
        journals = self._journals(runtime_stand, tmp_path)
        with runtime_stand.runs.open(call_stand.context(THREAD), FakeTurn()):
            stream = journals.begin("7", THREAD, "call-1", "shell")
            if stream is None:
                raise AssertionError("the journal of a streamable tool is opened")
            if journals.live(THREAD, "call-1") is not stream:
                raise AssertionError('journals.live(THREAD, "call-1") is stream')
            if journals.live_prefixes() != frozenset({stream.call_prefix}):
                raise AssertionError(journals.live_prefixes())

        if stream.probe(ToolChannel.STDOUT).note != CallOutcome.STOPPED.value:
            raise AssertionError(stream.probe(ToolChannel.STDOUT).note)
        if journals.live_prefixes() != frozenset():
            raise AssertionError(journals.live_prefixes())

    def test_run_without_streams_protects_nothing(
        self, runtime_stand: StandRefs, call_stand: CallStand, tmp_path: Path
    ) -> None:
        journals = self._journals(runtime_stand, tmp_path)
        with runtime_stand.runs.open(call_stand.context(THREAD), FakeTurn()):
            if journals.live_prefixes() != frozenset():
                raise AssertionError(journals.live_prefixes())

    def test_call_without_a_run_gets_no_journal(
        self, runtime_stand: StandRefs, tmp_path: Path
    ) -> None:
        journals = self._journals(runtime_stand, tmp_path)

        if journals.begin("7", THREAD, "call-1", "shell") is not None:
            raise AssertionError("a journal needs an active run to live in")


class TestAsyncTurn:
    """Обрыв корутины хода — прерыватель, который владелец подключает сам."""

    def test_cancel_interrupts_awaiting_turn(
        self, runtime_stand: StandRefs, call_stand: CallStand
    ) -> None:
        started = asyncio.Event()

        async def scenario() -> str:
            async def turn() -> str:
                context = call_stand.context(THREAD)
                with (
                    runtime_stand.runs.open(context, FakeTurn()),
                    runtime_stand.runs.task_abort(context.cancellation),
                ):
                    started.set()
                    try:
                        await asyncio.sleep(30)
                    except asyncio.CancelledError:
                        return "cancelled"
                return "finished"

            task = asyncio.create_task(turn())
            await started.wait()
            runtime_stand.runs.stop(THREAD, StopReason.USER_STOP)
            return await task

        if asyncio.run(scenario()) != "cancelled":
            raise AssertionError('asyncio.run(scenario()) == "cancelled"')

    def test_turn_is_unregistered_after_cancel(
        self, runtime_stand: StandRefs, call_stand: CallStand
    ) -> None:
        async def scenario() -> bool:
            started = asyncio.Event()

            async def turn() -> None:
                context = call_stand.context(THREAD)
                with (
                    runtime_stand.runs.open(context, FakeTurn()),
                    runtime_stand.runs.task_abort(context.cancellation),
                ):
                    started.set()
                    await asyncio.sleep(30)

            task = asyncio.create_task(turn())
            await started.wait()
            runtime_stand.runs.stop(THREAD, StopReason.USER_STOP)
            with pytest.raises(asyncio.CancelledError):
                await task
            return runtime_stand.runs.active(THREAD) is None

        if asyncio.run(scenario()) is not True:
            raise AssertionError("asyncio.run(scenario()) is True")


class TestStopButton:
    """Кнопка Stop — единственный способ оборвать ход."""

    def test_button_stops_the_open_turn(
        self, runtime_stand: StandRefs, call_stand: CallStand
    ) -> None:
        async def scenario() -> StopReason | None:
            with runtime_stand.runs.open(
                call_stand.context(THREAD), FakeTurn()
            ) as context:
                if runtime_stand.runs.stop(THREAD, StopReason.USER_STOP) is not True:
                    raise AssertionError("the open turn is stopped by the button")
                return context.cancellation.reason

        if asyncio.run(scenario()) is not StopReason.USER_STOP:
            raise AssertionError("asyncio.run(scenario()) is StopReason.USER_STOP")

    def test_button_without_turn_stops_nothing(self, runtime_stand: StandRefs) -> None:
        if runtime_stand.runs.stop(THREAD, StopReason.USER_STOP) is not False:
            raise AssertionError("there is no turn to stop")

    def test_turn_survives_when_nobody_pressed_stop(
        self, runtime_stand: StandRefs, call_stand: CallStand
    ) -> None:
        """Разрыв связи сам по себе ход не трогает: он доигрывает до конца."""

        async def scenario() -> bool:
            with runtime_stand.runs.open(
                call_stand.context(THREAD), FakeTurn()
            ) as context:
                await asyncio.sleep(0)
                return context.cancellation.cancelled

        if asyncio.run(scenario()) is not False:
            raise AssertionError("asyncio.run(scenario()) is False")
