"""Лента чата между инстансами: ход ведёт A, рендерер B рисует по шине и догоняет
по replay.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from boba.chainlit.chat.feed import TurnFeed
from boba.chainlit.domain.fields import StepField
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.rendering.chat_view import ChatView, RecordingSink, StepRole
from boba.chainlit.rendering.renderer import ChatRenderer, NoSurface
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import Scope
from boba.identity.locks import LockMode, LockPurpose
from boba.messaging import (
    Envelope,
    LockToken,
    Notice,
    NoticeLevel,
    TurnFinished,
    TurnOutcome,
)
from boba.runtime.bus import PgMessageBus
from boba.runtime.config import AppName
from boba.runtime.locks import PgLiveLocks
from boba.runtime.payloads import PgPayloadStore
from boba.runtime.turns import StaleTurnCloser
from boba.toolkit.result import TextResult

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

WAIT_SEC = 5.0
TURN = "msg-1"
CALL = "call-1"


class Node:
    """Один инстанс: шина, блокировки и хранилище тел над тестовой базой."""

    def __init__(
        self, bus: PgMessageBus, locks: PgLiveLocks, payloads: PgPayloadStore
    ) -> None:
        self.bus = bus
        self.locks = locks
        self.payloads = payloads


async def _node(
    app_config: AppConfig, test_database: str, pool: AsyncPostgresPool, name: str
) -> Node:
    cfg = app_config.data_layer.postgres.model_copy(update={"dbname": test_database})
    schema = app_config.data_layer.db_schema
    bus = PgMessageBus(cfg, schema, name, AppName.CHAINLIT, app_config.cluster)
    bus._pool_ref = pool
    await bus.setup()
    await bus.start()
    locks = PgLiveLocks(cfg, schema, name, app_config.cluster)
    locks._pool_ref = pool
    payloads = PgPayloadStore(cfg, schema)
    payloads._pool_ref = pool
    return Node(bus, locks, payloads)


@pytest.fixture
async def nodes(
    app_config: AppConfig, test_database: str, pool: AsyncPostgresPool
) -> AsyncIterator[tuple[Node, Node]]:
    first = await _node(app_config, test_database, pool, "node1-chainlit")
    second = await _node(app_config, test_database, pool, "node2-chainlit")
    try:
        yield first, second
    finally:
        await _stopped(first.bus)
        await _stopped(second.bus)


async def _stopped(bus: PgMessageBus) -> None:
    """Останавливает шину с пределом ожидания: зависший слушатель — ошибка теста."""
    try:
        await asyncio.wait_for(bus.stop(), WAIT_SEC)
    except TimeoutError as exc:
        stacks: list[str] = []
        for task in asyncio.all_tasks():
            frames = task.get_stack()
            if not frames:
                continue

            frame = frames[-1]
            stacks.append(f"{task.get_name()}: {frame.f_code.co_name}:{frame.f_lineno}")

        msg = f"bus.stop() hung; tasks: {stacks}"
        raise AssertionError(msg) from exc


def _renderer(thread_id: str, node: Node) -> tuple[ChatRenderer, RecordingSink]:
    sink = RecordingSink()
    view = ChatView(thread_id, sink, user_name="tester")
    return ChatRenderer(thread_id, view, node.payloads, NoSurface()), sink


async def _until(condition, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + WAIT_SEC
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(what)

        await asyncio.sleep(0.05)


def _outputs(sink: RecordingSink) -> list[str]:
    return [str(step.get(StepField.OUTPUT)) for step in sink.steps]


async def test_turn_on_one_instance_is_rendered_on_another(
    nodes: tuple[Node, Node],
) -> None:
    holder, viewer = nodes
    thread_id = str(uuid4())
    scope = Scope.chat(thread_id)
    renderer, sink = _renderer(thread_id, viewer)
    leave = viewer.bus.subscribe(scope, renderer.apply)

    lock = await holder.locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.TURN, 1)
    feed = TurnFeed(holder.bus, holder.payloads, scope, TURN, lock.token)
    try:
        await feed.started(TURN)
        await feed.model_answered()
        await feed.tool_started(CALL, "kb_probe", {"query": "x"})
        await feed.tool_finished(CALL, TextResult(text="hits", elapsed_ms=10))
        for token in ("hel", "lo"):
            await feed.answer_token(TURN, token)

        await feed.answer_closed(TURN)

        tool_id = ChatView.derive_id(thread_id, CALL, StepRole.TOOL)

        def rendered() -> bool:
            ids = [step.get(StepField.ID) for step in sink.steps]
            answer = renderer.view.answer_message
            if answer is None:
                return False

            return tool_id in ids and answer.content == "hello"

        await _until(rendered, f"viewer rendered the turn: {sink.steps}")
        assert renderer.turn_alive
        assert "hits" in _outputs(sink)

        # вкладка подключилась посреди хода на другом инстансе: догон по replay
        late, late_sink = _renderer(thread_id, viewer)
        assert (await late.catch_up(viewer.bus)).alive is True
        assert "hits" in _outputs(late_sink)
        assert late.turn_alive
        late_answer = late.view.answer_message
        assert late_answer is not None
        assert late_answer.content == "hello"

        await feed.finished(TurnOutcome.OK, "")
        await _until(lambda: not renderer.turn_alive, "viewer saw TurnFinished")

        after = ChatRenderer(
            thread_id,
            ChatView(thread_id, RecordingSink()),
            viewer.payloads,
            NoSurface(),
        )
        assert (await after.catch_up(viewer.bus)).alive is False
    finally:
        leave()
        await holder.locks.release(lock.token)


async def test_payload_store_keeps_bodies_and_purges_idle(
    nodes: tuple[Node, Node],
) -> None:
    holder, viewer = nodes
    scope = Scope.chat(str(uuid4()))

    model_ref = await holder.payloads.put(scope, TextResult(text="t", elapsed_ms=1))
    text_ref = await holder.payloads.put(scope, "plain")
    args_ref = await holder.payloads.put(scope, {"query": "x", "n": 2})

    body = await viewer.payloads.get(model_ref)
    assert isinstance(body, dict)
    assert body["text"] == "t"
    assert await viewer.payloads.get(text_ref) == "plain"
    assert await viewer.payloads.get(args_ref) == {"query": "x", "n": 2}

    assert await viewer.payloads.purge_idle(3600) == 0
    assert await viewer.payloads.purge(scope) == 3
    assert await viewer.bus.purge_idle(3600) >= 0


async def test_reaper_closes_the_turn_of_a_dead_holder_and_resume_marks_it(
    app_config: AppConfig, test_database: str, pool: AsyncPostgresPool
) -> None:
    """Держатель хода умер: сторож закрывает ход, чужой рендерер видит причину."""
    short = app_config.model_copy(
        update={
            "cluster": app_config.cluster.model_copy(
                update={"lock_ttl_sec": 2, "heartbeat_sec": 1, "reaper_period_sec": 1}
            )
        }
    )
    holder = await _node(short, test_database, pool, "node1-chainlit")
    viewer = await _node(short, test_database, pool, "node2-chainlit")
    try:
        thread_id = str(uuid4())
        scope = Scope.chat(thread_id)
        lock = await holder.locks.acquire(
            scope, LockMode.EXCLUSIVE, LockPurpose.TURN, 1
        )
        feed = TurnFeed(holder.bus, holder.payloads, scope, TURN, lock.token)
        await feed.started(TURN)
        await feed.answer_token(TURN, "partial")

        await asyncio.sleep(2.5)
        stale = await viewer.locks.reap()
        assert [s.scope for s in stale if s.scope == scope]

        closed = await StaleTurnCloser(viewer.bus, viewer.locks).close(stale)
        assert closed == 1

        late, _ = _renderer(thread_id, viewer)
        caught = await late.catch_up(viewer.bus)
        assert caught.alive is False
        assert caught.interrupted == TurnFinished.HOLDER_GONE

        # повторное закрытие ничего не делает: ход уже закрыт
        assert await StaleTurnCloser(viewer.bus, viewer.locks).close(stale) == 0
    finally:
        await holder.bus.stop()
        await viewer.bus.stop()


async def test_queue_usage_stays_low_under_load(nodes: tuple[Node, Node]) -> None:
    """Два инстанса по пять ходов: всё доходит, очередь уведомлений не растёт."""
    first, second = nodes
    scopes = [Scope.chat(str(uuid4())) for _ in range(5)]
    counts: dict[str, int] = {scope.id: 0 for scope in scopes}

    async def take(envelope: Envelope) -> None:
        counts[envelope.scope.id] += 1

    for scope in scopes:
        second.bus.subscribe(scope, take)

    async def turn(bus: PgMessageBus, scope: Scope) -> None:
        for index in range(40):
            await bus.publish(
                scope,
                Notice(level=NoticeLevel.INFO, text=str(index)),
                LockToken.local(),
            )

    await asyncio.gather(
        *[turn(first.bus, scope) for scope in scopes],
        *[turn(second.bus, scope) for scope in scopes],
    )

    await _until(
        lambda: all(count == 80 for count in counts.values()), f"delivered: {counts}"
    )
    assert await first.bus.queue_usage() < 0.05
