"""Хук PreToolUse на настоящем ходе агента: стенд-обработчик по HTTP,
стенд инструментов в процессе и обычный MCP-сервер субпроцессом.
Обработчик подменяет аргумент — подмену получает сервер, журнал хранит
исходное; `deny` снимает вызов; молчание, ошибка и не-JSON решаются
`onFailure`; `ask` ведёт к вопросу наружу; матчер отсеивает чужие
инструменты; отмена хода обрывает ожидание обработчика; узел вызова
workflow проходит обработчик как вызов своего инструмента."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from boba.agent.control import AllowAnswer
from boba.agent.events import RecordEvent, TurnOutcome
from boba.agent.history import SessionId
from boba.agent.hooks import (
    HookEventName,
    HookFailure,
    HookGroup,
    HookMatcher,
    HttpHookSpec,
    McpServerInfo,
    PreToolUseHooks,
)
from boba.agent.queue import QueueEntry, QueueKind
from boba.agent.records import (
    AssistantRecord,
    ChainRecord,
    PermissionMode,
    Record,
    ToolDenialKind,
    ToolResultBlock,
    ToolUseBlock,
    UserRecord,
)
from boba.cli.hooks import HttpHookTransport
from boba.identity.context import CallContexts
from boba.mcp_client.client import (
    DroppedSignals,
    McpServerConfig,
    McpToolServer,
    NamedBlocks,
    StdioCommand,
)
from boba.stand.agent import (
    Collected,
    FakeServer,
    Protocol,
    Served,
    StandAgents,
    StandArg,
    StandToolName,
    StandTools,
)
from boba.stand.hooks import HookStandRules, HookStandServer
from boba.stand.ui.fake_llm import ScenarioName, ToolCallSpec
from boba.toolkit.dag import CallDag, DagNode, NodeOutcome, ToolCard, ToolHints
from boba.toolkit.result import MarkdownResult
from boba.toolrun.hosted import DirectCalls

pytestmark = pytest.mark.anyio

SESSION = SessionId(value="cli-hooks-1")
CWD = "/work/project"
HEADERS = {"Authorization": "Bearer jwt-1"}
FEATURES: dict[str, Mapping[str, object]] = {
    "com.boba/connection-seal": {"key": {"kty": "EC", "kid": "k1"}}
}
MCP_SERVER = (
    Path(__file__).resolve().parents[3]
    / "services"
    / "boba-mcp-client"
    / "tests"
    / "standard_server.py"
)
QUERY_ARGUMENTS = {
    StandArg.CONNECTION.value: "dwh",
    StandArg.STATEMENT.value: "select 1",
}


def as_sent(name: str, arguments: Mapping[str, object]) -> dict[str, object]:
    """Аргументы, как их шлёт фейковая модель: с подписью intent."""
    return {**arguments, ToolCallSpec.INTENT_FIELD: f"stand call of {name}"}


SENT_QUERY = as_sent(StandToolName.QUERY.value, QUERY_ARGUMENTS)
SEALED_QUERY = {**SENT_QUERY, StandArg.CONNECTION.value: "sealed:dwh"}


def prompt(text: str) -> QueueEntry:
    return QueueEntry(kind=QueueKind.PROMPT, text=text)


def call(*specs: dict[str, object]) -> str:
    if len(specs) == 1:
        return f"{ScenarioName.CALL.value} {json.dumps(specs[0])}"

    return f"{ScenarioName.CALL.value} {json.dumps(list(specs))}"


def query_call(**arguments: object) -> str:
    merged: dict[str, object] = {**QUERY_ARGUMENTS, **arguments}

    return call({"name": StandToolName.QUERY.full, "arguments": merged})


def results(records: Sequence[Record] | Sequence[ChainRecord]) -> list[ToolResultBlock]:
    found: list[ToolResultBlock] = []
    for record in records:
        if isinstance(record, UserRecord):
            found.extend(record.message.tool_results())

    return found


def result_records(
    records: Sequence[Record] | Sequence[ChainRecord],
) -> list[UserRecord]:
    found: list[UserRecord] = []
    for record in records:
        if isinstance(record, UserRecord) and record.message.tool_results():
            found.append(record)

    return found


def tool_uses(records: Sequence[Record] | Sequence[ChainRecord]) -> list[ToolUseBlock]:
    found: list[ToolUseBlock] = []
    for record in records:
        if isinstance(record, AssistantRecord):
            found.extend(record.message.tool_uses())

    return found


@dataclass(frozen=True)
class HookOptions:
    """Настройки обработчиков теста: матчер, судьба отказа, срок."""

    matcher: str = "mcp__stand__.*"
    on_failure: HookFailure = HookFailure.BLOCK
    timeout_sec: float = 5.0


def hooks_for(
    handlers: Sequence[HookStandServer],
    transport: HttpHookTransport,
    servers: Mapping[str, McpServerInfo],
    options: HookOptions | None = None,
) -> PreToolUseHooks:
    """Исполнитель хука: по группе на обработчик, все под одним матчером."""
    if options is None:
        options = HookOptions()

    groups: list[HookGroup] = []
    for handler in handlers:
        spec = HttpHookSpec(
            url=handler.url,
            headers=HEADERS,
            timeout_sec=options.timeout_sec,
            on_failure=options.on_failure,
        )
        groups.append(HookGroup(matcher=HookMatcher(options.matcher), hooks=[spec]))

    return PreToolUseHooks(groups, transport, servers, CWD)


@pytest.fixture
async def server() -> AsyncIterator[FakeServer]:
    fake = FakeServer()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def agents(server: FakeServer, tmp_path: Path) -> AsyncIterator[StandAgents]:
    stand = StandAgents(server, tmp_path / "history")
    try:
        yield stand
    finally:
        await stand.aclose()


@pytest.fixture
async def transport() -> AsyncIterator[HttpHookTransport]:
    http = HttpHookTransport(httpx.AsyncClient(trust_env=False))
    try:
        yield http
    finally:
        await http.aclose()


class Handlers:
    """Стенд-обработчики одного теста: поднимаются по правилам, гасятся разом."""

    def __init__(self) -> None:
        self._started: list[HookStandServer] = []

    async def start(self, rules: HookStandRules) -> HookStandServer:
        handler = HookStandServer(rules)
        await handler.start()
        self._started.append(handler)

        return handler

    async def stop(self) -> None:
        for handler in self._started:
            await handler.stop()


@pytest.fixture
async def handlers() -> AsyncIterator[Handlers]:
    started = Handlers()
    try:
        yield started
    finally:
        await started.stop()


class Stand:
    """Агент стенда в режиме без вопросов с инструментами и хуком."""

    def __init__(self, agents: StandAgents, tools: StandTools) -> None:
        self._agents = agents
        self.tools = tools
        self.sink = Collected()

    def served(self, hooks: PreToolUseHooks) -> Served:
        agent = self._agents.agent(
            self._agents.profile(Protocol.OPENAI),
            self.sink,
            tools=self.tools,
            initial_mode=PermissionMode.BYPASS_PERMISSIONS,
            allow_bypass=True,
            hooks=hooks,
        )

        return Served(agent)


class LinkingTools(StandTools):
    """Стенд инструментов с инструментом workflow: вызов записывается таким,
    каким пришёл серверу, узлы не исполняются."""

    def __init__(self) -> None:
        super().__init__(FEATURES)
        self.workflow = self._names.full(self.SERVER, CallDag.WORKFLOW)
        """Полное имя инструмента workflow стенда."""
        self._cards.append(
            ToolCard(
                name=self.workflow,
                description="stand tool workflow",
                parameters={
                    "type": "object",
                    "properties": {CallDag.NODES: {"type": "array"}},
                },
                views=None,
                hints=ToolHints(),
            )
        )

    async def submit(
        self, calls: Sequence[DagNode]
    ) -> Sequence[asyncio.Future[NodeOutcome]]:
        pending: list[asyncio.Future[NodeOutcome]] = []
        for call in calls:
            if call.tool != self.workflow:
                pending.extend(await super().submit([call]))
                continue

            self.calls.append(call)
            linked = self._outcomes.of(call, MarkdownResult(text="linked"), False)
            pending.append(self._outcomes.settled(linked))

        return pending


class TestWorkflowNodes:
    """Узел вызова workflow — вызов своего инструмента: обработчик получает
    событие узла с именем, схемой и идентификатором вызова этого инструмента."""

    NODES: list[dict[str, object]] = [
        {"key": "src", "tool": StandToolName.QUERY.value, "args": QUERY_ARGUMENTS},
        {"key": "dst", "tool": StandToolName.LOOKUP.value},
    ]

    async def test_connections_of_nodes_are_sealed_and_the_journal_keeps_the_names(
        self,
        agents: StandAgents,
        transport: HttpHookTransport,
        handlers: Handlers,
    ) -> None:
        handler = await handlers.start(HookStandRules())
        tools = LinkingTools()
        stand = Stand(agents, tools)
        hooks = hooks_for([handler], transport, stand.tools.described())
        served = stand.served(hooks)
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(
            prompt(call({"name": tools.workflow, "arguments": {"nodes": self.NODES}}))
        )
        await served.agent.settled()
        await served.close()

        sent = stand.tools.calls
        assert [c.tool for c in sent] == ["mcp__stand__workflow"]
        assert sent[0].args[CallDag.NODES] == [
            {
                "key": "src",
                "tool": StandToolName.QUERY.value,
                "args": {**QUERY_ARGUMENTS, StandArg.CONNECTION.value: "sealed:dwh"},
            },
            {"key": "dst", "tool": StandToolName.LOOKUP.value},
        ]

        uses = tool_uses(session.records())
        assert uses[0].input[CallDag.NODES] == self.NODES
        assert results(session.records())[0].text() == "linked"

        events = [received.event for received in handler.app.received]
        assert [e.tool_name for e in events] == [
            "mcp__stand__workflow",
            "mcp__stand__query",
            "mcp__stand__lookup",
        ]
        assert [e.tool_use_id for e in events] == [
            uses[0].id,
            f"{uses[0].id}_0",
            f"{uses[0].id}_1",
        ]
        assert events[1].tool_input == QUERY_ARGUMENTS
        assert events[1].tool_schema == StandTools.QUERY_SCHEMA

    async def test_node_named_by_the_full_name_is_the_same_tool(
        self,
        agents: StandAgents,
        transport: HttpHookTransport,
        handlers: Handlers,
    ) -> None:
        """Модель видит инструменты под полными именами и вправе назвать
        узлу полное: обработчик получает то же имя, без второго сервера."""
        handler = await handlers.start(HookStandRules())
        tools = LinkingTools()
        stand = Stand(agents, tools)
        served = stand.served(hooks_for([handler], transport, tools.described()))
        await served.open(SESSION, resume=False)

        nodes = [{"key": "dst", "tool": StandToolName.LOOKUP.full}]
        served.agent.queue.enqueue(
            prompt(call({"name": tools.workflow, "arguments": {"nodes": nodes}}))
        )
        await served.agent.settled()
        await served.close()

        names = [received.event.tool_name for received in handler.app.received]
        assert names == ["mcp__stand__workflow", "mcp__stand__lookup"]

    async def test_denied_node_keeps_the_whole_call_from_the_server(
        self,
        agents: StandAgents,
        transport: HttpHookTransport,
        handlers: Handlers,
    ) -> None:
        handler = await handlers.start(
            HookStandRules(denied={"mcp__stand__query": "no grant for dwh"})
        )
        tools = LinkingTools()
        stand = Stand(agents, tools)
        hooks = hooks_for([handler], transport, stand.tools.described())
        served = stand.served(hooks)
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(
            prompt(call({"name": tools.workflow, "arguments": {"nodes": self.NODES}}))
        )
        await served.agent.settled()
        await served.close()

        assert stand.tools.calls == []
        found = results(session.records())
        assert found[0].is_error is True
        assert found[0].text() == (
            "The PreToolUse hook denied the call of mcp__stand__workflow: "
            "no grant for dwh"
        )


class TestSealing:
    async def test_server_gets_the_sealed_argument_and_the_journal_keeps_the_original(
        self,
        server: FakeServer,
        agents: StandAgents,
        transport: HttpHookTransport,
        handlers: Handlers,
    ) -> None:
        handler = await handlers.start(HookStandRules())
        stand = Stand(agents, StandTools(features=FEATURES))
        hooks = hooks_for([handler], transport, stand.tools.described())
        served = stand.served(hooks)
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(query_call()))
        await served.agent.settled()
        await served.close()

        assert [c.args for c in stand.tools.calls] == [SEALED_QUERY]
        uses = tool_uses(session.records())
        assert [u.input for u in uses] == [SENT_QUERY]
        assert results(session.records())[0].text() == "select 1 on sealed:dwh"
        assert served.agent.outcomes == [TurnOutcome.COMPLETED]

        recorded = [
            e.record
            for e in stand.sink.of(RecordEvent)
            if isinstance(e, RecordEvent) and isinstance(e.record, AssistantRecord)
        ]
        assert [u.input for r in recorded for u in r.message.tool_uses()] == [
            SENT_QUERY
        ]

        received = handler.app.received
        assert len(received) == 1
        event = received[0].event
        assert event.hook_event_name is HookEventName.PRE_TOOL_USE
        assert event.session_id == SESSION.value
        assert event.cwd == CWD
        assert event.permission_mode is PermissionMode.BYPASS_PERMISSIONS
        assert event.tool_name == "mcp__stand__query"
        assert event.tool_input == SENT_QUERY
        assert event.tool_use_id == uses[0].id
        assert event.tool_schema == StandTools.QUERY_SCHEMA
        assert event.mcp_server is not None
        assert event.mcp_server.name == StandTools.SERVER
        assert event.mcp_server.features == FEATURES
        assert received[0].headers["authorization"] == HEADERS["Authorization"]
        assert received[0].headers["content-type"] == "application/json"

        second = server.requests()[1]["messages"]
        assert isinstance(second, list)
        tool_messages = [m for m in second if m["role"] == "tool"]
        assert tool_messages[0]["content"] == "select 1 on sealed:dwh"

    async def test_handlers_run_in_order_and_each_sees_the_previous_replacement(
        self,
        agents: StandAgents,
        transport: HttpHookTransport,
        handlers: Handlers,
    ) -> None:
        sealing = await handlers.start(HookStandRules())
        rewriting = await handlers.start(
            HookStandRules(
                seal_prefix="",
                replacements={
                    "mcp__stand__query": {StandArg.STATEMENT.value: "select 2"}
                },
            )
        )
        stand = Stand(agents, StandTools())
        hooks = hooks_for([sealing, rewriting], transport, stand.tools.described())
        served = stand.served(hooks)
        await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(query_call()))
        await served.agent.settled()
        await served.close()

        assert [c.args for c in stand.tools.calls] == [
            {**SEALED_QUERY, StandArg.STATEMENT.value: "select 2"}
        ]
        seen_by_second = rewriting.app.received[0].event.tool_input
        assert seen_by_second[StandArg.CONNECTION.value] == "sealed:dwh"
        assert seen_by_second[StandArg.STATEMENT.value] == "select 1"

    async def test_unmatched_tool_is_not_sent_to_the_handler(
        self,
        agents: StandAgents,
        transport: HttpHookTransport,
        handlers: Handlers,
    ) -> None:
        handler = await handlers.start(HookStandRules())
        stand = Stand(agents, StandTools())
        hooks = hooks_for(
            [handler],
            transport,
            stand.tools.described(),
            HookOptions(matcher="mcp__other__.*"),
        )
        served = stand.served(hooks)
        await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(query_call()))
        await served.agent.settled()
        await served.close()

        assert handler.app.received == []
        assert [c.args for c in stand.tools.calls] == [SENT_QUERY]


class TestMcpServer:
    def _server(self) -> McpToolServer:
        endpoint = StdioCommand(command=sys.executable, args=(str(MCP_SERVER), "stdio"))
        config = McpServerConfig(
            endpoint=endpoint, connect_timeout_sec=20.0, call_timeout_sec=20.0
        )

        return McpToolServer(
            "standard",
            config,
            NamedBlocks(),
            DroppedSignals(),
            CallContexts(),
            None,
            DirectCalls(),
        )

    @staticmethod
    def _described(server: McpToolServer) -> Mapping[str, McpServerInfo]:
        info = McpServerInfo(name="standard", features=server.features())

        return {"standard": info}

    async def test_standard_server_receives_the_replacement(
        self,
        agents: StandAgents,
        transport: HttpHookTransport,
        handlers: Handlers,
    ) -> None:
        handler = await handlers.start(
            HookStandRules(replacements={"mcp__standard__shout": {"text": "sealed hi"}})
        )
        mcp = self._server()
        await mcp.open()
        sink = Collected()
        hooks = hooks_for(
            [handler],
            transport,
            self._described(mcp),
            HookOptions(matcher="mcp__standard__.*"),
        )
        served = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI),
                sink,
                tools=mcp,
                initial_mode=PermissionMode.BYPASS_PERMISSIONS,
                allow_bypass=True,
                hooks=hooks,
            )
        )
        session = await served.open(SESSION, resume=False)
        try:
            served.agent.queue.enqueue(
                prompt(
                    call({"name": "mcp__standard__shout", "arguments": {"text": "hi"}})
                )
            )
            await served.agent.settled()
        finally:
            await served.close()
            await mcp.close()

        sent = as_sent("shout", {"text": "hi"})
        assert results(session.records())[0].text() == "SEALED HI"
        assert [u.input for u in tool_uses(session.records())] == [sent]
        assert handler.app.received[0].event.tool_input == sent
        event = handler.app.received[0].event
        assert event.tool_name == "mcp__standard__shout"
        assert event.mcp_server is not None
        assert event.mcp_server.name == "standard"
        assert event.mcp_server.features == {}
        assert event.tool_schema.get("properties", {}).get("text") is not None


class TestDeny:
    async def test_denied_call_does_not_reach_the_server_and_the_model_sees_why(
        self,
        server: FakeServer,
        agents: StandAgents,
        transport: HttpHookTransport,
        handlers: Handlers,
    ) -> None:
        handler = await handlers.start(
            HookStandRules(denied={"mcp__stand__drop": "no grant for drop"})
        )
        stand = Stand(agents, StandTools())
        hooks = hooks_for([handler], transport, stand.tools.described())
        served = stand.served(hooks)
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(
            prompt(
                call(
                    {"name": StandToolName.DROP.full},
                    {"name": StandToolName.LOOKUP.full},
                )
            )
        )
        await served.agent.settled()
        await served.close()

        assert [c.tool for c in stand.tools.calls] == [StandToolName.LOOKUP.full]
        found = results(session.records())
        assert found[0].is_error is True
        assert found[0].text() == (
            "The PreToolUse hook denied the call of mcp__stand__drop: no grant for drop"
        )
        assert found[1].text() == "lookup done"
        assert result_records(session.records())[0].tool_denial_kind is None
        assert served.agent.outcomes == [TurnOutcome.COMPLETED]

        second = server.requests()[1]["messages"]
        assert isinstance(second, list)
        tool_messages = [m for m in second if m["role"] == "tool"]
        assert tool_messages[0]["content"].startswith("<tool_use_error>")
        assert "no grant for drop" in tool_messages[0]["content"]


class TestFailures:
    async def test_silent_handler_blocks_or_passes_by_on_failure(
        self,
        agents: StandAgents,
        transport: HttpHookTransport,
        handlers: Handlers,
    ) -> None:
        handler = await handlers.start(
            HookStandRules(silent=frozenset({"mcp__stand__query"}), hang_sec=2.0)
        )
        blocking = Stand(agents, StandTools())
        served = blocking.served(
            hooks_for(
                [handler],
                transport,
                blocking.tools.described(),
                HookOptions(on_failure=HookFailure.BLOCK, timeout_sec=0.3),
            )
        )
        session = await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(query_call()))
        await served.agent.settled()
        await served.close()

        assert blocking.tools.calls == []
        text = results(session.records())[0].text()
        assert text.startswith(f"The PreToolUse hook at {handler.url} did not answer")
        assert "query" in text
        assert "0.3 s" in text
        assert results(session.records())[0].is_error is True

        passing = Stand(agents, StandTools())
        served = passing.served(
            hooks_for(
                [handler],
                transport,
                passing.tools.described(),
                HookOptions(on_failure=HookFailure.CONTINUE, timeout_sec=0.3),
            )
        )
        session = await served.open(SessionId(value="cli-hooks-2"), resume=False)
        served.agent.queue.enqueue(prompt(query_call()))
        await served.agent.settled()
        await served.close()

        assert [c.args for c in passing.tools.calls] == [SENT_QUERY]
        assert results(session.records())[0].text() == "select 1 on dwh"

    @pytest.mark.parametrize(
        ("rules", "complaint"),
        [
            (HookStandRules(failing=frozenset({"mcp__stand__query"})), "got 500"),
            (
                HookStandRules(garbled=frozenset({"mcp__stand__query"})),
                "expected a JSON object",
            ),
        ],
    )
    async def test_bad_answer_counts_as_a_failure(
        self,
        agents: StandAgents,
        transport: HttpHookTransport,
        handlers: Handlers,
        rules: HookStandRules,
        complaint: str,
    ) -> None:
        handler = await handlers.start(rules)
        stand = Stand(agents, StandTools())
        served = stand.served(hooks_for([handler], transport, stand.tools.described()))
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(query_call()))
        await served.agent.settled()
        await served.close()

        assert stand.tools.calls == []
        text = results(session.records())[0].text()
        assert text.startswith(f"The PreToolUse hook at {handler.url} did not answer")
        assert complaint in text


class TestAsk:
    async def test_ask_turns_into_a_question_with_the_updated_input(
        self,
        agents: StandAgents,
        transport: HttpHookTransport,
        handlers: Handlers,
    ) -> None:
        handler = await handlers.start(
            HookStandRules(asked={"mcp__stand__query": "confirm the connection"})
        )
        stand = Stand(agents, StandTools())
        served = stand.served(hooks_for([handler], transport, stand.tools.described()))
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(query_call()))
        question = await stand.sink.next_question(0)
        assert question.request.tool_name == StandToolName.QUERY.full
        assert question.request.input == SEALED_QUERY
        assert served.agent.answer(question.request_id, AllowAnswer())
        await served.agent.settled()
        await served.close()

        assert [c.args for c in stand.tools.calls] == [SEALED_QUERY]
        assert [u.input for u in tool_uses(session.records())] == [SENT_QUERY]


class TestCancellation:
    async def test_interrupt_while_the_handler_is_silent(
        self,
        agents: StandAgents,
        transport: HttpHookTransport,
        handlers: Handlers,
    ) -> None:
        handler = await handlers.start(
            HookStandRules(silent=frozenset({"mcp__stand__query"}), hang_sec=5.0)
        )
        stand = Stand(agents, StandTools())
        served = stand.served(
            hooks_for(
                [handler],
                transport,
                stand.tools.described(),
                HookOptions(timeout_sec=10.0),
            )
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(query_call()))
        while not handler.app.received:
            await asyncio.sleep(0.01)

        assert served.agent.interrupt()
        await asyncio.wait_for(served.agent.settled(), timeout=3.0)
        await served.close()

        assert stand.tools.calls == []
        assert served.agent.outcomes == [TurnOutcome.ABORTED_TOOLS]
        record = result_records(session.records())[0]
        assert record.tool_denial_kind is ToolDenialKind.USER_REJECTED
