"""Лента из уведомлений ACP v2: один переводчик для живого хода (через шину)
и воспроизведения (`session/resume`); идентификаторы шагов из `messageId` и
`toolCallId` по правилам ChatView.derive_id; итог вызова из `rawOutput` —
ответа сервера MCP (артефакт, адрес журнала); панель про агента.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, cast
from urllib.parse import urlsplit

import chainlit.data as chainlit_data
import mcp_types as mt
import pytest
from acp.experimental.v2 import schema
from aid_stand import Seed, get_bytes
from chainlit.context import ChainlitContext, context_var
from chainlit.step import StepDict
from pydantic import AnyUrl, SecretStr

from boba.agent.records import PermissionMode
from boba.agent.settings import ModelSettings, ProviderKind, ProviderSettings
from boba.aid.agent.chat import SessionOptions, ThreadAgentsIndex
from boba.aid.agent.panel import AgentPanel
from boba.aid.agent.settings import (
    AgentSettingsError,
    ChatAgentSettings,
    UserAgentSettings,
)
from boba.aid.canvas.diagram import DiagramFiles, MermaidViewer
from boba.aid.canvas.panel import ImageViewer
from boba.aid.canvas.remote import RemoteJournals
from boba.aid.chat.feed import QuestionBody, TurnFeed
from boba.aid.chat.panel_text import PanelText
from boba.aid.domain.fields import StepField
from boba.aid.domain.keys import CanvasFileUrl
from boba.aid.rendering.acp_view import (
    SessionUpdate,
    SessionUpdateCodec,
    ToolMedia,
    UpdateView,
)
from boba.aid.rendering.chat_view import (
    ChatView,
    LiveSink,
    StepKind,
    StepRole,
    StepText,
)
from boba.aid.rendering.renderer import ChatRenderer, NoSurface
from boba.canvas.canvas import CanvasAction, CanvasElement, CanvasRegistry
from boba.canvas.keys import ObjectKey, ThreadDir
from boba.chat.profiles import ChatProfileConfig, UserSetting
from boba.identity.context import Scope
from boba.mcp_adapter import JournalUris
from boba.mcp_client.client import JournalLinks, McpServerApps
from boba.messaging import (
    JournalOpened,
    LockToken,
    MemoryMessageBus,
    MemoryPayloadStore,
    TurnOutcome,
)
from boba.runtime.storage import LocalStorageClient
from boba.stand.refs import StandRefs
from boba.stand.storage import StorageSeed
from boba.toolkit.channels import ToolChannel
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
    ContentMime,
    ErrorResult,
    FileResult,
    MarkdownResult,
    ToolArtifact,
    ToolResultBase,
)
from boba.toolkit.wire import JournalFeature, JournalRead, McpContent, ResultWire
from boba.transport.acp.protocol import IdleErrorUpdate

THREAD = "11111111-1111-1111-1111-111111111111"
QUESTION = "22222222-2222-2222-2222-222222222222"
MESSAGE = "msg_a"
SECOND_MESSAGE = "msg_b"
CALL = "call_1"
SECOND_CALL = "call_2"
RUN = "run-7"
JOURNAL = JournalFeature(
    path="/journals",
    resource="boba-journal://{run}/{node}/{channel}{?offset,before}",
)
"""Журнал вызовов сервера boba стенда ленты."""


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры conftest: БД этим тестам не нужна."""


class FakeSession:
    """Сессия для Message и Step: им нужны только идентификаторы."""

    def __init__(self, thread_id: str) -> None:
        self.id = "session-1"
        self.thread_id = thread_id

    async def persist_file(
        self,
        name: str,
        mime: str,
        path: str | None = None,
        content: bytes | str | None = None,
    ) -> dict[str, str]:
        """Файл сессии кастом-элемента: в тесте хранить его негде и незачем."""
        del mime, path, content

        return {"id": f"session-file-{name}"}


class FakeEmitter:
    """Эмиттер-рекордер: кадры ленты копятся вместо отправки в сокет; по ним
    видно и шаги sink'а, и стримящиеся сообщения, которые идут мимо sink'а."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, StepDict]] = []
        self.tokens: dict[str, str] = {}
        self.elements: list[dict[str, Any]] = []

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
        self.elements.append(dict(element_dict))

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


def _result(artifact: ToolResultBase, journaled: Sequence[str] = ()) -> dict[str, Any]:
    """Ответ tools/call сервера boba-mcp: блоки результата и ссылки на
    stdout журналов вызовов journaled запуска RUN, как их дописывает
    сервер."""
    call = DagNode(key="srv-call", tool="pg_query")
    outcome = NodeOutcomes().of(call, artifact, False)
    wire = ResultWire().packed(outcome)
    uris = JournalUris(JOURNAL.resource)
    links = McpContent(RUN)
    for node in journaled:
        address = JournalRead(run=RUN, node=node, channel=ToolChannel.STDOUT)
        links.link(
            uris.uri(address), ToolChannel.STDOUT.value, ContentMime.PLAIN, False
        )

    content = [*wire.content, *links.blocks()]

    reply = mt.CallToolResult(
        content=content,
        structured_content=wire.structured,
        is_error=wire.is_error,
        _meta=dict(wire.meta),
    )

    return reply.model_dump(mode="json", by_alias=True, exclude_none=True)


def _journal_opened(call_id: str, call: str, node: str | None) -> JournalOpened:
    """Сообщение ретранслятора: сервер boba открыл журнал call вызова
    call_id агента (у связки — журнал узла node), едва журнал открылся."""
    return JournalOpened(call_id=call_id, node=node, server="boba", run=RUN, call=call)


def _buttons(recorded: FakeEmitter) -> list[tuple[str, str]]:
    """Вызов и шаг каждой кнопки журнала в ленте: кнопка с тем же id —
    одна, сколько бы раз шаг её ни переотправил."""
    found: dict[str, tuple[str, str]] = {}
    for element in recorded.elements:
        if element.get("name") != CanvasElement.STREAM:
            continue

        props = element["props"]
        found[element["id"]] = (props[CanvasAction.CALL_ID], element["forId"])

    return list(found.values())


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


def _view(
    runtime_stand: StandRefs, storage: LocalStorageClient, journals: RemoteJournals
) -> UpdateView:
    view = ChatView(THREAD, LiveSink(), "u-1", storage, user_name="alice")
    media = ToolMedia()
    media.shown("boba", [], None, JournalLinks(JOURNAL.resource), None)

    return UpdateView(view, THREAD, journals, media)


class TestUpdateViewIds:
    """Шаги ленты получают прежние детерминированные id."""

    @pytest.mark.anyio
    async def test_replay_derives_ids_from_message_and_call_ids(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        updates = _view(runtime_stand, storage, RemoteJournals())
        await updates.apply(_question())
        for update in _turn_updates():
            await updates.apply(update)

        await updates.finish_turn()

        steps = recorded.last()
        assert steps[QUESTION].get(StepField.TYPE) == StepKind.USER.value
        assert steps[QUESTION].get(StepField.NAME) == "alice"

        thinking = _id(MESSAGE, StepRole.THINKING)
        assert steps[thinking].get(StepField.OUTPUT) == "let me think"
        assert steps[thinking].get(StepField.NAME) == "✔ thinking"

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
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        journals = RemoteJournals()
        updates = _view(runtime_stand, storage, journals)
        updates.begin_turn(QUESTION)

        address = JournalAddress(server="boba", run=RUN, call="srv-call")
        await updates.apply(_call(CALL, "mcp__boba__bash", {"cmd": "ls"}))
        await updates.apply(
            _done(
                CALL,
                "ok",
                _result(MarkdownResult(text="ok", elapsed_ms=12), ["srv-call"]),
            )
        )

        assert await journals.address(THREAD, CALL) == address
        step = recorded.last()[_id(CALL, StepRole.TOOL)]
        assert step.get(StepField.OUTPUT) == "ok", "a journal link is not drawn"

    @pytest.mark.anyio
    async def test_opened_journal_gives_the_running_step_its_button(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        """Ретранслятор называет журнал, едва сервер его открыл (JournalOpened):
        адрес известен и кнопка журнала стоит на шаге, пока вызов идёт."""
        journals = RemoteJournals()
        updates = _view(runtime_stand, storage, journals)
        updates.begin_turn(QUESTION)

        await updates.apply(_call(CALL, "mcp__boba__bash", {"cmd": "ls"}))
        await updates.journal_opened(_journal_opened(CALL, "srv-call", None))

        address = JournalAddress(server="boba", run=RUN, call="srv-call")
        assert await journals.address(THREAD, CALL) == address
        step = recorded.last()[_id(CALL, StepRole.TOOL)]
        assert step.get(StepField.OUTPUT) == StepText.RUNNING.value, step
        buttons = _buttons(recorded)
        assert buttons == [(CALL, _id(CALL, StepRole.TOOL))], buttons

        await updates.apply(
            _done(
                CALL,
                "ok",
                _result(MarkdownResult(text="ok", elapsed_ms=12), ["srv-call"]),
            )
        )

        assert _buttons(recorded) == buttons, "the result does not add a second one"

    @pytest.mark.anyio
    async def test_failed_call_without_artifact_is_an_error_step(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        updates = _view(runtime_stand, storage, RemoteJournals())
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
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
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
        media.shown(
            "boba", [mt.Tool(name="pg_query", input_schema=arguments)], None, None, None
        )
        view = ChatView(
            THREAD,
            LiveSink(),
            "u-1",
            storage,
            user_name="alice",
        )
        updates = UpdateView(view, THREAD, RemoteJournals(), media)
        updates.begin_turn(QUESTION)

        await updates.apply(_call(CALL, "mcp__boba__pg_query", {"sql": "select 1"}))

        step = recorded.last()[_id(CALL, StepRole.TOOL)]
        assert str(step.get(StepField.NAME)).endswith("pg_query")
        assert "mcp__" not in str(step.get(StepField.NAME))
        assert str(step.get("input")).startswith("```sql\nselect 1\n```")

    @pytest.mark.anyio
    async def test_progress_text_is_shown_in_the_running_step(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        """Ход вызова агент шлёт текстом `content` идущего вызова."""
        updates = _view(runtime_stand, storage, RemoteJournals())
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


class TestToolApps:
    """Инструмент с приложением MCP Apps у сервера с секцией apps: шаг
    вызова получает элемент приложения с аргументами и итогом под
    детерминированным id, блоки итога показываются рядом; у сервера без
    секции — только блоки."""

    BOARD: ClassVar[mt.Tool] = mt.Tool.model_validate(
        {
            "name": "board",
            "inputSchema": {"type": "object"},
            "_meta": {"ui": {"resourceUri": "ui://standard/board.html"}},
        }
    )
    SANDBOX: ClassVar[McpServerApps] = McpServerApps.model_validate(
        {"sandbox_url": "http://localhost:8123/mcp-apps/sandbox.html"}
    )
    REPLY: ClassVar[dict[str, Any]] = {
        "content": [{"type": "text", "text": "drawn"}],
        "isError": False,
        "resultType": "complete",
    }

    async def _drawn(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        apps: McpServerApps | None,
    ) -> None:
        media = ToolMedia()
        media.shown("standard", [self.BOARD], None, None, apps)
        view = ChatView(THREAD, LiveSink(), "u-1", storage, user_name="a")
        updates = UpdateView(view, THREAD, RemoteJournals(), media)
        updates.begin_turn(QUESTION)

        await updates.apply(_call(CALL, "mcp__standard__board", {"markup": "<b/>"}))
        await updates.apply(_done(CALL, "drawn", self.REPLY))

    @pytest.mark.anyio
    async def test_app_is_an_element_of_the_step_with_the_call_and_its_result(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        await self._drawn(runtime_stand, storage, self.SANDBOX)

        apps = [e for e in recorded.elements if e.get("name") == "ToolApp"]
        step = recorded.last()[_id(CALL, StepRole.TOOL)]

        assert len(apps) == 1, recorded.elements
        assert apps[0]["id"] == _id(CALL, StepRole.APP)
        assert apps[0]["props"] == {
            "server": "standard",
            "tool": "board",
            "resource_uri": "ui://standard/board.html",
            "sandbox_url": "http://localhost:8123/mcp-apps/sandbox.html",
            "call_id": CALL,
            "input": {"markup": "<b/>"},
            "result": self.REPLY,
        }
        assert step.get(StepField.OUTPUT) == "drawn"

    @pytest.mark.anyio
    async def test_server_without_apps_draws_only_the_blocks(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        await self._drawn(runtime_stand, storage, None)

        step = recorded.last()[_id(CALL, StepRole.TOOL)]

        assert not [e for e in recorded.elements if e.get("name") == "ToolApp"]
        assert step.get(StepField.OUTPUT) == "drawn"


class TestQuestionRecords:
    """Вопрос агента пользователю в ленте: вызов AskUserQuestion — своё
    сообщение основной ленты (не шаг контейнера хода) с элементом Question:
    пока ждёт — висящий вопрос по вводу, после итога — запись ответа тем же
    элементом; вопросы сервера MCP в вызове — такими же сообщениями по
    раундам из `_meta` результата. Живой ход и воспроизведение рисуют их
    одним кодом (UpdateView)."""

    ASKED: ClassVar[dict[str, Any]] = {
        "questions": [
            {
                "question": "Which checks should run?",
                "header": "Checks",
                "multiSelect": True,
                "options": [
                    {"label": "Unit tests", "description": "Fast."},
                    {"label": "UI tests", "description": "Slow."},
                ],
            }
        ]
    }
    ITEM: ClassVar[dict[str, Any]] = {
        "key": "Which checks should run?",
        "header": "Checks",
        "text": "Which checks should run?",
        "kind": "multi",
        "options": [
            {
                "value": "Unit tests",
                "label": "Unit tests",
                "description": "Fast.",
                "preview": "",
            },
            {
                "value": "UI tests",
                "label": "UI tests",
                "description": "Slow.",
                "preview": "",
            },
        ],
        "other": "",
        "required": True,
        "minimum": None,
        "maximum": None,
    }

    def _updates(
        self, storage: LocalStorageClient, media: ToolMedia | None = None
    ) -> UpdateView:
        if media is None:
            media = ToolMedia()

        view = ChatView(THREAD, LiveSink(), "u-1", storage, user_name="a")
        updates = UpdateView(view, THREAD, RemoteJournals(), media)
        updates.begin_turn(QUESTION)

        return updates

    def _questions(self, recorded: FakeEmitter) -> list[dict[str, Any]]:
        return [e for e in recorded.elements if e.get("name") == "Question"]

    @pytest.mark.anyio
    async def test_answered_question_is_a_feed_message_with_its_record(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        updates = self._updates(storage)
        output = {
            "questions": self.ASKED["questions"],
            "answers": {"Which checks should run?": "UI tests, smoke"},
            "picks": {
                "Which checks should run?": {"labels": ["UI tests"], "other": "smoke"}
            },
            "outcome": "accept",
        }

        await updates.apply(_call(CALL, "AskUserQuestion", self.ASKED))
        pending = recorded.last()[_id(CALL, StepRole.QUESTION)]
        await updates.apply(_done(CALL, "answered", output))
        message = recorded.last()[_id(CALL, StepRole.QUESTION)]

        assert _id(CALL, StepRole.TOOL) not in recorded.last()
        for step in (pending, message):
            assert step.get(StepField.TYPE) == "assistant_message"
            assert step.get(StepField.PARENT_ID) is None
            assert step.get(StepField.OUTPUT) == ""
            assert step.get(StepField.IS_ERROR) is False

        asking, record = self._questions(recorded)
        assert asking["id"] == _id(CALL, StepRole.QUESTION_CARD)
        assert asking["forId"] == _id(CALL, StepRole.QUESTION)
        assert asking["display"] == "inline"
        assert asking["props"] == {
            "state": "pending",
            "items": [self.ITEM],
            "source": None,
            "answers": {},
            "failed": False,
        }
        assert record["id"] == asking["id"]
        assert record["props"] == {
            "state": "accept",
            "items": [self.ITEM],
            "source": None,
            "answers": {
                "Which checks should run?": {"picked": ["UI tests"], "typed": "smoke"}
            },
            "failed": False,
        }

    @pytest.mark.anyio
    async def test_declined_and_interrupted_questions(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        """Отказ — запись decline без ответов, вызов неудачен; вызов,
        оборванный концом хода, — запись без ответа, тоже неудачен."""
        updates = self._updates(storage)
        declined = {"questions": self.ASKED["questions"], "outcome": "decline"}
        failed = schema.SessionToolCallUpdate(
            tool_call_id=CALL,
            status="failed",
            content=[
                schema.ContentToolCallContent(
                    content=schema.TextContentBlock(text="rejected")
                )
            ],
            raw_output=declined,
        )
        await updates.apply(_call(CALL, "AskUserQuestion", self.ASKED))
        await updates.apply(failed)
        await updates.apply(_call(SECOND_CALL, "AskUserQuestion", self.ASKED))
        await updates.finish_turn()

        assert _id(CALL, StepRole.QUESTION) in recorded.last()
        assert _id(SECOND_CALL, StepRole.QUESTION) in recorded.last()
        outcomes: list[tuple[str, bool]] = []
        for element in self._questions(recorded):
            props = element["props"]
            outcomes.append((props["state"], props["failed"]))
            assert props["answers"] == {}

        assert outcomes == [
            ("pending", False),
            ("decline", True),
            ("pending", False),
            ("cancel", True),
        ]

    @pytest.mark.anyio
    async def test_server_questions_are_feed_messages_after_the_call(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        """Раунд вопроса сервера MCP в `_meta` результата — сообщение ленты
        с записью после итога вызова, источник — сервер и его сообщение."""
        updates = self._updates(storage)
        requested = {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "title": "Target",
                    "description": "Which table should receive the rows?",
                    "oneOf": [
                        {"const": "sales.orders", "title": "Orders"},
                        {"const": "sales.stage", "title": "Stage"},
                    ],
                },
                "rows": {"type": "integer", "title": "Rows", "minimum": 1},
            },
            "required": ["table"],
        }
        reply = {
            "content": [{"type": "text", "text": "copied"}],
            "isError": False,
            "_meta": {
                "boba/elicitations": [
                    {
                        "server": "boba",
                        "tool": "pg_copy_in",
                        "message": "pg_copy_in needs the target table",
                        "requested": requested,
                        "action": "accept",
                        "content": {"table": "sales.stage", "rows": 10},
                    }
                ]
            },
        }

        await updates.apply(_call(CALL, "mcp__boba__pg_copy_in", {"sql": "x"}))
        await updates.apply(_done(CALL, "copied", reply))

        step = recorded.last()[_id(CALL, StepRole.TOOL)]
        assert step.get(StepField.OUTPUT) == "copied"
        message = recorded.last()[_id(f"{CALL}#0", StepRole.QUESTION)]
        assert message.get(StepField.PARENT_ID) is None
        (element,) = self._questions(recorded)
        assert element["id"] == _id(f"{CALL}#0", StepRole.QUESTION_CARD)
        assert element["forId"] == _id(f"{CALL}#0", StepRole.QUESTION)
        props = element["props"]
        assert props["state"] == "accept"
        assert props["failed"] is False
        assert props["source"] == {
            "server": "boba",
            "message": "pg_copy_in needs the target table",
        }
        kinds: list[tuple[str, str, bool]] = []
        for item in props["items"]:
            kinds.append((item["key"], item["kind"], item["required"]))

        assert kinds == [("table", "choice", True), ("rows", "integer", False)]
        assert props["items"][0]["options"][1]["label"] == "Stage"
        assert props["answers"] == {
            "table": {"picked": ["sales.stage"], "typed": None},
            "rows": {"picked": [], "typed": "10"},
        }


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
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        journals: RemoteJournals,
        nested: bool,
    ) -> UpdateView:
        media = ToolMedia()
        declared = None
        if nested:
            declared = CallDag().feature()

        media.shown("boba", [], declared, JournalLinks(JOURNAL.resource), None)
        view = ChatView(
            THREAD,
            LiveSink(),
            "u-1",
            storage,
            user_name="alice",
        )

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
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        updates = self._updates(runtime_stand, storage, RemoteJournals(), True)
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
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        updates = self._updates(runtime_stand, storage, RemoteJournals(), True)
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
    async def test_journal_of_a_node_gives_its_step_the_button(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        journals = RemoteJournals()
        updates = self._updates(runtime_stand, storage, journals, True)
        updates.begin_turn(QUESTION)
        await updates.apply(_call(CALL, "mcp__boba__workflow", {"nodes": self.NODES}))

        opened = _journal_opened(CALL, "srv-call_1", "sink")
        await updates.journal_opened(opened)

        address = JournalAddress(server="boba", run=RUN, call="srv-call_1")
        assert await journals.address(THREAD, f"{CALL}_1") == address
        sink = _id(f"{CALL}_1", StepRole.TOOL)
        assert _buttons(recorded) == [(f"{CALL}_1", sink)]

    @pytest.mark.anyio
    async def test_each_node_ends_with_its_own_result_and_journal(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        journals = RemoteJournals()
        updates = self._updates(runtime_stand, storage, journals, True)
        updates.begin_turn(QUESTION)
        await updates.apply(_call(CALL, "mcp__boba__workflow", {"nodes": self.NODES}))

        await updates.apply(
            self._failed(CALL, _result(self._workflow(), ["srv-call_0", "srv-call_1"]))
        )

        steps = recorded.last()
        source = steps[_id(f"{CALL}_0", StepRole.TOOL)]
        sink = steps[_id(f"{CALL}_1", StepRole.TOOL)]
        assert str(source.get(StepField.NAME)).startswith("✔")
        assert "sent 1 row into c" in str(source.get(StepField.OUTPUT))
        assert str(sink.get(StepField.NAME)).startswith("✖")
        assert "table orders does not exist" in str(sink.get(StepField.OUTPUT))

        address = await journals.address(THREAD, f"{CALL}_1")
        assert address == JournalAddress(server="boba", run=RUN, call="srv-call_1")

    @pytest.mark.anyio
    async def test_refusal_before_the_start_ends_every_node(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        updates = self._updates(runtime_stand, storage, RemoteJournals(), True)
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
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        updates = self._updates(runtime_stand, storage, RemoteJournals(), False)
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
        self, runtime_stand: StandRefs, storage: LocalStorageClient
    ) -> None:
        pulse = _id(QUESTION, StepRole.PULSE)

        live_context = FakeContext(THREAD)
        token = context_var.set(cast("ChainlitContext", live_context))
        try:
            bus = MemoryMessageBus("test-aid")
            payloads = MemoryPayloadStore()
            scope = Scope.chat(THREAD)
            renderer = ChatRenderer(
                THREAD,
                _view(runtime_stand, storage, RemoteJournals()),
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
            replay = _view(runtime_stand, storage, RemoteJournals())
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
    async def test_question_attachments_are_the_same_live_and_in_the_replay(
        self, runtime_stand: StandRefs, storage: LocalStorageClient
    ) -> None:
        """Вложение вопроса — блок resource_link: живой ход (TurnStarted) и
        воспроизведение (`user_message`) дают шагу вопроса один и тот же файл
        с относительной ссылкой на маршрут canvas."""
        path = f"/boba-test/canvas/{THREAD}/upload/note.txt"
        link = schema.ResourceContentBlock(
            name="note.txt",
            uri=AnyUrl(f"http://agent-side:1{path}"),
            mime_type="text/plain",
        )

        live_context = FakeContext(THREAD)
        token = context_var.set(cast("ChainlitContext", live_context))
        try:
            bus = MemoryMessageBus("test-aid")
            payloads = MemoryPayloadStore()
            scope = Scope.chat(THREAD)
            renderer = ChatRenderer(
                THREAD,
                _view(runtime_stand, storage, RemoteJournals()),
                payloads,
                NoSurface(),
                ThreadAgentsIndex(),
            )
            leave = bus.subscribe(scope, renderer.apply)
            feed = TurnFeed(bus, payloads, scope, QUESTION, LockToken.local())
            try:
                body = QuestionBody(text="hello", author="alice", links=(link,))
                await feed.started(QUESTION, body)
                await feed.finished(TurnOutcome.OK, "")
            finally:
                leave()
        finally:
            context_var.reset(token)

        replay_context = FakeContext(THREAD)
        token = context_var.set(cast("ChainlitContext", replay_context))
        try:
            replay = _view(runtime_stand, storage, RemoteJournals())
            asked = schema.UserMessageUpdate(
                message_id=QUESTION,
                content=[schema.TextContentBlock(text="hello"), link],
            )
            await replay.apply(asked)
            await replay.finish_turn()
        finally:
            context_var.reset(token)

        def shown(context: FakeContext) -> set[tuple[str, str, str, str]]:
            found: set[tuple[str, str, str, str]] = set()
            for element in context.emitter.elements:
                found.add(
                    (
                        str(element["id"]),
                        str(element["name"]),
                        str(element["url"]),
                        str(element["forId"]),
                    )
                )

            return found

        expected = {
            (_id(f"{QUESTION}/0", StepRole.ELEMENT), "note.txt", path, QUESTION)
        }
        assert shown(live_context) == expected
        assert shown(replay_context) == expected

    @pytest.mark.anyio
    async def test_large_call_result_reaches_the_tabs_by_reference(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        """Итог вызова больше предела сообщения шины уходит телом в
        PayloadStore: вкладки получают его целиком."""
        rows = "row " * 5000
        bus = MemoryMessageBus("test-aid")
        payloads = MemoryPayloadStore()
        scope = Scope.chat(THREAD)
        renderer = ChatRenderer(
            THREAD,
            _view(runtime_stand, storage, RemoteJournals()),
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


class TestResultsFromRawOutput:
    """Файлы итогов и ссылки панели рисует лента из `rawOutput`: живой ход
    (шина) и воспроизведение дают один и тот же элемент у ответа хода, и
    так же рисуются итоги старых тредов (снапшоты истории boba-toolkit) —
    канвас снапшота показывает спеку mermaid, её ссылку карточка строит по
    тексту из хранилища."""

    SNAPSHOTS: ClassVar[Path] = (
        Path(__file__).resolve().parents[3] / "core/boba-toolkit/tests/history"
    )
    SPEC: ClassVar[str] = "graph TD; A-->B"

    def _snapshot(self, name: str, thread_id: str) -> str:
        raw = (self.SNAPSHOTS / name).read_text(encoding="utf-8")

        return raw.replace("/workspace/t/", f"/workspace/{thread_id}/")

    def _updates(self, thread_id: str) -> list[SessionUpdate]:
        attached = FileResult.model_validate_json(
            self._snapshot("file.json", thread_id)
        )
        shown = CanvasResult.model_validate_json(
            self._snapshot("canvas.json", thread_id)
        )

        return [
            _chunk("Here.", MESSAGE),
            _call(CALL, "mcp__boba__file_attach", {"path": attached.path}),
            _done(CALL, "attached", _result(attached)),
            _call(SECOND_CALL, "mcp__boba__canvas_open", {"path": shown.path}),
            _done(SECOND_CALL, "shown", _result(shown)),
        ]

    @staticmethod
    def _shown(context: FakeContext) -> list[tuple[str, str, str, str, str]]:
        found: list[tuple[str, str, str, str, str]] = []
        for element in context.emitter.elements:
            found.append(
                (
                    str(element["id"]),
                    str(element["name"]),
                    str(element.get("url")),
                    str(element.get("props")),
                    str(element["forId"]),
                )
            )

        return sorted(found)

    async def _drawn(
        self, runtime_stand: StandRefs, storage: LocalStorageClient, seeded: Seed
    ) -> tuple[FakeContext, FakeContext]:
        """Ход со снапшотами итогов вживую через шину и при воспроизведении."""
        thread_id = seeded.thread_id
        live_context = FakeContext(thread_id)
        token = context_var.set(cast("ChainlitContext", live_context))
        try:
            bus = MemoryMessageBus("test-aid")
            payloads = MemoryPayloadStore()
            scope = Scope.chat(thread_id)
            view = ChatView(thread_id, LiveSink(), seeded.user.id, storage)
            renderer = ChatRenderer(
                thread_id,
                UpdateView(view, thread_id, RemoteJournals(), ToolMedia()),
                payloads,
                NoSurface(),
                ThreadAgentsIndex(),
            )
            leave = bus.subscribe(scope, renderer.apply)
            feed = TurnFeed(bus, payloads, scope, QUESTION, LockToken.local())
            try:
                await feed.started(QUESTION, QuestionBody(text="hello"))
                for update in self._updates(thread_id):
                    await feed.agent_update(update)

                await feed.finished(TurnOutcome.OK, "")
            finally:
                leave()
        finally:
            context_var.reset(token)

        replay_context = FakeContext(thread_id)
        token = context_var.set(cast("ChainlitContext", replay_context))
        try:
            view = ChatView(thread_id, LiveSink(), seeded.user.id, storage)
            replay = UpdateView(view, thread_id, RemoteJournals(), ToolMedia())
            await replay.apply(_question())
            for update in self._updates(thread_id):
                await replay.apply(update)

            await replay.finish_turn()
        finally:
            context_var.reset(token)

        return live_context, replay_context

    @pytest.mark.anyio
    async def test_live_and_replay_draw_the_same_elements(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        seeded: Seed,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(chainlit_data, "_data_layer", seeded.layer)
        monkeypatch.setattr(chainlit_data, "_data_layer_initialized", True)
        spec = ObjectKey(
            user_id=seeded.user.id,
            thread_id=seeded.thread_id,
            dir=ThreadDir.MERMAID,
            name="a.mmd",
        )
        await StorageSeed().put(seeded.layer.storage, spec.render(), self.SPEC)
        CanvasRegistry.register(MermaidViewer(DiagramFiles()))
        try:
            live_context, replay_context = await self._drawn(
                runtime_stand, storage, seeded
            )
        finally:
            CanvasRegistry.reset()

        thread_id = seeded.thread_id
        live = self._shown(live_context)
        assert live == self._shown(replay_context)
        drawn: dict[str, tuple[str, str, str]] = {}
        for element_id, name, url, props, for_id in live:
            drawn[name] = (element_id, for_id, f"{url} {props}")

        answer = ChatView.derive_id(thread_id, QUESTION, StepRole.ANSWER)
        attached = ChatView.derive_id(thread_id, f"{CALL}/0", StepRole.ELEMENT)
        card = ChatView.derive_id(thread_id, f"{SECOND_CALL}/0", StepRole.ELEMENT)
        assert sorted(drawn) == sorted(["a.pdf", CanvasElement.VIEW])
        assert drawn["a.pdf"][:2] == (attached, answer), live
        assert f"/canvas/{thread_id}/upload/a.pdf" in drawn["a.pdf"][2], live
        assert drawn[CanvasElement.VIEW][:2] == (card, answer), live
        assert spec.in_workspace() in drawn[CanvasElement.VIEW][2], live

    @pytest.mark.anyio
    async def test_missing_spec_is_noted_the_same_live_and_in_the_replay(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        seeded: Seed,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Спеки диаграммы в хранилище нет: ссылки панели нет, в выходе шага
        вызова одно и то же пояснение вживую и при воспроизведении."""
        monkeypatch.setattr(chainlit_data, "_data_layer", seeded.layer)
        monkeypatch.setattr(chainlit_data, "_data_layer_initialized", True)
        CanvasRegistry.register(MermaidViewer(DiagramFiles()))
        try:
            live_context, replay_context = await self._drawn(
                runtime_stand, storage, seeded
            )
        finally:
            CanvasRegistry.reset()

        step_id = ChatView.derive_id(seeded.thread_id, SECOND_CALL, StepRole.TOOL)
        assert step_id is not None
        live = live_context.emitter.last()[step_id].get(StepField.OUTPUT)
        replayed = replay_context.emitter.last()[step_id].get(StepField.OUTPUT)
        assert live == replayed
        assert "a.mmd is not shown: file not found" in str(live), live
        names: list[str] = []
        for element in live_context.emitter.elements:
            names.append(str(element["name"]))

        assert names == ["a.pdf"], names


class TestElementIdsWithoutKey:
    """Id элемента выводится из ключа вопроса или вызова; без ключа элемент
    остаётся со своим id — элементы разных вопросов и вызовов не совпадают
    и фронт не заменяет один другим."""

    @pytest.mark.anyio
    async def test_questions_and_calls_without_a_key_get_distinct_elements(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        view = ChatView(THREAD, LiveSink(), "u-1", storage)
        link = schema.ResourceContentBlock(
            name="note.txt",
            uri=AnyUrl(f"http://agent-side:1/boba-test/canvas/{THREAD}/upload/n.txt"),
            mime_type="text/plain",
        )
        attached = FileResult(
            path=f"/workspace/{THREAD}/upload/a.pdf",
            name="a.pdf",
            mime="application/pdf",
        )
        await view.question("one", links=(link,))
        await view.question("two", links=(link,))
        view.begin_turn(QUESTION)
        for _ in range(2):
            step = await view.tool_started(
                "mcp__boba__send_file", {"path": attached.path}, None, None, None
            )
            reply = mt.CallToolResult.model_validate(_result(attached))
            await view.tool_finished(step, reply, None, 0)

        await view.finish_turn()

        ids: list[str] = []
        for element in recorded.elements:
            ids.append(str(element["id"]))

        assert len(ids) == 4, recorded.elements
        assert len(set(ids)) == 4, ids


class TestOldThreads:
    """Треды прежних выпусков: вопрос со ссылкой, склеенной в текст, рисуется
    текстом как есть и без элементов — вложений у него в истории нет."""

    @pytest.mark.anyio
    async def test_old_question_with_a_glued_link_is_drawn_as_text(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        text = f"look[@note.txt](/boba-test/workspace/attachment/{THREAD}/upload/e)"
        replay = _view(runtime_stand, storage, RemoteJournals())
        await replay.apply(
            schema.UserMessageUpdate(
                message_id=QUESTION, content=[schema.TextContentBlock(text=text)]
            )
        )
        await replay.finish_turn()

        assert recorded.last()[QUESTION].get(StepField.OUTPUT) == text
        assert recorded.elements == []


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


def _chat(mode: PermissionMode) -> ChatAgentSettings:
    return ChatAgentSettings(
        model_name="general",
        model=ModelSettings(
            provider=ProviderSettings(
                kind=ProviderKind.OPENAI, base_url="http://llm.example/v1"
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
            _chat(PermissionMode.DEFAULT),
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
        assert change.mode is PermissionMode.DEFAULT
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


class TestBlockDrawings:
    """Look-тесты отрисовщика: блок ответа рисуется по виду и MIME (7.3) —
    одинаково у boba-mcp и у чужого сервера; каждый вид результата boba-mcp
    (7.4) выглядит как до перехода на стандартные блоки (без ведущего
    перевода строки у таблиц — markdown его не показывает)."""

    SNAPSHOTS: ClassVar[Path] = (
        Path(__file__).resolve().parents[3] / "core/boba-toolkit/tests/history"
    )

    LOOKS: ClassVar[dict[str, str]] = {
        "error": ("**boom**"),
        "exception": (
            "**ClickHouseQueryError: clickhouse statement failed: Code: 36. "
            "DB::Exception: Macro 'uuid' in engine arguments is only supported "
            "when the UUID is explicitly specified. (BAD\\_ARGUMENTS)**\n"
            "\n"
            "**raised at:** `boba/tool/ch/tools.py:812 in ch_stream_in`\n"
            "\n"
            "**caused by:** `DatabaseError (same message)`"
        ),
        "markdown": ("```sql\nx\n```\n\n_n_"),
        "shell": ("```stdout\nhi\n```\n\n_exit code: 0_"),
        "sql": ("| a  |\n|----|\n| 1  |"),
        "sql_failure": (
            "**ClickHouseQueryError: clickhouse statement failed: Code: 44. "
            "DB::Exception: Sorting key contains nullable columns "
            "(ILLEGAL\\_COLUMN)**\n"
            "\n"
            "**raised at:** `boba/db/clickhouse/transfer.py:175 in command`\n"
            "\n"
            "| column | stream field | type            | nullable | source type | "
            "note |\n"
            "|--------|--------------|-----------------|----------|-------------|------|\n"
            "| id     | id           | Nullable(Int32) | nullable | integer     |  "
            "    |\n"
            "\n"
            "```sql\n"
            "drop table if exists `default`.`people_src__ex`\n"
            "```\n"
            "\n"
            "_done_\n"
            "\n"
            "```sql\n"
            "create table `default`.`people_src` (`id` Nullable(Int32)) engine = "
            "MergeTree order by id\n"
            "```\n"
            "\n"
            "_failed: ClickHouseQueryError_"
        ),
        "sql_pump": (
            "```sql\n"
            "set work_mem = '64MB'\n"
            "```\n"
            "\n"
            "_SET_\n"
            "\n"
            "```sql\n"
            "copy (select id from t) to stdout (format csv)\n"
            "```\n"
            "\n"
            "_streamed out copy csv from postgres 17.10, 1 columns_\n"
            "\n"
            "**server:** `backend pid 1, version 170010`\n"
            "\n"
            "| column | type    | not null |\n"
            "|--------|---------|----------|\n"
            "| id     | integer | ✓        |"
        ),
        "stream_group_failure": (
            "**stopped: ch\\_stream\\_in failed in the stream group**\n"
            "\n"
            "**failed call:** `ch_stream_in (call_01)`\n"
            "\n"
            "**cause:**\n"
            "```\n"
            "ClickHouseQueryError: clickhouse statement failed: Code: 36. "
            "DB::Exception: Macro 'uuid' in engine arguments is only supported "
            "when the UUID is explicitly specified. (BAD_ARGUMENTS)\n"
            "```\n"
            "\n"
            "_nothing was committed_"
        ),
        "table": ("| a  |\n|----|\n| 1  |\n\n_n_"),
        "visual": ("_(plotly: t)_"),
    }

    async def _drawn(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        raw: Any,
        name: str = "mcp__boba__probe",
    ) -> None:
        view = ChatView(THREAD, LiveSink(), "u-1", storage)
        updates = UpdateView(view, THREAD, RemoteJournals(), ToolMedia())
        await updates.apply(_question())
        await updates.apply(_call(CALL, name, {}))
        await updates.apply(_done(CALL, "text for the model", raw))
        await updates.finish_turn()

    def _reply(self, *blocks: mt.ContentBlock) -> dict[str, Any]:
        reply = mt.CallToolResult(content=list(blocks))

        return reply.model_dump(mode="json", by_alias=True, exclude_none=True)

    @pytest.mark.anyio
    @pytest.mark.parametrize("kind", sorted(LOOKS))
    async def test_kind_of_boba_mcp_looks_as_before(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
        kind: str,
    ) -> None:
        raw = (self.SNAPSHOTS / f"{kind}.json").read_text(encoding="utf-8")
        artifact = ToolArtifact.revive(json.loads(raw))
        assert artifact is not None

        await self._drawn(runtime_stand, storage, _result(artifact))

        step = recorded.last()[_id(CALL, StepRole.TOOL)]
        assert step.get(StepField.OUTPUT) == self.LOOKS[kind]
        assert step.get(StepField.IS_ERROR) is (not artifact.ok), step

    def _output(self, recorded: FakeEmitter) -> str:
        return str(recorded.last()[_id(CALL, StepRole.TOOL)].get(StepField.OUTPUT))

    def _resource(self, mime: str, text: str, name: str) -> mt.EmbeddedResource:
        contents = mt.TextResourceContents(
            uri=f"file:///srv/{name}", mime_type=mime, text=text
        )

        return mt.EmbeddedResource(resource=contents)

    @pytest.mark.anyio
    async def test_text_is_markdown_and_model_only_blocks_are_hidden(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        model = mt.Annotations(audience=["assistant"])
        user = mt.Annotations(audience=["user"])
        reply = self._reply(
            mt.TextContent(text="for the model", annotations=model),
            mt.TextContent(text="**for the user**", annotations=user),
            mt.TextContent(text="for both"),
        )

        await self._drawn(runtime_stand, storage, reply)

        assert self._output(recorded) == "**for the user**\n\nfor both"

    @pytest.mark.anyio
    async def test_csv_resource_is_a_table(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        table = self._resource("text/csv", "a,b\n1,\n", "report.csv")

        await self._drawn(runtime_stand, storage, self._reply(table))

        assert self._output(recorded) == "| a  | b  |\n|----|----|\n| 1  |    |"

    @pytest.mark.anyio
    async def test_code_and_unknown_text_are_fenced_with_the_resource_name(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        reply = self._reply(
            self._resource("application/sql", "select 1", "sql"),
            self._resource("application/json", '{"a": 1', "json"),
            self._resource("text/plain", "line", "stdout"),
            self._resource("text/x-unknown", "<b>raw</b>", "notes"),
            self._resource("text/html", "<script>x()</script>", "page"),
        )

        await self._drawn(runtime_stand, storage, reply)

        assert self._output(recorded) == (
            "```sql\nselect 1\n```\n\n"
            '```json\n{"a": 1\n```\n\n'
            "```stdout\nline\n```\n\n"
            "```notes\n<b>raw</b>\n```\n\n"
            "```page\n<script>x()</script>\n```"
        )

    @pytest.mark.anyio
    async def test_json_is_a_tree_under_the_step_output(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        """JSON рисуется деревом элементом шага вызова под его выходом; текст
        вокруг остаётся выходом шага, а не JSON-строкой."""
        document = {"rows": [1, 2], "note": "<script>x()</script>"}
        reply = self._reply(
            mt.TextContent(text="before"),
            self._resource("application/json", json.dumps(document), "answer"),
        )

        await self._drawn(runtime_stand, storage, reply)

        assert self._output(recorded) == "before"
        trees: list[Any] = []
        for element in recorded.elements:
            if element.get("name") != CanvasElement.JSON_TREE:
                continue
            trees.append(element)

        assert len(trees) == 1, recorded.elements
        tree = trees[0]
        assert tree["props"] == {"value": document}, tree
        assert tree["forId"] == _id(CALL, StepRole.TOOL), tree
        assert tree["id"] == _id(f"{CALL}/0", StepRole.ELEMENT), tree

    @pytest.mark.anyio
    async def test_too_large_json_is_fenced(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        text = json.dumps({"blob": "x" * ChatView.TREE_BYTES})
        reply = self._reply(self._resource("application/json", text, "big"))

        await self._drawn(runtime_stand, storage, reply)

        assert self._output(recorded) == f"```big\n{text}\n```"
        for element in recorded.elements:
            assert element.get("name") != CanvasElement.JSON_TREE, element

    @pytest.mark.anyio
    async def test_plotly_figure_is_a_chart_next_to_the_step(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        figure = {"data": [{"type": "bar", "y": [1, 2]}]}
        chart = self._resource(
            "application/vnd.plotly.v1+json", json.dumps(figure), "orders"
        )

        await self._drawn(runtime_stand, storage, self._reply(chart))

        assert self._output(recorded) == ""
        carrier = recorded.last()[_id(CALL, StepRole.CHART)]
        assert carrier.get(StepField.OUTPUT) == "orders"
        plotted: list[str] = []
        for element in recorded.elements:
            plotted.append(str(element.get("type")))

        assert plotted == ["plotly"], recorded.elements

    @pytest.mark.anyio
    async def test_image_and_audio_are_thread_files_of_the_answer(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        """Картинка и звук ложатся файлом в хранилище треда и рисуются как
        файл workspace у ответа хода: картинка — карточкой вьювера, звук без
        вьювера — вложением по адресу канваса. Байтов в ленте нет, второй
        показ (воспроизведение, другая вкладка) ведёт к тому же файлу."""
        body = bytes(range(256)) * 400
        data = base64.b64encode(body).decode("ascii")
        assert len(data) > ChatView.BASE64_SLICE, "the block spans several slices"
        reply = self._reply(
            mt.ImageContent(data=data, mime_type="image/png"),
            mt.AudioContent(data=data, mime_type="audio/mpeg"),
        )

        CanvasRegistry.register(ImageViewer())
        try:
            await self._drawn(runtime_stand, storage, reply)
            await self._drawn(runtime_stand, storage, reply)
        finally:
            CanvasRegistry.reset()

        assert self._output(recorded) == ""
        shown: list[tuple[str, str, str, str]] = []
        for element in recorded.elements:
            props = element.get("props")
            url = str(element.get("url"))
            if isinstance(props, dict):
                url = str(props.get("path"))

            shown.append(
                (str(element["name"]), url, str(element["id"]), str(element["forId"]))
            )

        assert len(shown) == 4, recorded.elements
        assert shown[:2] == shown[2:], "a replay draws the same files"
        card, attached = shown[:2]
        answer = _id(QUESTION, StepRole.ANSWER)
        assert card[0] == CanvasElement.LINK, card
        assert card[2:] == (_id(f"{CALL}/0", StepRole.ELEMENT), answer), card
        image = ObjectKey.from_workspace("u-1", THREAD, card[1])
        assert image.name.endswith(".png"), image
        assert await get_bytes(storage, image.render()) == body

        name = PurePosixPath(urlsplit(attached[1]).path).name
        audio = ObjectKey(user_id="u-1", thread_id=THREAD, name=name)
        assert attached == (
            name,
            CanvasFileUrl.path(audio, "audio/mpeg"),
            _id(f"{CALL}/1", StepRole.ELEMENT),
            answer,
        ), attached
        assert name.endswith(".mp3"), name
        assert await get_bytes(storage, audio.render()) == body

    @pytest.mark.anyio
    async def test_bytes_and_unknown_links_are_json_without_the_bytes(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        blob = base64.b64encode(b"zip-bytes-of-the-file").decode("ascii")
        contents = mt.BlobResourceContents(
            uri="file:///srv/a.zip", mime_type="application/zip", blob=blob
        )
        reply = self._reply(
            mt.EmbeddedResource(resource=contents),
            mt.ResourceLink(name="x", uri="javascript:alert(1)"),
            mt.ImageContent(data="not base64!", mime_type="image/png"),
        )

        await self._drawn(runtime_stand, storage, reply)

        output = self._output(recorded)
        assert blob not in output
        assert '"mimeType": "application/zip"' in output
        assert '"uri": "javascript:alert(1)"' in output
        assert '"mimeType": "image/png"' in output
        assert "not base64!" not in output
        assert output.startswith("```json\n")
        assert not recorded.elements

    @pytest.mark.anyio
    async def test_http_link_is_an_attachment(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        link = mt.ResourceLink(
            name="report.csv", uri="https://example.org/r.csv", mime_type="text/csv"
        )

        await self._drawn(runtime_stand, storage, self._reply(link))

        attached: list[tuple[str, str, str]] = []
        for element in recorded.elements:
            attached.append(
                (str(element["name"]), str(element["url"]), str(element["forId"]))
            )

        answer = _id(QUESTION, StepRole.ANSWER)
        assert attached == [("report.csv", "https://example.org/r.csv", answer)]

    @pytest.mark.anyio
    async def test_error_flag_marks_the_step_failed(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        reply = mt.CallToolResult(content=[mt.TextContent(text="boom")], is_error=True)
        raw = reply.model_dump(mode="json", by_alias=True, exclude_none=True)

        await self._drawn(runtime_stand, storage, raw)

        step = recorded.last()[_id(CALL, StepRole.TOOL)]
        assert step.get(StepField.IS_ERROR) is True
        assert str(step.get(StepField.NAME)).startswith("✖ probe")
        assert step.get(StepField.OUTPUT) == "boom"

    @pytest.mark.anyio
    async def test_raw_output_of_an_old_thread_is_json(
        self,
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        recorded: FakeEmitter,
    ) -> None:
        """Старый тред хранит в rawOutput модель результата с kind: второго
        пути чтения нет, она показывается неизвестным — своим JSON."""
        old = {"kind": "markdown", "text": "rows", "ok": True}

        await self._drawn(runtime_stand, storage, old)

        step = recorded.last()[_id(CALL, StepRole.TOOL)]
        assert json.loads(str(step.get(StepField.OUTPUT))) == old
        assert step.get("language") == "json"
