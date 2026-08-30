"""Шина на Postgres: два инстанса одного процесса общаются через таблицы и NOTIFY."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest

from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import Scope
from boba.messaging import (
    BusLimit,
    CommandEnvelope,
    Envelope,
    LockToken,
    MessageBusError,
    MessageKind,
    MessageTooLargeError,
    Notice,
    NoticeLevel,
    RunListChanged,
    StopRequested,
)
from boba.messaging.bus import ListenerState
from boba.runtime.bus import LiveListener, PgMessageBus, Pointer
from boba.runtime.config import AppName, RuntimeConfig

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

WAIT_SEC = 5.0


class Inbox:
    """Накопитель конвертов с ожиданием нужного количества."""

    def __init__(self) -> None:
        self.envelopes: list[Envelope] = []
        self.commands: list[CommandEnvelope] = []

    async def take(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)

    async def take_command(self, envelope: CommandEnvelope) -> None:
        self.commands.append(envelope)

    async def wait(self, count: int) -> None:
        deadline = asyncio.get_running_loop().time() + WAIT_SEC
        while len(self.envelopes) < count:
            if asyncio.get_running_loop().time() > deadline:
                msg = f"expected {count} envelopes, got {len(self.envelopes)}"
                raise AssertionError(msg)

            await asyncio.sleep(0.02)

    async def wait_commands(self, count: int) -> None:
        deadline = asyncio.get_running_loop().time() + WAIT_SEC
        while len(self.commands) < count:
            if asyncio.get_running_loop().time() > deadline:
                msg = f"expected {count} commands, got {len(self.commands)}"
                raise AssertionError(msg)

            await asyncio.sleep(0.02)

    def tokens(self) -> list[str]:
        out: list[str] = []
        for envelope in self.envelopes:
            body = envelope.message.model_dump()
            out.append(str(body["text"]))

        return out


async def _bus(
    runtime_config: RuntimeConfig, test_database: str, pool: AsyncPostgresPool, name: str
) -> PgMessageBus:
    cfg = runtime_config.data_layer.postgres.model_copy(update={"dbname": test_database})
    bus = PgMessageBus(
        cfg, runtime_config.cluster.db_schema, name, AppName.STUDIO, runtime_config.cluster
    )
    bus._pool_ref = pool
    await bus.setup()
    await bus.start()
    return bus


@pytest.fixture
async def buses(
    runtime_config: RuntimeConfig, test_database: str, pool: AsyncPostgresPool
) -> AsyncIterator[tuple[PgMessageBus, PgMessageBus]]:
    first = await _bus(runtime_config, test_database, pool, "node1-studio")
    second = await _bus(runtime_config, test_database, pool, "node2-studio")
    try:
        yield first, second
    finally:
        await first.stop()
        await second.stop()


def _token(text: str) -> Notice:
    """Сообщение без держателя: тесты шины не про блокировки."""
    return Notice(level=NoticeLevel.INFO, text=text)


async def test_messages_cross_instances_in_seq_order(
    buses: tuple[PgMessageBus, PgMessageBus],
) -> None:
    first, second = buses
    scope = Scope.chat(str(uuid4()))
    local = Inbox()
    remote = Inbox()
    first.subscribe(scope, local.take)
    second.subscribe(scope, remote.take)
    token = LockToken.local()

    seqs = [await first.publish(scope, _token(str(i)), token) for i in range(30)]

    assert seqs == list(range(1, 31))
    await local.wait(30)
    await remote.wait(30)
    assert local.tokens() == [str(i) for i in range(30)]
    assert remote.tokens() == [str(i) for i in range(30)]
    assert remote.envelopes[0].origin == "node1-studio"
    assert remote.envelopes[0].scope == scope


async def test_identical_messages_are_delivered_both_times(
    buses: tuple[PgMessageBus, PgMessageBus],
) -> None:
    first, second = buses
    scope = Scope.workflow(uuid4())
    remote = Inbox()
    second.subscribe(scope, remote.take)
    token = LockToken.local()

    await first.publish(scope, _token("same"), token)
    await first.publish(scope, _token("same"), token)

    await remote.wait(2)
    assert [e.seq for e in remote.envelopes] == [1, 2]


async def test_replay_and_purge(buses: tuple[PgMessageBus, PgMessageBus]) -> None:
    first, second = buses
    scope = Scope.workflow(uuid4())
    token = LockToken.local()
    for text in ("a", "b", "c"):
        await first.publish(scope, _token(text), token)

    tail = await second.replay(scope, after_seq=1)

    assert [e.seq for e in tail] == [2, 3]
    assert tail[0].origin == "node1-studio"

    assert await second.purge(scope) == 3
    assert await first.replay(scope, after_seq=0) == []
    assert await first.publish(scope, _token("d"), token) == 1


async def test_oversized_message_is_rejected(
    buses: tuple[PgMessageBus, PgMessageBus],
) -> None:
    first, _ = buses
    scope = Scope.chat(str(uuid4()))
    huge = Notice(level=NoticeLevel.INFO, text="x" * (BusLimit.BODY_MAX_BYTES + 1))

    with pytest.raises(MessageTooLargeError):
        await first.publish(scope, huge, LockToken.local())

    assert await first.replay(scope, after_seq=0) == []


async def test_command_reaches_both_instances_and_is_taken_once(
    buses: tuple[PgMessageBus, PgMessageBus],
) -> None:
    first, second = buses
    scope = Scope.workflow(uuid4())
    left = Inbox()
    right = Inbox()
    first.subscribe_commands(left.take_command)
    second.subscribe_commands(right.take_command)

    command_id = await second.command(
        scope, StopRequested(by_user=UUID(int=1), by_instance=second.instance)
    )

    await left.wait_commands(1)
    await right.wait_commands(1)
    assert left.commands[0].command_id == command_id
    assert left.commands[0].scope == scope
    assert isinstance(left.commands[0].command, StopRequested)

    taken = await asyncio.gather(
        first.take(scope, command_id, first.instance),
        second.take(scope, command_id, second.instance),
    )

    assert sorted(taken) == [False, True]


async def test_listener_reconnects_and_catches_up(
    buses: tuple[PgMessageBus, PgMessageBus], pool: AsyncPostgresPool
) -> None:
    first, second = buses
    scope = Scope.chat(str(uuid4()))
    remote = Inbox()
    second.subscribe(scope, remote.take)
    token = LockToken.local()
    await first.publish(scope, _token("before"), token)
    await remote.wait(1)

    async with pool.cursor() as cur:
        await cur.execute(
            "select pg_terminate_backend(%s)", (second.listener.backend_pid,)
        )

    deadline = asyncio.get_running_loop().time() + WAIT_SEC
    while second.listener.state is ListenerState.LISTENING:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)

    await first.publish(scope, _token("while-down"), token)
    await second.listener.wait_listening(WAIT_SEC)
    await first.publish(scope, _token("after"), token)

    await remote.wait(3)
    assert remote.tokens() == ["before", "while-down", "after"]


async def test_failing_subscriber_stops_the_listener_and_the_bus_refuses(
    runtime_config: RuntimeConfig, test_database: str, pool: AsyncPostgresPool
) -> None:
    bus = await _bus(runtime_config, test_database, pool, "node3-studio")
    try:
        scope = Scope.chat(str(uuid4()))
        seen: list[int] = []

        async def broken(envelope: Envelope) -> None:
            raise RuntimeError("renderer is broken")

        async def take(envelope: Envelope) -> None:
            seen.append(envelope.seq)

        bus.subscribe(scope, broken)
        bus.subscribe(scope, take)
        await bus.publish(scope, _token("a"), LockToken.local())

        deadline = asyncio.get_running_loop().time() + WAIT_SEC
        while bus.listener.state is not ListenerState.FAILED:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)

        assert seen == [1]

        with pytest.raises(MessageBusError, match="unusable"):
            await bus.publish(scope, _token("b"), LockToken.local())
    finally:
        await bus.stop()


async def test_user_scope_events_cross_instances(
    buses: tuple[PgMessageBus, PgMessageBus],
) -> None:
    first, second = buses
    scope = Scope.user(UUID(int=7))
    inbox = Inbox()
    second.subscribe(scope, inbox.take)

    await first.publish(
        scope,
        RunListChanged(
            run_id=uuid4(), workflow_id=UUID(int=1), workflow_name="w", status="pending"
        ),
        LockToken.local(),
    )

    await inbox.wait(1)
    assert inbox.envelopes[0].message.kind is MessageKind.RUN_LIST_CHANGED


async def test_listener_retries_when_the_catch_up_fails(
    runtime_config: RuntimeConfig,
    test_database: str,
    pool: AsyncPostgresPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ошибка базы при догоне после реконнекта — повтор, а не остановка слушателя."""
    cfg = runtime_config.data_layer.postgres.model_copy(update={"dbname": test_database})
    calls: list[int] = []

    async def handler(pointer: Pointer) -> None:
        return None

    async def on_reconnect() -> None:
        calls.append(len(calls))
        if len(calls) == 1:
            msg = "message bus: read failed while the pool was still dead"
            raise MessageBusError(msg)

    monkeypatch.setattr(LiveListener, "RETRY_SEC", 0.1)
    listener = LiveListener(cfg, handler, on_reconnect)
    await listener.start()
    try:
        async with pool.cursor() as cur:
            await cur.execute(
                "select pg_terminate_backend(%s)", (listener.backend_pid,)
            )

        deadline = asyncio.get_running_loop().time() + WAIT_SEC
        while len(calls) < 2:
            assert asyncio.get_running_loop().time() < deadline, calls
            await asyncio.sleep(0.05)

        await listener.wait_listening(WAIT_SEC)
        assert listener.state is ListenerState.LISTENING
        listener.ensure_alive()
    finally:
        await listener.stop()


async def test_user_scopes_of_two_applications_do_not_cross(
    runtime_config: RuntimeConfig, test_database: str, pool: AsyncPostgresPool
) -> None:
    """Одна шина на два приложения: область пользователя — его users.id, у каждой схемы
    свои uuid, поэтому события одного приложения не приходят в область другого.
    """
    chat = await _bus(runtime_config, test_database, pool, "node1-chainlit")
    studio = await _bus(runtime_config, test_database, pool, "node1-studio")
    try:
        chat_user = Scope.user(UUID(int=1))
        studio_user = Scope.user(UUID(int=2))
        foreign = Inbox()
        own = Inbox()
        studio.subscribe(studio_user, foreign.take)
        studio.subscribe(chat_user, own.take)
        token = LockToken.local()

        await chat.publish(chat_user, _token("thread-changed"), token)

        await own.wait(1)
        await asyncio.sleep(0.3)
        assert own.tokens() == ["thread-changed"]
        assert foreign.tokens() == []
    finally:
        await studio.stop()
        await chat.stop()
