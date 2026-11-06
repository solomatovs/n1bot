"""Контракты ProcessStop.run/on_stop: причины ошибок и завершение finally."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Mapping

import pytest

from boba.cancellation import ProcessSignalError, ProcessStop, ProcessStopError
from boba.core.process import ProcessSignalError as CoreProcessSignalError

pytestmark = pytest.mark.anyio


class StopWork:
    """Работа и действия с барьерами начала, очистки и её завершения."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.finish = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.actions: list[signal.Signals] = []
        self.errors: list[object] = []
        self.failures: list[ValueError] = []

    async def run(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleaning.set()
            await self.finish.wait()
            self.cleaned.set()

    def record(self, received: signal.Signals) -> None:
        self.actions.append(received)

    def fail(self, received: signal.Signals) -> None:
        failure = ValueError("injected stop failure")
        self.failures.append(failure)
        raise failure

    async def in_signals(self, stop: ProcessStop) -> None:
        async with stop.signals:
            self.started.set()
            await self.finish.wait()

    async def run_failure(self) -> None:
        self.fail(signal.SIGTERM)

    def release(self, received: signal.Signals) -> None:
        self.finish.set()

    def error(
        self, loop: asyncio.AbstractEventLoop, context: Mapping[str, object]
    ) -> None:
        self.errors.append(context["exception"])


async def test_repeated_stop_runs_actions_once_and_waits_for_cleanup() -> None:
    work, stop = StopWork(), ProcessStop()
    stop.on_stop(work.record)
    running = asyncio.create_task(stop.run(work.run()))
    try:
        await asyncio.wait_for(work.started.wait(), 2)
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(work.cleaning.wait(), 2)
        signal.raise_signal(signal.SIGINT)
        assert not running.done()
        work.finish.set()
        await asyncio.wait_for(running, 2)
        assert work.cleaned.is_set()
        assert work.actions == [signal.SIGTERM]
    finally:
        work.finish.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_external_cancellation_is_not_reported_as_successful_signal_stop() -> (
    None
):
    work, stop = StopWork(), ProcessStop()
    stop.on_stop(work.record)
    running = asyncio.create_task(stop.run(work.run()))
    try:
        await asyncio.wait_for(work.started.wait(), 2)
        work.finish.set()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        assert work.cleaned.is_set()
        assert work.actions == []
    finally:
        work.finish.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_failing_action_does_not_skip_other_actions_or_work_cleanup() -> None:
    work, stop = StopWork(), ProcessStop()
    stop.on_stop(work.fail)
    stop.on_stop(work.record)
    stop.on_stop(work.fail)
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(work.error)
    running = asyncio.create_task(stop.run(work.run()))
    try:
        await asyncio.wait_for(work.started.wait(), 2)
        work.finish.set()
        signal.raise_signal(signal.SIGTERM)
        with pytest.raises(ProcessSignalError, match="injected stop failure") as caught:
            await asyncio.wait_for(running, 2)

        assert type(caught.value) is CoreProcessSignalError
        causes = caught.value.__cause__
        assert isinstance(causes, ExceptionGroup)
        assert len(causes.exceptions) == 1
        delivery = causes.exceptions[0]
        assert isinstance(delivery, ProcessSignalError)
        stop_failure = delivery.__cause__
        assert isinstance(stop_failure, ProcessStopError)
        action_failures = stop_failure.__cause__
        assert isinstance(action_failures, ExceptionGroup)
        assert action_failures.exceptions == tuple(work.failures)
        assert len(work.failures) == 2
        assert work.actions == [signal.SIGTERM]
        assert work.cleaned.is_set()
        assert len(work.errors) == 1
    finally:
        work.finish.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        loop.set_exception_handler(previous)


async def test_signals_context_exposes_the_same_public_error_as_run() -> None:
    work, stop = StopWork(), ProcessStop()
    stop.on_stop(work.fail)
    stop.on_stop(work.release)
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(work.error)
    running = asyncio.create_task(work.in_signals(stop))
    try:
        await asyncio.wait_for(work.started.wait(), 2)
        signal.raise_signal(signal.SIGINT)
        with pytest.raises(ProcessSignalError, match="injected stop failure") as caught:
            await asyncio.wait_for(running, 2)

        assert type(caught.value) is CoreProcessSignalError
        assert work.finish.is_set()
        assert len(work.errors) == 1
    finally:
        work.finish.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        loop.set_exception_handler(previous)


async def test_subscription_failure_is_catchable_from_cancellation_package() -> None:
    work, owner, competing = StopWork(), ProcessStop(), ProcessStop()
    async with owner.signals:
        with pytest.raises(ProcessSignalError, match="another instance owns"):
            await competing.run(work.run())

    assert not work.started.is_set()


async def test_work_error_is_preserved_without_signal_failure() -> None:
    work, stop = StopWork(), ProcessStop()
    with pytest.raises(ValueError, match="injected stop failure") as caught:
        await stop.run(work.run_failure())

    assert caught.value is work.failures[0]
