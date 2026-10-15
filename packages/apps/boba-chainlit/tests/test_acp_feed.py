"""Лента из уведомлений ACP v2: один переводчик для живого хода (через шину)
и воспроизведения (`session/resume`); идентификаторы шагов из `messageId` и
`toolCallId` по правилам ChatView.derive_id; итог вызова из `rawOutput` —
ответа сервера MCP (артефакт, адрес журнала); панель про агента.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar, cast

import chainlit.data as chainlit_data
import mcp_types as mt
import pytest
from acp.experimental.v2 import schema
from chainlit.context import ChainlitContext, context_var, init_http_context
from chainlit.step import StepDict
from chainlit_stand import Seed
from pydantic import SecretStr

from boba.canvas.canvas import CanvasRegistry
from boba.chainlit.agent.chat import SessionOptions, ThreadAgentsIndex
from boba.chainlit.agent.client import SessionUpdate
from boba.chainlit.agent.feed import ResultMounts
from boba.chainlit.agent.panel import AgentPanel
from boba.chainlit.agent.settings import (
    AgentModel,
    AgentProvider,
    AgentSettingsError,
    ChatAgentSettings,
    PermissionModeName,
    ProviderKindName,
    UserAgentSettings,
)
from boba.chainlit.canvas.panel import ImageViewer
from boba.chainlit.canvas.remote import RemoteJournals
from boba.chainlit.chat.feed import QuestionBody, TurnFeed
from boba.chainlit.chat.panel_text import PanelText
from boba.chainlit.domain.fields import StepField
from boba.chainlit.rendering.acp_view import (
    SessionUpdateCodec,
    ToolMedia,
    UpdateView,
)
from boba.chainlit.rendering.chat_view import (
    ChatView,
    LiveSink,
    StepKind,
    StepRole,
    StepText,
)
from boba.chainlit.rendering.renderer import ChatRenderer, NoSurface
from boba.chat.profiles import ChatProfileConfig, UserSetting
from boba.identity.context import Scope
from boba.messaging import LockToken, MemoryMessageBus, MemoryPayloadStore, TurnOutcome
from boba.stand.refs import StandRefs
from boba.toolkit.dag import (
    CallDag,
    DagNode,
    JournalAddress,
    NodeOutcomes,
    WorkflowNodeResult,
    WorkflowResult,
)
from boba.toolkit.result import (
    CanvasResult,
    ErrorResult,
    FileResult,
    MarkdownResult,
    ToolResultBase,
)
from boba.toolkit.wire import ResultWire
from boba.transport.acp.protocol import IdleErrorUpdate

THREAD = "11111111-1111-1111-1111-111111111111"
QUESTION = "22222222-2222-2222-2222-222222222222"
MESSAGE = "msg_a"
SECOND_MESSAGE = "msg_b"
CALL = "call_1"
SECOND_CALL = "call_2"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры conftest: БД этим тестам не нужна."""


class FakeSession:
    """Сессия для Message и Step: им нужны только идентификаторы."""

    def __init__(self, thread_id: str) -> None:
        self.id = "session-1"
        self.thread_id = thread_id


class FakeEmitter:
    """Эмиттер-рекордер: кадры ленты копятся вместо отправки в сокет; по ним
    видно и шаги sink'а, и стримящиеся сообщения, которые идут мимо sink'а."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, StepDict]] = []
        self.tokens: dict[str, str] = {}

    async def send_step(self, step_dict: StepDict) -> None:
        self.frames.append(("new_message", step_dict))

    async def update_step(self, step_dict: StepDict) -> None:
        self.frames.append(("update_message", step_dict))

    async def delete_step(self, step_dict: StepDict) -> None:
        self.frames.append(("delete_message", step_dict))

    async def stream_start(self, step_dict: StepDict) -> None:
        self.frames.append(("stream_start", step_dict))
        self.tokens[str(step_dict.get(StepField.ID, ""))] = str(
            step_dict.get(StepField.OUTPUT) or ""
        )

    async def send_token(
        self,
        id: str,  # noqa: A002 — chainlit зовёт по имени: emitter-контракт
        token: str,
        is_sequence: bool = False,
        is_input: bool = False,
    ) -> None:
        self.tokens[id] = self.tokens.get(id, "") + token

    async def send_element(self, element_dict: Any) -> None:
        return

    async def task_start(self) -> None:
        return

    async def task_end(self) -> None:
        return

    def last(self) -> dict[str, StepDict]:
        """Последний кадр каждого шага в порядке первого появления."""
        found: dict[str, StepDict] = {}
        for _, step in self.frames:
            found[str(step.get(StepField.ID, ""))] = step

        return found

    def outline(self, skip: str) -> list[tuple[str, str, str]]:
        """Тип, id и вывод каждого шага, кроме skip (кружок ожидания)."""
        outline: list[tuple[str, str, str]] = []
        for step_id, step in self.last().items():
            if step_id == skip:
                continue

            outline.append(
                (
                    str(step.get(StepField.TYPE, "")),
                    step_id,
                    str(step.get(StepField.OUTPUT) or ""),
                )
            )

        return outline


class FakeContext:
    """Контекст ленты: сессия и эмиттер."""

    def __init__(self, thread_id: str) -> None:
        self.session = FakeSession(thread_id)
        self.emitter = FakeEmitter()


@pytest.fixture
async def recorded() -> AsyncIterator[FakeEmitter]:
    """Контекст chainlit теста с эмиттером-рекордером. Фикстура асинхронная:
    раннер anyio, переживший тест с сессионной асинхронной фикстурой, ведёт
    тесты в своей задаче, и переменную контекста видно только оттуда."""
    context = FakeContext(THREAD)
    token = context_var.set(cast("ChainlitContext", context))
    try:
        yield context.emitter
    finally:
        context_var.reset(token)


def _id(key: str, role: StepRole) -> str:
    derived = ChatView.derive_id(THREAD, key, role)
    assert derived is not None

    return derived


def _chunk(text: str, message_id: str) -> SessionUpdate:
    content = schema.TextContentBlock(text=text)

    return schema.AgentMessageChunk(message_id=message_id, content=content)


def _thought(text: str, message_id: str) -> SessionUpdate:
    content = schema.TextContentBlock(text=text)

    return schema.AgentThoughtChunk(message_id=message_id, content=content)


def _call(call_id: str, name: str, args: dict[str, Any]) -> SessionUpdate:
    return schema.SessionToolCallUpdate(
        tool_call_id=call_id,
        name=name,
        title=f"{name} {args}",
        status="pending",
        raw_input=dict(args),
    )


def _done(call_id: str, text: str, raw_output: Any) -> SessionUpdate:
    content = schema.ContentToolCallContent(content=schema.TextContentBlock(text=text))

    return schema.SessionToolCallUpdate(
        tool_call_id=call_id,
        status="completed",
        content=[content],
        raw_output=raw_output,
    )


def _result(artifact: ToolResultBase, run: str = "") -> dict[str, Any]:
    """Ответ tools/call сервера boba-mcp: текст для модели и модель результата
    в structuredContent; run — запуск, под которым сервер ведёт журнал."""
    call = DagNode(key="srv-call", tool="pg_query")
    outcome = NodeOutcomes().of(call, artifact, False)
    wire = ResultWire().packed(outcome)
    structured = wire.structured
    if run and structured is not None:
        structured = ResultWire().stamped(structured, run)

    reply = mt.CallToolResult(
        content=list(wire.content),
        structured_content=structured,
        is_error=wire.is_error,
    )

    return reply.model_dump(mode="json", by_alias=True, exclude_none=True)


def _question() -> SessionUpdate:
    return schema.UserMessageUpdate(
        message_id=QUESTION, content=[schema.TextContentBlock(text="hello")]
    )


def _turn_updates() -> list[SessionUpdate]:
    """Ход: рассуждение, вызов инструмента, ответ после него."""
    return [
        _thought("let me ", MESSAGE),
        _thought("think", MESSAGE),
        _chunk("First ", MESSAGE),
        _call(CALL, "mcp__boba__pg_query", {"sql": "select 1"}),
        schema.SessionToolCallUpdate(tool_call_id=CALL, status="in_progress"),
        _done(CALL, "1", _result(MarkdownResult(text="one row", elapsed_ms=12))),
        _chunk("and then ", SECOND_MESSAGE),
        _chunk("done.", SECOND_MESSAGE),
    ]


def _replayed_updates() -> list[SessionUpdate]:
    """Тот же ход, каким его воспроизводит агент: сообщения целиком."""
    thought = schema.AgentThoughtUpdate(
        message_id=MESSAGE, content=[schema.TextContentBlock(text="let me think")]
    )
    first = schema.AgentMessageUpdate(
        message_id=MESSAGE, content=[schema.TextContentBlock(text="First ")]
    )
    second = schema.AgentMessageUpdate(
        message_id=SECOND_MESSAGE,
        content=[schema.TextContentBlock(text="and then done.")],
    )

    return [
        thought,
        first,
        _call(CALL, "mcp__boba__pg_query", {"sql": "select 1"}),
        _done(CALL, "1", _result(MarkdownResult(text="one row", elapsed_ms=12))),
        second,
    ]


def _view(runtime_stand: StandRefs, journals: RemoteJournals) -> UpdateView:
    view = ChatView(THREAD, LiveSink(), runtime_stand.journals, user_name="alice")

    return UpdateView(view, THREAD, journals, ToolMedia())


class TestUpdateViewIds:
    """Шаги ленты получают прежние детерминированные id."""

    @pytest.mark.anyio
    async def test_replay_derives_ids_from_message_and_call_ids(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        updates = _view(runtime_stand, RemoteJournals())
        await updates.apply(_question())
        for update in _turn_updates():
            await updates.apply(update)

        await updates.finish_turn()

        steps = recorded.last()
        assert steps[QUESTION].get(StepField.TYPE) == StepKind.USER.value
        assert steps[QUESTION].get(StepField.NAME) == "alice"

        thinking = _id(MESSAGE, StepRole.THINKING)
        assert steps[thinking].get(StepField.OUTPUT) == "let me think"

        tool = _id(CALL, StepRole.TOOL)
        assert "one row" in str(steps[tool].get(StepField.OUTPUT))
        assert str(steps[tool].get(StepField.NAME)).startswith("✔ pg_query")

        first = _id(QUESTION, StepRole.ANSWER)
        second = _id(f"{QUESTION}#1", StepRole.ANSWER)
        assert steps[first].get(StepField.OUTPUT) == "First "
        assert steps[second].get(StepField.OUTPUT) == "and then done."
        assert recorded.tokens[first] == "First "
        assert recorded.tokens[second] == "and then done."

        container = _id(QUESTION, StepRole.PROCESS)
        assert steps[tool].get(StepField.PARENT_ID) == container
        assert steps[thinking].get(StepField.PARENT_ID) == container
        assert steps[container].get(StepField.NAME) == StepText.CONTAINER.value

    @pytest.mark.anyio
    async def test_journal_address_is_remembered_from_raw_output(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        journals = RemoteJournals()
        updates = _view(runtime_stand, journals)
        updates.begin_turn(QUESTION)

        address = JournalAddress(server="boba", run="run-7", call="srv-call")
        await updates.apply(_call(CALL, "mcp__boba__bash", {"cmd": "ls"}))
        await updates.apply(
            _done(
                CALL, "ok", _result(MarkdownResult(text="ok", elapsed_ms=12), "run-7")
            )
        )

        assert await journals.address(THREAD, CALL) == address

    @pytest.mark.anyio
    async def test_failed_call_without_artifact_is_an_error_step(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        updates = _view(runtime_stand, RemoteJournals())
        updates.begin_turn(QUESTION)

        await updates.apply(_call(CALL, "nope", {}))
        content = schema.TextContentBlock(text="unknown tool")
        failed = schema.SessionToolCallUpdate(
            tool_call_id=CALL,
            status="failed",
            content=[schema.ContentToolCallContent(content=content)],
            raw_output="unknown tool",
        )
        await updates.apply(failed)

        step = recorded.last()[_id(CALL, StepRole.TOOL)]
        assert step.get(StepField.IS_ERROR) is True
        assert step.get(StepField.OUTPUT) == "unknown tool"
        assert str(step.get(StepField.NAME)).startswith("✖ nope")

    @pytest.mark.anyio
    async def test_step_is_named_without_the_server_and_drawn_by_the_card(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        """Агент называет инструмент полным именем: шаг подписан именем без
        сервера, а вид аргументов найден по схеме инструмента сервера."""
        arguments = {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "contentMediaType": "application/sql"}
            },
        }
        media = ToolMedia()
        media.shown("boba", [mt.Tool(name="pg_query", input_schema=arguments)], None)
        view = ChatView(THREAD, LiveSink(), runtime_stand.journals, user_name="alice")
        updates = UpdateView(view, THREAD, RemoteJournals(), media)
        updates.begin_turn(QUESTION)

        await updates.apply(_call(CALL, "mcp__boba__pg_query", {"sql": "select 1"}))

        step = recorded.last()[_id(CALL, StepRole.TOOL)]
        assert str(step.get(StepField.NAME)).endswith("pg_query")
        assert "mcp__" not in str(step.get(StepField.NAME))
        assert str(step.get("input")).startswith("```sql\nselect 1\n```")

    @pytest.mark.anyio
    async def test_progress_text_is_shown_in_the_running_step(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        """Ход вызова агент шлёт текстом `content` идущего вызова."""
        updates = _view(runtime_stand, RemoteJournals())
        updates.begin_turn(QUESTION)

        await updates.apply(_call(CALL, "pg_copy", {}))
        text = schema.TextContentBlock(text="rows copied — 50 of 200")
        progress = schema.SessionToolCallUpdate(
            tool_call_id=CALL,
            status="in_progress",
            content=[schema.ContentToolCallContent(content=text)],
        )
        await updates.apply(progress)

        step = recorded.last()[_id(CALL, StepRole.TOOL)]
        assert "rows copied — 50 of 200" in str(step.get(StepField.OUTPUT))


class TestNestedCallSteps:
    """Вызов инструмента, который сервер объявил несущим вложенные вызовы,
    рисуется подшагами узлов: имя — инструмент и подпись узла, ход — по
    ключу узла в `_meta`, итог — по записи узла в structuredContent."""

    NODES: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "src",
            "tool": "pg_stream_out",
            "args": {"intent": "copy orders out", "sql": "select 1", "out": "c"},
        },
        {
            "key": "sink",
            "tool": "ch_stream_in",
            "args": {"intent": "load orders", "feed": "c"},
        },
    ]

    def _updates(
        self, runtime_stand: StandRefs, journals: RemoteJournals, nested: bool
    ) -> UpdateView:
        media = ToolMedia()
        declared = None
        if nested:
            declared = CallDag().feature()

        media.shown("boba", [], declared)
        view = ChatView(THREAD, LiveSink(), runtime_stand.journals, user_name="alice")

        return UpdateView(view, THREAD, journals, media)

    @staticmethod
    def _workflow() -> WorkflowResult:
        sent = MarkdownResult(text="sent 1 row into c", elapsed_ms=5)
        refused = ErrorResult(message="table orders does not exist", error_kind="x")
        nodes = [
            WorkflowNodeResult(
                key="src",
                call_id="srv-call_0",
                tool="pg_stream_out",
                errored=False,
                content=sent.llm_view(),
                result=sent,
            ),
            WorkflowNodeResult(
                key="sink",
                call_id="srv-call_1",
                tool="ch_stream_in",
                errored=False,
                content=refused.llm_view(),
                result=refused,
            ),
        ]

        return WorkflowResult(nodes=nodes, ok=False)

    @staticmethod
    def _failed(call_id: str, raw_output: Any) -> SessionUpdate:
        return schema.SessionToolCallUpdate(
            tool_call_id=call_id, status="failed", raw_output=raw_output
        )

    @pytest.mark.anyio
    async def test_nodes_are_steps_named_by_their_tool_and_intent(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        updates = self._updates(runtime_stand, RemoteJournals(), True)
        updates.begin_turn(QUESTION)

        await updates.apply(_call(CALL, "mcp__boba__workflow", {"nodes": self.NODES}))

        steps = recorded.last()
        assert _id(CALL, StepRole.TOOL) not in steps
        source = steps[_id(f"{CALL}_0", StepRole.TOOL)]
        sink = steps[_id(f"{CALL}_1", StepRole.TOOL)]
        assert " pg_stream_out · copy orders out" in str(source.get(StepField.NAME))
        assert " ch_stream_in · load orders" in str(sink.get(StepField.NAME))
        assert "select 1" in str(source.get("input"))

    @pytest.mark.anyio
    async def test_progress_goes_to_the_node_named_in_meta(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        updates = self._updates(runtime_stand, RemoteJournals(), True)
        updates.begin_turn(QUESTION)
        await updates.apply(_call(CALL, "mcp__boba__workflow", {"nodes": self.NODES}))

        text = schema.TextContentBlock(text="connecting to clickhouse")
        progress = schema.SessionToolCallUpdate(
            tool_call_id=CALL,
            status="in_progress",
            content=[schema.ContentToolCallContent(content=text)],
            field_meta={CallDag.PROGRESS_NODE: "sink"},
        )
        await updates.apply(progress)

        steps = recorded.last()
        source = steps[_id(f"{CALL}_0", StepRole.TOOL)]
        sink = steps[_id(f"{CALL}_1", StepRole.TOOL)]
        assert "connecting to clickhouse" in str(sink.get(StepField.OUTPUT))
        assert "connecting to clickhouse" not in str(source.get(StepField.OUTPUT))

    @pytest.mark.anyio
    async def test_each_node_ends_with_its_own_result_and_journal(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        journals = RemoteJournals()
        updates = self._updates(runtime_stand, journals, True)
        updates.begin_turn(QUESTION)
        await updates.apply(_call(CALL, "mcp__boba__workflow", {"nodes": self.NODES}))

        await updates.apply(self._failed(CALL, _result(self._workflow(), "run-7")))

        steps = recorded.last()
        source = steps[_id(f"{CALL}_0", StepRole.TOOL)]
        sink = steps[_id(f"{CALL}_1", StepRole.TOOL)]
        assert str(source.get(StepField.NAME)).startswith("✔")
        assert "sent 1 row into c" in str(source.get(StepField.OUTPUT))
        assert str(sink.get(StepField.NAME)).startswith("✖")
        assert "table orders does not exist" in str(sink.get(StepField.OUTPUT))

        address = await journals.address(THREAD, f"{CALL}_1")
        assert address == JournalAddress(server="boba", run="run-7", call="srv-call_1")

    @pytest.mark.anyio
    async def test_refusal_before_the_start_ends_every_node(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        updates = self._updates(runtime_stand, RemoteJournals(), True)
        updates.begin_turn(QUESTION)
        await updates.apply(_call(CALL, "mcp__boba__workflow", {"nodes": self.NODES}))

        refusal = ErrorResult(
            message="stream plan: channel c has no readers", error_kind="x"
        )
        await updates.apply(self._failed(CALL, _result(refusal)))

        steps = recorded.last()
        for index in (0, 1):
            node = steps[_id(f"{CALL}_{index}", StepRole.TOOL)]
            assert str(node.get(StepField.NAME)).startswith("✖")
            assert "has no readers" in str(node.get(StepField.OUTPUT))

    @pytest.mark.anyio
    async def test_without_the_extension_the_call_is_one_step_of_its_blocks(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        updates = self._updates(runtime_stand, RemoteJournals(), False)
        updates.begin_turn(QUESTION)
        await updates.apply(_call(CALL, "mcp__boba__workflow", {"nodes": self.NODES}))

        workflow = self._workflow()
        await updates.apply(_done(CALL, workflow.llm_view(), _result(workflow)))

        steps = recorded.last()
        assert _id(f"{CALL}_0", StepRole.TOOL) not in steps
        step = steps[_id(CALL, StepRole.TOOL)]
        assert "[sink] ch_stream_in — failed" in str(step.get(StepField.OUTPUT))


class TestLiveEqualsReplay:
    """Живой ход через шину (куски) и воспроизведение (сообщения целиком)
    дают одну и ту же ленту."""

    @pytest.mark.anyio
    async def test_bus_rendering_matches_the_replay(
        self, runtime_stand: StandRefs
    ) -> None:
        pulse = _id(QUESTION, StepRole.PULSE)

        live_context = FakeContext(THREAD)
        token = context_var.set(cast("ChainlitContext", live_context))
        try:
            bus = MemoryMessageBus("test-chainlit")
            payloads = MemoryPayloadStore()
            scope = Scope.chat(THREAD)
            renderer = ChatRenderer(
                THREAD,
                _view(runtime_stand, RemoteJournals()),
                payloads,
                NoSurface(),
                ThreadAgentsIndex(),
            )
            leave = bus.subscribe(scope, renderer.apply)
            feed = TurnFeed(bus, payloads, scope, QUESTION, LockToken.local())
            try:
                await feed.started(QUESTION, QuestionBody(text="hello", author="alice"))
                for update in _turn_updates():
                    await feed.agent_update(update)

                await feed.finished(TurnOutcome.OK, "")
            finally:
                leave()
        finally:
            context_var.reset(token)

        replay_context = FakeContext(THREAD)
        token = context_var.set(cast("ChainlitContext", replay_context))
        try:
            replay = _view(runtime_stand, RemoteJournals())
            await replay.apply(_question())
            for update in _replayed_updates():
                await replay.apply(update)

            await replay.finish_turn()
        finally:
            context_var.reset(token)

        live = live_context.emitter.outline(pulse)
        assert live == replay_context.emitter.outline(pulse)
        assert len(live) >= 6
        assert live_context.emitter.tokens == replay_context.emitter.tokens

    @pytest.mark.anyio
    async def test_large_call_result_reaches_the_tabs_by_reference(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        """Итог вызова больше предела сообщения шины уходит телом в
        PayloadStore: вкладки получают его целиком."""
        rows = "row " * 5000
        bus = MemoryMessageBus("test-chainlit")
        payloads = MemoryPayloadStore()
        scope = Scope.chat(THREAD)
        renderer = ChatRenderer(
            THREAD,
            _view(runtime_stand, RemoteJournals()),
            payloads,
            NoSurface(),
            ThreadAgentsIndex(),
        )
        leave = bus.subscribe(scope, renderer.apply)
        feed = TurnFeed(bus, payloads, scope, QUESTION, LockToken.local())
        result = _result(MarkdownResult(text=rows, elapsed_ms=12))
        try:
            await feed.started(QUESTION, QuestionBody(text="hello", author="alice"))
            await feed.agent_update(_call(CALL, "mcp__boba__pg_query", {"sql": "1"}))
            await feed.agent_update(_done(CALL, rows, result))
            await feed.finished(TurnOutcome.OK, "")
        finally:
            leave()

        step = recorded.last()[_id(CALL, StepRole.TOOL)]
        assert str(step.get(StepField.NAME)).startswith("✔ pg_query"), step
        assert rows.strip() in str(step.get(StepField.OUTPUT))


class HolderSurface(NoSurface):
    """Поверхность держателя хода в тесте: контекст chainlit HTTP-сессии
    пользователя треда, в котором открывается панель и пишутся элементы."""

    def __init__(self, context: ChainlitContext) -> None:
        self._context = context

    def context(self) -> ChainlitContext | None:
        return self._context


class TestResultMounts:
    """Побочные действия итогов вызовов живого хода у держателя: агент
    называет инструмент только в первом уведомлении вызова, итог приходит
    без имени и заголовка, а переводчик воспроизведения живых уведомлений не
    видит."""

    @pytest.mark.anyio
    async def test_live_results_open_the_panel_and_attach_the_file(
        self, runtime_stand: StandRefs, seeded: Seed, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chainlit_data, "_data_layer", seeded.layer)
        monkeypatch.setattr(chainlit_data, "_data_layer_initialized", True)
        thread_id = seeded.thread_id
        chart = f"/workspace/{thread_id}/upload/chart.png"
        report = f"/workspace/{thread_id}/upload/report.csv"
        shown = CanvasResult(path=chart, label="chart.png")
        attached = FileResult(path=report, name="report.csv", mime="text/csv")
        updates = [
            _call(CALL, "mcp__boba__canvas_open", {"path": chart}),
            _call(SECOND_CALL, "mcp__boba__file_attach", {"path": report}),
            schema.SessionToolCallUpdate(tool_call_id=CALL, status="in_progress"),
            _done(CALL, "shown", _result(shown)),
            _done(SECOND_CALL, "attached", _result(attached)),
        ]
        view = ChatView(thread_id, LiveSink(), runtime_stand.journals, user_name="u")
        replay = UpdateView(view, thread_id, RemoteJournals(), ToolMedia())
        bus = MemoryMessageBus("test-chainlit")
        scope = Scope.chat(thread_id)
        feed = TurnFeed(bus, MemoryPayloadStore(), scope, QUESTION, LockToken.local())
        CanvasRegistry.register(ImageViewer())
        context = init_http_context(user=seeded.user, thread_id=thread_id)
        mounts = ResultMounts(thread_id, seeded.user.id, HolderSurface(context), replay)
        try:
            for update in updates:
                await mounts.mounted(update, QUESTION, feed)
        finally:
            init_http_context()
            CanvasRegistry.reset()

        panel_id = ChatView.derive_id(thread_id, CALL, StepRole.ELEMENT)
        file_id = ChatView.derive_id(thread_id, SECOND_CALL, StepRole.ELEMENT)
        assert panel_id is not None
        assert file_id is not None
        panel = await seeded.layer.get_element(thread_id, panel_id)
        file = await seeded.layer.get_element(thread_id, file_id)
        assert panel is not None, "the canvas link of the live call is not stored"
        assert panel.get("props") == {"path": chart, "label": "chart.png"}, panel
        assert file is not None, "the attachment of the live call is not stored"
        assert file.get("name") == "report.csv", file


class TestCodecAndResults:
    def test_codec_round_trip_keeps_the_name_and_order(self) -> None:
        codec = SessionUpdateCodec()
        start = _call(CALL, "mcp__boba__pg_query", {"b": 1, "a": 2})

        body = codec.encode(start)
        assert body["name"] == "mcp__boba__pg_query"
        assert list(body["rawInput"]) == ["b", "a"]
        assert codec.decode(body) == start

        failed = IdleErrorUpdate(
            stop_reason="error", error=schema.Error(code=-32603, message="model down")
        )
        decoded = codec.decode(codec.encode(failed))
        assert isinstance(decoded, IdleErrorUpdate), decoded
        assert decoded.error == failed.error


def _profile(settings: list[str]) -> ChatProfileConfig:
    return ChatProfileConfig.model_validate(
        {
            "display_name": "General",
            "description": "stand profile",
            "provider": {
                "kind": "openai",
                "transport": {},
                "connection": {
                    "host": "llm.example",
                    "path": "/v1",
                    "auth": {"method": "bearer", "token": "token"},
                },
            },
            "model": "m",
            "settings": settings,
            "system_prompt": "be brief",
        }
    )


def _options() -> SessionOptions:
    options = SessionOptions()
    options.replace(
        [
            schema.SelectSessionConfigOption(
                config_id="mode",
                name="Mode",
                category="mode",
                current_value="bypassPermissions",
                options=[
                    schema.SessionConfigSelectOption(value="default", name="Default"),
                    schema.SessionConfigSelectOption(
                        value="bypassPermissions", name="Bypass"
                    ),
                ],
            ),
            schema.SelectSessionConfigOption(
                config_id="model",
                name="Model",
                category="model",
                current_value="general",
                options=[
                    schema.SessionConfigSelectOption(value="general", name="general")
                ],
            ),
        ]
    )

    return options


def _chat(mode: PermissionModeName) -> ChatAgentSettings:
    return ChatAgentSettings(
        model_name="general",
        model=AgentModel(
            provider=AgentProvider(
                kind=ProviderKindName.OPENAI, base_url="http://llm.example/v1"
            ),
            model="m",
            sampling={},
            context_window=32000,
            max_output_tokens=4096,
        ),
        system_prompt="be brief",
        mode=mode,
    )


class TestAgentPanel:
    """Вкладки про агента: слой пользователя и выбор сессии из слоя чата."""

    def test_tabs_show_user_layer_and_session_options(self) -> None:
        user = UserAgentSettings(base_url="http://llm:1/v1", api_key=SecretStr("k"))
        panel = AgentPanel(
            _profile(["*"]), user, None, _options(), PanelText("", "en-US")
        )

        tabs = {tab.id: tab for tab in panel.tabs()}
        assert set(tabs) == {"agent_tab", "session_tab"}

        agent = {widget.id: widget for widget in tabs["agent_tab"].inputs}
        assert agent[UserSetting.AGENT_BASE_URL.value].initial == "http://llm:1/v1"
        assert agent[UserSetting.AGENT_API_KEY.value].initial == ""

        session = {widget.id: widget for widget in tabs["session_tab"].inputs}
        assert set(session) == {"session_mode", "session_model"}
        assert session["session_mode"].initial == "bypassPermissions"
        # подпись варианта → значение, которое вернёт форма
        assert session["session_mode"].to_dict()["items"] == [
            {"label": "Default", "value": "default"},
            {"label": "Bypass", "value": "bypassPermissions"},
        ]

    def test_session_values_come_from_the_chat_layer(self) -> None:
        """Агент ещё работает с прежним режимом, слой чата уже сохранён:
        панель показывает сохранённое."""
        panel = AgentPanel(
            _profile(["*"]),
            UserAgentSettings(),
            _chat(PermissionModeName.DEFAULT),
            _options(),
            PanelText("", "en-US"),
        )

        tabs = {tab.id: tab for tab in panel.tabs()}
        session = {widget.id: widget for widget in tabs["session_tab"].inputs}
        assert session["session_mode"].initial == "default"
        assert session["session_model"].initial == "general"

    def test_closed_profile_hides_the_user_layer(self) -> None:
        panel = AgentPanel(
            _profile([]),
            UserAgentSettings(),
            None,
            SessionOptions(),
            PanelText("", "en-US"),
        )

        assert panel.tabs() == []

    def test_retired_setting_name_is_a_config_error(self) -> None:
        with pytest.raises(ValueError, match="unknown settings"):
            _profile(["user_prompt"])

    def test_parse_keeps_the_secret_and_reads_the_session_choice(self) -> None:
        saved = UserAgentSettings(api_key=SecretStr("old"), model="m1")
        panel = AgentPanel(
            _profile(["*"]), saved, None, _options(), PanelText("", "en-US")
        )

        change = panel.parse(
            {
                UserSetting.AGENT_BASE_URL.value: " http://other/v1 ",
                UserSetting.AGENT_API_KEY.value: "",
                UserSetting.AGENT_MODEL.value: "",
                "session_mode": "default",
                "session_model": "general",
            }
        )

        assert change.user.base_url == "http://other/v1"
        assert change.user.model is None
        assert change.user.api_key is not None
        assert change.user.api_key.get_secret_value() == "old"
        assert change.mode is PermissionModeName.DEFAULT
        assert change.model_name == "general"

    def test_parse_without_session_fields_keeps_the_chat_choice(self) -> None:
        panel = AgentPanel(
            _profile(["*"]),
            UserAgentSettings(),
            None,
            SessionOptions(),
            PanelText("", "en-US"),
        )

        change = panel.parse({UserSetting.AGENT_MODEL.value: "picked"})

        assert change.user.model == "picked"
        assert change.mode is None
        assert change.model_name is None

    def test_parse_refuses_a_value_the_agent_did_not_offer(self) -> None:
        panel = AgentPanel(
            _profile(["*"]),
            UserAgentSettings(),
            None,
            _options(),
            PanelText("", "en-US"),
        )

        with pytest.raises(AgentSettingsError, match="no-such-mode"):
            panel.parse({"session_mode": "no-such-mode"})
