"""Лента из уведомлений ACP: один переводчик для живого хода (через шину) и
воспроизведения (`session/load`); идентификаторы шагов из `messageId` и
`toolCallId` по правилам ChatView.derive_id; итог вызова из `rawOutput`
(артефакт, адрес журнала); панель про агента.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from acp import (
    start_tool_call,
    text_block,
    tool_content,
    update_agent_message,
    update_agent_thought,
    update_tool_call,
    update_user_message,
)
from acp.schema import SessionConfigOptionSelect, SessionConfigSelectOption
from chainlit.context import ChainlitContext, context_var
from chainlit.step import StepDict
from pydantic import SecretStr

from boba.chainlit.agent.chat import SessionOptions
from boba.chainlit.agent.client import SessionUpdate
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
from boba.chainlit.canvas.remote import RemoteJournals
from boba.chainlit.chat.feed import QuestionBody, TurnFeed
from boba.chainlit.chat.panel_text import PanelText
from boba.chainlit.domain.fields import StepField
from boba.chainlit.rendering.acp_view import (
    MetaKey,
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
    JournalAddress,
    ToolCard,
    ToolHints,
    ToolUseResult,
    ToolUseResults,
    WorkflowNodeResult,
    WorkflowResult,
)
from boba.toolkit.result import ErrorResult, MarkdownResult

THREAD = "11111111-1111-1111-1111-111111111111"
QUESTION = "22222222-2222-2222-2222-222222222222"
MESSAGE = "msg_a"
SECOND_MESSAGE = "msg_b"
CALL = "call_1"


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
def recorded() -> Iterator[FakeEmitter]:
    """Контекст chainlit теста с эмиттером-рекордером."""
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
    chunk = update_agent_message(text_block(text))
    chunk.message_id = message_id

    return chunk


def _thought(text: str, message_id: str) -> SessionUpdate:
    chunk = update_agent_thought(text_block(text))
    chunk.message_id = message_id

    return chunk


def _call(call_id: str, name: str, args: dict[str, Any]) -> SessionUpdate:
    start = start_tool_call(
        call_id, f"{name} {args}", status="pending", raw_input=dict(args)
    )
    start.field_meta = {MetaKey.TOOL_NAME.value: name}

    return start


def _done(call_id: str, text: str, raw_output: Any) -> SessionUpdate:
    return update_tool_call(
        call_id,
        status="completed",
        content=[tool_content(text_block(text))],
        raw_output=raw_output,
    )


def _result(text: str, journal: JournalAddress | None = None) -> dict[str, Any]:
    return ToolUseResult(
        artifact=MarkdownResult(text=text, elapsed_ms=12).model_dump(
            mode="json", exclude_none=True
        ),
        journal=journal,
    ).model_dump(mode="json", exclude_none=True)


def _question() -> SessionUpdate:
    question = update_user_message(text_block("hello"))
    question.message_id = QUESTION

    return question


def _turn_updates() -> list[SessionUpdate]:
    """Ход: рассуждение, вызов инструмента, ответ после него."""
    return [
        _thought("let me ", MESSAGE),
        _thought("think", MESSAGE),
        _chunk("First ", MESSAGE),
        _call(CALL, "mcp__boba__pg_query", {"sql": "select 1"}),
        update_tool_call(CALL, status="in_progress"),
        _done(CALL, "1", _result("one row")),
        _chunk("and then ", SECOND_MESSAGE),
        _chunk("done.", SECOND_MESSAGE),
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
        await updates.apply(_call(CALL, "bash", {"cmd": "ls"}))
        await updates.apply(_done(CALL, "ok", _result("ok", address)))

        assert await journals.address(THREAD, CALL) == address

    @pytest.mark.anyio
    async def test_failed_call_without_artifact_is_an_error_step(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        updates = _view(runtime_stand, RemoteJournals())
        updates.begin_turn(QUESTION)

        await updates.apply(_call(CALL, "nope", {}))
        failed = update_tool_call(
            CALL,
            status="failed",
            content=[tool_content(text_block("unknown tool"))],
            raw_output="unknown tool",
        )
        await updates.apply(failed)

        step = recorded.last()[_id(CALL, StepRole.TOOL)]
        assert step.get(StepField.IS_ERROR) is True
        assert step.get(StepField.OUTPUT) == "unknown tool"
        assert str(step.get(StepField.NAME)).startswith("✖ nope")

    @pytest.mark.anyio
    async def test_workflow_call_is_drawn_as_steps_of_its_nodes(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        """Вызов workflow — шаги его узлов под идентификаторами вызовов узлов:
        каждый кончается своим результатом и получает свой адрес журнала."""
        journals = RemoteJournals()
        updates = _view(runtime_stand, journals)
        updates.begin_turn(QUESTION)

        nodes = [
            {"key": "src", "tool": "pg_stream_out", "args": {"out": "rows"}},
            {"key": "dst", "tool": "ch_stream_in", "args": {"feed": "rows"}},
        ]
        await updates.apply(_call(CALL, "mcp__boba__workflow", {"nodes": nodes}))

        source = _id(f"{CALL}_0", StepRole.TOOL)
        sink = _id(f"{CALL}_1", StepRole.TOOL)
        started = recorded.last()
        assert str(started[source].get(StepField.NAME)).endswith("pg_stream_out")
        assert str(started[sink].get(StepField.NAME)).endswith("ch_stream_in")
        assert _id(CALL, StepRole.TOOL) not in started

        progress = update_tool_call(f"{CALL}_0", status="in_progress")
        progress.field_meta = {
            MetaKey.PROGRESS.value: {"message": "rows sent", "progress": 3}
        }
        await updates.apply(progress)
        assert "rows sent" in str(recorded.last()[source].get(StepField.OUTPUT))

        result = WorkflowResult(
            nodes=[
                WorkflowNodeResult(
                    key="src",
                    call_id=f"{CALL}_0",
                    tool="pg_stream_out",
                    args={"out": "rows"},
                    errored=False,
                    content="3 rows",
                    result=MarkdownResult(text="3 rows sent"),
                ),
                WorkflowNodeResult(
                    key="dst",
                    call_id=f"{CALL}_1",
                    tool="ch_stream_in",
                    args={"feed": "rows"},
                    errored=True,
                    content="no such table",
                    result=ErrorResult(message="no such table", error_kind="tool"),
                ),
            ]
        )
        address = JournalAddress(server="boba", run="run-7", call="linked")
        raw = ToolUseResult(
            artifact=result.model_dump(mode="json", exclude_none=True),
            journal=address,
        ).model_dump(mode="json", exclude_none=True)
        await updates.apply(_done(CALL, "done", raw))

        steps = recorded.last()
        assert str(steps[source].get(StepField.NAME)).startswith("✔ pg_stream_out")
        assert "3 rows sent" in str(steps[source].get(StepField.OUTPUT))
        assert str(steps[sink].get(StepField.NAME)).startswith("✖ ch_stream_in")
        assert steps[sink].get(StepField.IS_ERROR) is True
        assert "no such table" in str(steps[sink].get(StepField.OUTPUT))
        assert updates.open_steps() == []

        first = await journals.address(THREAD, f"{CALL}_0")
        assert first == address.model_copy(update={"call": f"{CALL}_0"})

    @pytest.mark.anyio
    async def test_refused_workflow_call_fails_every_node_step(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        """Отказ вызова workflow целиком — итог каждого его узла."""
        updates = _view(runtime_stand, RemoteJournals())
        updates.begin_turn(QUESTION)

        nodes = [{"key": "src", "tool": "pg_stream_out", "args": {"out": "rows"}}]
        await updates.apply(_call(CALL, "mcp__boba__workflow", {"nodes": nodes}))
        refused = update_tool_call(
            CALL,
            status="failed",
            content=[tool_content(text_block("the hook denied the call"))],
            raw_output="the hook denied the call",
        )
        await updates.apply(refused)

        step = recorded.last()[_id(f"{CALL}_0", StepRole.TOOL)]
        assert step.get(StepField.IS_ERROR) is True
        assert step.get(StepField.OUTPUT) == "the hook denied the call"

    @pytest.mark.anyio
    async def test_step_is_named_without_the_server_and_drawn_by_the_card(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        """Агент называет инструмент полным именем: шаг подписан именем без
        сервера, а вид аргументов найден по карточке полного имени."""
        schema = {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "contentMediaType": "application/sql"}
            },
        }
        media = ToolMedia()
        media.shown(
            [
                ToolCard(
                    name="mcp__boba__pg_query",
                    description="query",
                    parameters=schema,
                    views=None,
                    hints=ToolHints(),
                )
            ]
        )
        view = ChatView(THREAD, LiveSink(), runtime_stand.journals, user_name="alice")
        updates = UpdateView(view, THREAD, RemoteJournals(), media)
        updates.begin_turn(QUESTION)

        await updates.apply(_call(CALL, "mcp__boba__pg_query", {"sql": "select 1"}))

        step = recorded.last()[_id(CALL, StepRole.TOOL)]
        assert str(step.get(StepField.NAME)).endswith("pg_query")
        assert "mcp__" not in str(step.get(StepField.NAME))
        assert str(step.get("input")).startswith("```sql\nselect 1\n```")

    @pytest.mark.anyio
    async def test_progress_meta_is_shown_in_the_running_step(
        self, runtime_stand: StandRefs, recorded: FakeEmitter
    ) -> None:
        updates = _view(runtime_stand, RemoteJournals())
        updates.begin_turn(QUESTION)

        await updates.apply(_call(CALL, "pg_copy", {}))
        progress = update_tool_call(CALL, status="in_progress")
        progress.field_meta = {
            MetaKey.PROGRESS.value: {
                "message": "rows copied",
                "progress": 50,
                "total": 200,
                "elapsed_sec": 1.5,
            }
        }
        await updates.apply(progress)

        step = recorded.last()[_id(CALL, StepRole.TOOL)]
        assert "rows copied" in str(step.get(StepField.OUTPUT))
        assert "25%" in str(step.get(StepField.OUTPUT))


class TestLiveEqualsReplay:
    """Живой ход через шину и воспроизведение дают одну и ту же ленту."""

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
                THREAD, _view(runtime_stand, RemoteJournals()), payloads, NoSurface()
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
            for update in _turn_updates():
                await replay.apply(update)

            await replay.finish_turn()
        finally:
            context_var.reset(token)

        live = live_context.emitter.outline(pulse)
        assert live == replay_context.emitter.outline(pulse)
        assert len(live) >= 6
        assert live_context.emitter.tokens == replay_context.emitter.tokens


class TestCodecAndResults:
    def test_codec_round_trip_keeps_meta_and_order(self) -> None:
        codec = SessionUpdateCodec()
        start = _call(CALL, "mcp__boba__pg_query", {"b": 1, "a": 2})

        body = codec.encode(start)
        assert body["_meta"] == {MetaKey.TOOL_NAME.value: "mcp__boba__pg_query"}
        assert list(body["rawInput"]) == ["b", "a"]

        decoded = codec.decode(body)
        assert decoded == start

    def test_tool_use_results_accept_wrapped_bare_and_text(self) -> None:
        results = ToolUseResults()
        address = JournalAddress(server="s", run="r", call="c")
        wrapped = results.of({"artifact": {"kind": "markdown"}, "journal": address})
        assert wrapped.journal == address
        assert wrapped.artifact == {"kind": "markdown"}

        bare = results.of({"kind": "markdown", "text": "x"})
        assert bare.journal is None
        assert bare.artifact == {"kind": "markdown", "text": "x"}

        text = results.of("denied")
        assert text.artifact == "denied"


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
            SessionConfigOptionSelect(
                id="mode",
                name="Mode",
                category="mode",
                type="select",
                current_value="bypassPermissions",
                options=[
                    SessionConfigSelectOption(value="default", name="Default"),
                    SessionConfigSelectOption(value="bypassPermissions", name="Bypass"),
                ],
            ),
            SessionConfigOptionSelect(
                id="model",
                name="Model",
                category="model",
                type="select",
                current_value="general",
                options=[SessionConfigSelectOption(value="general", name="general")],
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
