"""Контракт шины в памяти: порядок seq, replay после пропуска, лимит тела, команды."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from boba.identity.context import Scope
from boba.messaging import (
    AnswerToken,
    BusLimit,
    CommandEnvelope,
    Envelope,
    ListenerFailedError,
    LockToken,
    MemoryMessageBus,
    MessageTooLargeError,
    Notice,
    NoticeLevel,
    RunFinished,
    StopRequested,
)

pytestmark = pytest.mark.anyio


def _scope() -> Scope:
    return Scope.chat(str(uuid4()))


def _token(text: str) -> AnswerToken:
    return AnswerToken(turn_id="t1", key="m1", token=text)


async def test_messages_arrive_in_seq_order_to_every_subscriber() -> None:
    bus = MemoryMessageBus("node1-chainlit")
    scope = _scope()
    first: list[Envelope] = []
    second: list[Envelope] = []

    async def take_first(envelope: Envelope) -> None:
        first.append(envelope)

    async def take_second(envelope: Envelope) -> None:
        second.append(envelope)

    leave = bus.subscribe(scope, take_first)
    bus.subscribe(scope, take_second)
    token = LockToken.local()

    seqs = [await bus.publish(scope, _token(text), token) for text in ("a", "b", "c")]

    assert seqs == [1, 2, 3]
    assert [e.seq for e in first] == [1, 2, 3]
    assert [e.message.model_dump()["token"] for e in second] == ["a", "b", "c"]
    assert first[0].origin == "node1-chainlit"
    assert first[0].scope == scope

    leave()
    await bus.publish(scope, _token("d"), token)

    assert len(first) == 3
    assert len(second) == 4


async def test_identical_messages_are_delivered_both_times() -> None:
    bus = MemoryMessageBus("n")
    scope = _scope()
    seen: list[int] = []

    async def take(envelope: Envelope) -> None:
        seen.append(envelope.seq)

    bus.subscribe(scope, take)
    token = LockToken.local()
    await bus.publish(scope, _token("same"), token)
    await bus.publish(scope, _token("same"), token)

    assert seen == [1, 2]


async def test_replay_returns_only_messages_after_the_given_seq() -> None:
    bus = MemoryMessageBus("n")
    scope = _scope()
    token = LockToken.local()
    for text in ("a", "b", "c", "d"):
        await bus.publish(scope, _token(text), token)

    tail = await bus.replay(scope, after_seq=2)

    assert [e.seq for e in tail] == [3, 4]
    assert await bus.replay(_scope(), after_seq=0) == []


async def test_oversized_body_is_rejected_before_delivery() -> None:
    bus = MemoryMessageBus("n")
    scope = _scope()
    seen: list[Envelope] = []

    async def take(envelope: Envelope) -> None:
        seen.append(envelope)

    bus.subscribe(scope, take)
    huge = Notice(level=NoticeLevel.INFO, text="x" * (BusLimit.BODY_MAX_BYTES + 1))

    with pytest.raises(MessageTooLargeError):
        await bus.publish(scope, huge, LockToken.local())

    assert seen == []
    assert await bus.replay(scope, after_seq=0) == []


async def test_failing_listener_is_reported_after_the_others_got_the_message() -> None:
    bus = MemoryMessageBus("n")
    scope = _scope()
    seen: list[int] = []

    async def broken(envelope: Envelope) -> None:
        raise RuntimeError("boom")

    async def take(envelope: Envelope) -> None:
        seen.append(envelope.seq)

    bus.subscribe(scope, broken)
    bus.subscribe(scope, take)

    with pytest.raises(ListenerFailedError, match="boom") as caught:
        await bus.publish(scope, _token("a"), LockToken.local())

    assert seen == [1]
    assert [type(f).__name__ for f in caught.value.failures] == ["RuntimeError"]
    assert await bus.replay(scope, after_seq=0) != []


async def test_commands_reach_every_process_but_are_taken_once() -> None:
    bus = MemoryMessageBus("n")
    scope = Scope.workflow(uuid4())
    seen: list[CommandEnvelope] = []

    async def take(envelope: CommandEnvelope) -> None:
        seen.append(envelope)

    bus.subscribe_commands(take)
    command_id = await bus.command(scope, StopRequested(by_user=7, by_instance="n"))

    assert [e.command_id for e in seen] == [command_id]
    assert seen[0].scope == scope

    taken = await asyncio.gather(
        bus.take(scope, command_id, "node1-studio"),
        bus.take(scope, command_id, "node2-studio"),
    )

    assert sorted(taken) == [False, True]


async def test_purge_drops_stored_messages_and_restarts_seq() -> None:
    bus = MemoryMessageBus("n")
    scope = Scope.workflow(uuid4())
    token = LockToken.local()
    await bus.publish(scope, RunFinished(run_id=uuid4(), status="done"), token)

    assert await bus.purge(scope) == 1
    assert await bus.replay(scope, after_seq=0) == []
    assert await bus.publish(scope, _token("a"), token) == 1
