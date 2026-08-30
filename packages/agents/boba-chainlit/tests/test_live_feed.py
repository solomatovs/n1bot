"""Лента чата между инстансами: ход ведёт A, рендерер B рисует по шине и догоняет
по replay.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from chainlit.element import ElementDict
from conftest import make_context

from boba.chainlit.chat.feed import QuestionBody, ShownElement, TurnFeed
from boba.chainlit.domain.fields import StepField
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.rendering.chat_view import ChatView, RecordingSink, StepRole
from boba.chainlit.rendering.renderer import ChatRenderer, NoSurface
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import Scope
from boba.identity.locks import LockMode, LockPurpose
from boba.identity.run import RunRegistry
from boba.messaging import (
    ElementRemoved,
    Envelope,
    FeedbackChanged,
    LockToken,
    Notice,
    NoticeLevel,
    StreamAppended,
    ThreadRewound,
    TurnFinished,
    TurnOutcome,
)
from boba.runtime.bus import PgMessageBus
from boba.runtime.config import AppName
from boba.runtime.journal import DirVault, StreamJournal
from boba.runtime.locks import PgLiveLocks
from boba.runtime.payloads import PgPayloadStore
from boba.runtime.turns import StaleTurnCloser
from boba.toolkit.channels import CallOutcome, ToolChannel
from boba.toolkit.result import TextResult
from boba.toolrun.streams import StreamPump, StreamPumps, ToolStream, ToolStreams

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
    locks = PgLiveLocks(cfg, schema, name, AppName.STUDIO, app_config.cluster)
    locks._pool_ref = pool
    await locks.register_instance()
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


def _stream_messages(seen: Iterator[Envelope]) -> Iterator[StreamAppended]:
    for envelope in seen:
        message = envelope.message
        if not isinstance(message, StreamAppended):
            continue

        yield message


async def test_turn_on_one_instance_is_rendered_on_another(
    nodes: tuple[Node, Node],
) -> None:
    holder, viewer = nodes
    thread_id = str(uuid4())
    scope = Scope.chat(thread_id)
    renderer, sink = _renderer(thread_id, viewer)
    leave = viewer.bus.subscribe(scope, renderer.apply)

    lock = await holder.locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=1))
    feed = TurnFeed(holder.bus, holder.payloads, scope, TURN, lock.token)
    try:
        await feed.started(TURN, QuestionBody(text="question"))
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
            scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=1)
        )
        feed = TurnFeed(holder.bus, holder.payloads, scope, TURN, lock.token)
        await feed.started(TURN, QuestionBody(text="question"))
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


async def test_stream_growth_is_published_through_the_pump(
    nodes: tuple[Node, Node], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Инструмент пишет журнал в своём потоке на инстансе A; насос хода публикует
    StreamAppended, и инстанс B видит рост канала и его закрытие с итогом.
    """
    holder, viewer = nodes
    thread_id = str(uuid4())
    scope = Scope.chat(thread_id)
    ToolStreams.reset()
    ToolStreams.configure(
        StreamJournal(DirVault(str(tmp_path / "journal")), reserve_bytes=0)
    )
    monkeypatch.setattr(StreamPump, "COALESCE_SEC", 0.02)

    seen: list[Envelope] = []

    async def collect(envelope: Envelope) -> None:
        seen.append(envelope)

    leave = viewer.bus.subscribe(scope, collect)
    lock = await holder.locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=1))
    feed = TurnFeed(holder.bus, holder.payloads, scope, TURN, lock.token)
    pumps = StreamPumps(feed)
    line = b"line 00\n"
    lines = 20

    def write_all(stream: ToolStream) -> None:
        sink = stream.sink_of(ToolChannel.STDOUT)
        for index in range(lines):
            sink.feed(b"line %02d\n" % index)
            time.sleep(0.01)

        stream.close(str(CallOutcome.FINISHED))

    try:
        with RunRegistry.open(make_context(thread_id), on_stream=pumps.opened):
            stream = ToolStreams.begin("7", thread_id, CALL, "shell")
            assert stream is not None
            await asyncio.to_thread(write_all, stream)

        await pumps.close()

        def closed() -> bool:
            flags = [message.closed for message in _stream_messages(iter(seen))]
            return any(flags)

        await _until(closed, f"viewer saw the closed stream: {seen}")

        messages = list(_stream_messages(iter(seen)))
        sizes = [message.size for message in messages]
        assert sizes == sorted(sizes)
        assert sizes[-1] == lines * len(line)
        assert len(sizes) >= 2, "growth is reported before the close"
        assert messages[-1].closed
        assert messages[-1].note == str(CallOutcome.FINISHED)
        assert messages[-1].channel == ToolChannel.STDOUT.value
        assert all(message.call_id == CALL for message in messages)
    finally:
        leave()
        await holder.locks.release(lock.token)
        RunRegistry.reset()
        ToolStreams.reset()


class RecordingSurface(NoSurface):
    """Поверхность в тестах: считает перечитывания ленты, копит элементы, удаления
    и обновлённые шаги.
    """

    def __init__(self) -> None:
        self.resumed = 0
        self.elements: list[ElementDict] = []
        self.removed: list[str] = []
        self.refreshed: list[str] = []

    async def resume_thread(self) -> None:
        self.resumed += 1

    async def send_element(self, element: ElementDict) -> None:
        self.elements.append(element)

    async def remove_element(self, element_id: str) -> None:
        self.removed.append(element_id)

    async def refresh_step(self, step_id: str) -> None:
        self.refreshed.append(step_id)


ELEMENT = {
    "id": "el-1",
    "threadId": "",
    "type": "file",
    "url": "/files/a.txt",
    "chainlitKey": None,
    "name": "a.txt",
    "display": "inline",
    "objectKey": None,
    "size": None,
    "props": {"dir": "upload"},
    "page": None,
    "autoPlay": None,
    "playerConfig": None,
    "language": None,
    "forId": "answer-1",
    "mime": "text/plain",
}


async def test_rewind_and_elements_reach_the_viewer_instance(
    nodes: tuple[Node, Node],
) -> None:
    """Правка вопроса перечитывает ленту, а карточка вызова показывается у зрителя
    на другом инстансе — и по живой доставке, и по догону replay.
    """
    holder, viewer = nodes
    thread_id = str(uuid4())
    scope = Scope.chat(thread_id)
    surface = RecordingSurface()
    view = ChatView(thread_id, RecordingSink(), user_name="tester")
    renderer = ChatRenderer(thread_id, view, viewer.payloads, surface)
    leave = viewer.bus.subscribe(scope, renderer.apply)

    await holder.bus.publish(scope, ThreadRewound(turn_id=TURN), LockToken.local())
    await _until(lambda: surface.resumed == 1, "viewer re-read the thread")

    removed = ElementRemoved(element_id="el-old")
    await holder.bus.publish(scope, removed, LockToken.local())
    feedback = FeedbackChanged(step_id="answer-0", value=1, comment="ok")
    await holder.bus.publish(scope, feedback, LockToken.local())
    await _until(lambda: surface.refreshed == ["answer-0"], "viewer refreshed the step")
    assert surface.removed == ["el-old"]

    lock = await holder.locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=1))
    feed = TurnFeed(holder.bus, holder.payloads, scope, TURN, lock.token)
    element = {**ELEMENT, "threadId": thread_id}
    attachment = ShownElement.model_validate(
        {**element, "id": "upload-1", "name": "note.txt", "forId": TURN}
    )
    try:
        await feed.started(TURN, QuestionBody(text="question", elements=(attachment,)))
        await feed.tool_started(CALL, "send_file", {"path": "a.txt"})
        await feed.element_shown(CALL, element)
        await _until(
            lambda: len(surface.elements) == 2, "viewer received both elements"
        )

        # вложение вопроса приходит вместе с вопросом, карточка вызова — по ElementShown
        assert surface.elements[0].get("id") == "upload-1"
        assert surface.elements[0].get("forId") == TURN
        shown = surface.elements[1]
        assert shown.get("id") == "el-1"
        assert shown.get("forId") == "answer-1"
        assert shown.get("props") == {"dir": "upload"}
        assert shown.get("threadId") == thread_id

        late_surface = RecordingSurface()
        late = ChatRenderer(
            thread_id,
            ChatView(thread_id, RecordingSink(), user_name="tester"),
            viewer.payloads,
            late_surface,
        )
        assert (await late.catch_up(viewer.bus)).alive is True
        assert [item.get("id") for item in late_surface.elements] == [
            "upload-1",
            "el-1",
        ]

        await feed.finished(TurnOutcome.OK, "")
    finally:
        leave()
        await holder.locks.release(lock.token)
