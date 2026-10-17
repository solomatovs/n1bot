"""Лента чата между инстансами: ход ведёт A, рендерер B рисует по шине и догоняет
по replay.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from importlib.resources import files
from uuid import UUID, uuid4

import pytest
from acp.experimental.v2 import schema
from aid_stand import RecordingSink

from boba.aid.canvas.remote import RemoteJournals
from boba.aid.chat.feed import QuestionBody, TurnFeed
from boba.aid.domain.fields import StepField
from boba.aid.infra.config import AppConfig
from boba.aid.rendering.acp_view import ToolMedia, UpdateView
from boba.aid.rendering.chat_view import ChatView, StepRole
from boba.aid.rendering.renderer import ChatRenderer, NoSurface, ThreadRewinds
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.identity.context import Scope
from boba.identity.locks import LockMode, LockPurpose
from boba.messaging import (
    Envelope,
    FeedbackChanged,
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
from boba.runtime.storage import LocalStorageClient
from boba.runtime.turns import StaleTurnCloser
from boba.stand.refs import StandRefs
from boba.toolkit.result import MarkdownResult

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
    cfg = app_config.postgres.model_copy(update={"dbname": test_database})
    schema = app_config.db_schema
    bus = PgMessageBus(cfg, schema, name, AppName.AID, app_config.cluster)
    bus._pool_ref = pool
    schema_dir = files("boba.runtime") / "schema"
    async with pool.connection() as conn:
        await PostgresSchema(schema).apply_files(conn, schema_dir, {})

    await bus.start()
    locks = PgLiveLocks(cfg, schema, name, AppName.AID, app_config.cluster)
    locks._pool_ref = pool
    await locks.register_instance()
    payloads = PgPayloadStore(cfg, schema)
    payloads._pool_ref = pool
    return Node(bus, locks, payloads)


@pytest.fixture
async def nodes(
    app_config: AppConfig, test_database: str, pool: AsyncPostgresPool
) -> AsyncIterator[tuple[Node, Node]]:
    first = await _node(app_config, test_database, pool, "node1-aid")
    second = await _node(app_config, test_database, pool, "node2-aid")
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


def _renderer(
    runtime_stand: StandRefs, storage: LocalStorageClient, thread_id: str, node: Node
) -> tuple[ChatRenderer, RecordingSink]:
    sink = RecordingSink()
    view = ChatView(thread_id, sink, "u-1", storage, user_name="tester")
    updates = UpdateView(view, thread_id, RemoteJournals(), ToolMedia())
    renderer = ChatRenderer(
        thread_id, updates, node.payloads, NoSurface(), RecordingRewinds()
    )

    return renderer, sink


async def _until(condition, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + WAIT_SEC
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(what)

        await asyncio.sleep(0.05)


def _outputs(sink: RecordingSink) -> list[str]:
    return [str(step.get(StepField.OUTPUT)) for step in sink.steps]


async def test_turn_on_one_instance_is_rendered_on_another(
    runtime_stand: StandRefs,
    storage: LocalStorageClient,
    nodes: tuple[Node, Node],
) -> None:
    holder, viewer = nodes
    thread_id = str(uuid4())
    scope = Scope.chat(thread_id)
    renderer, sink = _renderer(runtime_stand, storage, thread_id, viewer)
    leave = viewer.bus.subscribe(scope, renderer.apply)

    lock = await holder.locks.acquire(
        scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=1)
    )
    feed = TurnFeed(holder.bus, holder.payloads, scope, TURN, lock.token)
    try:
        await feed.started(TURN, QuestionBody(text="question"))
        await feed.agent_update(
            schema.SessionToolCallUpdate(
                tool_call_id=CALL,
                name="kb_probe",
                status="pending",
                raw_input={"query": "x"},
            )
        )
        hits = schema.ContentToolCallContent(
            content=schema.TextContentBlock(text="hits")
        )
        await feed.agent_update(
            schema.SessionToolCallUpdate(
                tool_call_id=CALL, status="completed", content=[hits]
            )
        )
        for token in ("hel", "lo"):
            chunk = schema.AgentMessageChunk(
                message_id="m-1", content=schema.TextContentBlock(text=token)
            )
            await feed.agent_update(chunk)

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
        late, late_sink = _renderer(runtime_stand, storage, thread_id, viewer)
        assert (await late.catch_up(viewer.bus)).alive is True
        assert "hits" in _outputs(late_sink)
        assert late.turn_alive

        def late_answered() -> bool:
            # токены ответа уходят пачкой по таймеру склейки
            answer = late.view.answer_message
            if answer is None:
                return False

            return answer.content == "hello"

        await _until(late_answered, "the late renderer streamed the answer")

        await feed.finished(TurnOutcome.OK, "")
        await _until(lambda: not renderer.turn_alive, "viewer saw TurnFinished")

        after, _ = _renderer(runtime_stand, storage, thread_id, viewer)
        assert (await after.catch_up(viewer.bus)).alive is False
    finally:
        leave()
        await holder.locks.release(lock.token)


async def test_payload_store_keeps_bodies_and_purges_idle(
    nodes: tuple[Node, Node],
) -> None:
    holder, viewer = nodes
    scope = Scope.chat(str(uuid4()))

    model_ref = await holder.payloads.put(scope, MarkdownResult(text="t", elapsed_ms=1))
    text_ref = await holder.payloads.put(scope, "plain")
    args_ref = await holder.payloads.put(scope, {"query": "x", "n": 2})

    body = await viewer.payloads.get(model_ref)
    assert isinstance(body, dict)
    assert body["text"] == "t"
    assert await viewer.payloads.get(text_ref) == "plain"
    assert await viewer.payloads.get(args_ref) == {"query": "x", "n": 2}

    # база набора общая: старые тела других тестов чистка вправе унести,
    # свежие тела этой области обязаны её пережить
    await viewer.payloads.purge_idle(3600)
    assert await viewer.payloads.purge(scope) == 3
    assert await viewer.bus.purge_idle(3600) >= 0


async def test_reaper_closes_the_turn_of_a_dead_holder_and_resume_marks_it(
    runtime_stand: StandRefs,
    storage: LocalStorageClient,
    app_config: AppConfig,
    test_database: str,
    pool: AsyncPostgresPool,
) -> None:
    """Держатель хода умер: сторож закрывает ход, чужой рендерер видит причину."""
    short = app_config.model_copy(
        update={
            "cluster": app_config.cluster.model_copy(
                update={"lock_ttl_sec": 2, "heartbeat_sec": 1, "reaper_period_sec": 1}
            )
        }
    )
    holder = await _node(short, test_database, pool, "node1-aid")
    viewer = await _node(short, test_database, pool, "node2-aid")
    try:
        thread_id = str(uuid4())
        scope = Scope.chat(thread_id)
        lock = await holder.locks.acquire(
            scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=1)
        )
        feed = TurnFeed(holder.bus, holder.payloads, scope, TURN, lock.token)
        await feed.started(TURN, QuestionBody(text="question"))

        await asyncio.sleep(2.5)
        stale = await viewer.locks.reap()
        assert [s.scope for s in stale if s.scope == scope]

        closed = await StaleTurnCloser(viewer.bus, viewer.locks).close(stale)
        assert closed == 1

        late, _ = _renderer(runtime_stand, storage, thread_id, viewer)
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


class RecordingRewinds(ThreadRewinds):
    """Реализация ThreadRewinds в тестах: копит срезы истории тредов."""

    def __init__(self) -> None:
        self.rewound_to: list[tuple[str, str]] = []

    async def rewound(self, thread_id: str, session_id: str) -> None:
        self.rewound_to.append((thread_id, session_id))


class RecordingSurface(NoSurface):
    """Поверхность в тестах: считает перечитывания ленты и копит обновлённые
    шаги.
    """

    def __init__(self) -> None:
        self.resumed = 0
        self.refreshed: list[str] = []

    async def resume_thread(self) -> None:
        self.resumed += 1

    async def refresh_step(self, step_id: str) -> None:
        self.refreshed.append(step_id)


async def test_feedback_reaches_the_viewer_instance(
    runtime_stand: StandRefs,
    storage: LocalStorageClient,
    nodes: tuple[Node, Node],
) -> None:
    """Оценка шага доходит до рендерера на другом инстансе."""
    holder, viewer = nodes
    thread_id = str(uuid4())
    scope = Scope.chat(thread_id)
    surface = RecordingSurface()
    view = ChatView(
        thread_id,
        RecordingSink(),
        "u-1",
        storage,
        user_name="tester",
    )
    updates = UpdateView(view, thread_id, RemoteJournals(), ToolMedia())
    renderer = ChatRenderer(
        thread_id, updates, viewer.payloads, surface, RecordingRewinds()
    )
    leave = viewer.bus.subscribe(scope, renderer.apply)
    try:
        feedback = FeedbackChanged(step_id="answer-0", value=1, comment="ok")
        await holder.bus.publish(scope, feedback, LockToken.local())
        await _until(
            lambda: surface.refreshed == ["answer-0"], "viewer refreshed the step"
        )
    finally:
        leave()


async def test_rewound_history_reaches_the_tabs_of_the_viewer_instance(
    runtime_stand: StandRefs,
    storage: LocalStorageClient,
    nodes: tuple[Node, Node],
) -> None:
    """Срез истории правкой держателя доходит до рендерера треда на другом
    инстансе: его вкладки перечитывают ветку."""
    holder, viewer = nodes
    thread_id = str(uuid4())
    scope = Scope.chat(thread_id)
    view = ChatView(
        thread_id,
        RecordingSink(),
        "u-1",
        storage,
        user_name="tester",
    )
    updates = UpdateView(view, thread_id, RemoteJournals(), ToolMedia())
    rewinds = RecordingRewinds()
    renderer = ChatRenderer(thread_id, updates, viewer.payloads, NoSurface(), rewinds)
    leave = viewer.bus.subscribe(scope, renderer.apply)
    lock = await holder.locks.acquire(
        scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=1)
    )
    feed = TurnFeed(holder.bus, holder.payloads, scope, TURN, lock.token)
    try:
        await feed.rewound("branch-1")
        await _until(lambda: bool(rewinds.rewound_to), "viewer got the rewind")
    finally:
        leave()
        await holder.locks.release(lock.token)

    assert rewinds.rewound_to == [(thread_id, "branch-1")]
