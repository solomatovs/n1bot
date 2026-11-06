"""Контракты подписок: владение, доставка, отмена и сохранение причин отказа."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Mapping

import pytest

from boba.core.process import ProcessSignalError, ProcessSignals, SignalHandler

pytestmark = pytest.mark.anyio


class Actions:
    """Управляемые действия: барьеры делают гонки воспроизводимыми без sleep."""

    def __init__(self) -> None:
        self.calls: list[signal.Signals] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.twice = asyncio.Event()
        self.errors: list[object] = []
        self.running = 0
        self.peak = 0

    def record(self, received: signal.Signals) -> None:
        self.calls.append(received)
        self.started.set()

    def fail(self, received: signal.Signals) -> None:
        raise ValueError(f"broken action {received.name}")

    async def fail_async(self, received: signal.Signals) -> None:
        self.fail(received)

    async def block(self, received: signal.Signals) -> None:
        self.record(received)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await self.release.wait()
        finally:
            self.running -= 1
            self.closed.set()

        if len(self.calls) == 2:
            self.twice.set()

    def error(
        self, loop: asyncio.AbstractEventLoop, context: Mapping[str, object]
    ) -> None:
        self.errors.append(context["exception"])


async def test_on_delivers_in_order_and_restores_previous_handler() -> None:
    actions = Actions()
    owner = ProcessSignals()
    owner.on(signal.SIGUSR1, actions.record)
    owner.on(signal.SIGUSR1, actions.record)
    previous = signal.getsignal(signal.SIGUSR1)

    async with owner:
        signal.raise_signal(signal.SIGUSR1)
        await asyncio.wait_for(actions.started.wait(), 2)
        assert actions.calls == [signal.SIGUSR1, signal.SIGUSR1]

    assert signal.getsignal(signal.SIGUSR1) is previous


async def test_overlapping_owner_fails_without_damaging_active_subscription() -> None:
    actions = Actions()
    owner = ProcessSignals()
    contender = ProcessSignals()
    owner.on(signal.SIGUSR1, actions.record)
    contender.on(signal.SIGUSR1, actions.fail)
    contender.on(signal.SIGUSR2, actions.fail)
    untouched = signal.getsignal(signal.SIGUSR2)

    async with owner:
        with pytest.raises(ProcessSignalError, match="another instance"):
            async with contender:
                pytest.fail("overlapping subscriptions were accepted")

        assert signal.getsignal(signal.SIGUSR2) is untouched
        signal.raise_signal(signal.SIGUSR1)
        await asyncio.wait_for(actions.started.wait(), 2)

    assert actions.calls == [signal.SIGUSR1]


async def test_disjoint_owners_are_independent() -> None:
    left, right = ProcessSignals(), ProcessSignals()
    first, second = Actions(), Actions()
    left.on(signal.SIGUSR1, first.record)
    right.on(signal.SIGUSR2, second.record)

    async with left, right:
        signal.raise_signal(signal.SIGUSR1)
        signal.raise_signal(signal.SIGUSR2)
        await asyncio.wait_for(first.started.wait(), 2)
        await asyncio.wait_for(second.started.wait(), 2)

    assert first.calls == [signal.SIGUSR1]
    assert second.calls == [signal.SIGUSR2]


async def test_registration_and_reentry_are_rejected_after_activation() -> None:
    owner, actions = ProcessSignals(), Actions()
    async with owner:
        with pytest.raises(ProcessSignalError, match="expected new owner"):
            owner.on(signal.SIGUSR1, actions.record)

        with pytest.raises(ProcessSignalError, match="expected new owner"):
            owner.on_async(signal.SIGUSR1, actions.block)

        with pytest.raises(ProcessSignalError, match="expected new owner"):
            await owner.__aenter__()

    with pytest.raises(ProcessSignalError, match="expected new owner"):
        await owner.__aenter__()

    with pytest.raises(ProcessSignalError, match="inactive"):
        owner.cancel_pending()


@pytest.mark.parametrize("received", [signal.SIGKILL, signal.SIGSTOP])
async def test_uncatchable_signals_are_rejected(received: signal.Signals) -> None:
    owner, actions = ProcessSignals(), Actions()
    with pytest.raises(ProcessSignalError, match="cannot be caught"):
        owner.on(received, actions.record)

    with pytest.raises(ProcessSignalError, match="cannot be caught"):
        owner.on_async(received, actions.block)


async def test_registration_from_worker_thread_is_rejected() -> None:
    owner, actions = ProcessSignals(), Actions()
    with pytest.raises(ProcessSignalError, match="main thread"):
        await asyncio.to_thread(owner.on, signal.SIGUSR1, actions.record)


async def test_external_handler_is_not_overwritten_on_exit() -> None:
    owner, actions = ProcessSignals(), Actions()
    previous = signal.getsignal(signal.SIGUSR1)
    owner.on(signal.SIGUSR1, actions.record)
    try:
        with pytest.raises(ProcessSignalError, match="replaced by another owner"):
            async with owner:
                signal.signal(signal.SIGUSR1, signal.SIG_IGN)

        assert signal.getsignal(signal.SIGUSR1) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGUSR1, previous)


async def test_queued_delivery_is_ignored_after_exit() -> None:
    owner, actions = ProcessSignals(), Actions()
    owner.on(signal.SIGUSR1, actions.record)
    async with owner:
        signal.raise_signal(signal.SIGUSR1)

    # Барьер очереди event loop: обработчик уже был поставлен перед ним.
    barrier = asyncio.Event()
    asyncio.get_running_loop().call_soon(barrier.set)
    await barrier.wait()
    assert actions.calls == []


async def test_async_burst_is_coalesced_and_actions_do_not_overlap() -> None:
    owner, actions = ProcessSignals(), Actions()
    owner.on_async(signal.SIGHUP, actions.block)
    delivered = Actions()
    owner.on(signal.SIGHUP, delivered.record)
    async with owner:
        signal.raise_signal(signal.SIGHUP)
        await asyncio.wait_for(actions.started.wait(), 2)
        delivered.started.clear()
        for _ in range(40):
            signal.raise_signal(signal.SIGHUP)

        await asyncio.wait_for(delivered.started.wait(), 2)
        actions.release.set()
        await asyncio.wait_for(actions.twice.wait(), 2)

    assert len(actions.calls) == 2
    assert actions.peak == 1
    assert len(delivered.calls) == 41


async def test_cancel_pending_runs_finally_and_disables_further_async_work() -> None:
    owner, actions = ProcessSignals(), Actions()
    owner.on_async(signal.SIGHUP, actions.block)
    async with owner:
        signal.raise_signal(signal.SIGHUP)
        await asyncio.wait_for(actions.started.wait(), 2)
        owner.cancel_pending()
        owner.cancel_pending()
        await asyncio.wait_for(actions.closed.wait(), 2)
        signal.raise_signal(signal.SIGHUP)

    assert actions.calls == [signal.SIGHUP]
    assert actions.running == 0


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_action_failure_reaches_loop_and_context_with_original_cause(
    asynchronous: bool,
) -> None:
    owner, actions = ProcessSignals(), Actions()
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(actions.error)
    owner.on(signal.SIGUSR1, actions.record)
    if asynchronous:
        owner.on_async(signal.SIGUSR1, actions.fail_async)
    else:
        owner.on(signal.SIGUSR1, actions.fail)
        owner.on(signal.SIGUSR1, actions.record)

    try:
        with pytest.raises(ProcessSignalError, match="broken action") as caught:  # noqa: PT012 — ошибка контекста после доставки
            async with owner:
                signal.raise_signal(signal.SIGUSR1)
                await asyncio.wait_for(actions.started.wait(), 2)

        group = caught.value.__cause__
        assert isinstance(group, ExceptionGroup)
        assert isinstance(group.exceptions[0].__cause__, ValueError)
        assert len(actions.errors) == 1
        if not asynchronous:
            assert len(actions.calls) == 2
    finally:
        loop.set_exception_handler(previous)


async def test_install_failure_restores_already_installed_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, actions = ProcessSignals(), Actions()
    owner.on(signal.SIGUSR1, actions.record)
    owner.on(signal.SIGUSR2, actions.record)
    original = signal.signal
    saved = {
        received: signal.getsignal(received)
        for received in (signal.SIGUSR1, signal.SIGUSR2)
    }
    installs = 0

    def fail_second(received: int, handler: SignalHandler) -> SignalHandler | None:
        nonlocal installs
        installs += 1
        if installs == 2:
            raise OSError("injected registration failure")

        return original(received, handler)

    monkeypatch.setattr(signal, "signal", fail_second)
    with pytest.raises(ProcessSignalError, match="registration failure") as caught:
        await owner.__aenter__()

    assert isinstance(caught.value.__cause__, OSError)
    for received, previous in saved.items():
        assert signal.getsignal(received) is previous


async def test_cancel_from_non_owner_thread_does_not_cancel_running_action() -> None:
    owner, actions = ProcessSignals(), Actions()
    owner.on_async(signal.SIGHUP, actions.block)
    async with owner:
        signal.raise_signal(signal.SIGHUP)
        await asyncio.wait_for(actions.started.wait(), 2)
        with pytest.raises(ProcessSignalError, match="owning event loop"):
            await asyncio.to_thread(owner.cancel_pending)

        assert not actions.closed.is_set()
        owner.cancel_pending()
        await asyncio.wait_for(actions.closed.wait(), 2)
