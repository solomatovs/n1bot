"""Отложенные инструменты и ToolSearch на стенде: имена во вложении
deferred_tools_delta, загрузка `select:` и поиск по словам, определение
загруженного в конце `tools` со следующего запроса и в истории
(deferred_tools_record), список «всегда загружать», вызов незагруженного —
ошибка, возобновление шлёт те же байты, сжатие переносит загруженные через
границу (docs/prompt_assembly/specs/07_system_tools.md, раздел 2.3)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest
from test_agent_compaction import journal

from boba.agent.attachments import Announced
from boba.agent.builder import RequestBuilder
from boba.agent.history import SessionId
from boba.agent.queue import QueueEntry, QueueKind
from boba.agent.records import (
    AttachmentRecord,
    ChainRecord,
    CompactBoundaryRecord,
    DeferredToolsDeltaAttachment,
    DeferredToolsRecordAttachment,
    Record,
    SnapshotTool,
    ToolReferenceBlock,
    ToolResultRecord,
    TurnOutcome,
)
from boba.agent.template import AgentTemplate
from boba.agent.tools import ToolMatcher, ToolSearchInput, ToolSearchOutput
from boba.stand.agent import (
    Collected,
    FakeServer,
    Protocol,
    Served,
    StandAgents,
    StandToolName,
    StandTools,
)
from boba.stand.ui.fake_llm import ScenarioName, WindowSettings

pytestmark = pytest.mark.anyio

SESSION = SessionId(value="agent-tool-search-1")
SEARCH = "ToolSearch"
DEFERRED_HEAD = (
    "The following deferred tools are now available via ToolSearch. Their "
    "schemas are NOT loaded — calling them directly will fail with "
    "InputValidationError."
)
LARGE_CHARS = 24_000


def prompt(text: str) -> QueueEntry:
    return QueueEntry(kind=QueueKind.PROMPT, text=text)


def call(*specs: dict[str, object]) -> str:
    if len(specs) == 1:
        return f"{ScenarioName.CALL.value} {json.dumps(specs[0])}"

    return f"{ScenarioName.CALL.value} {json.dumps(list(specs))}"


def search(query: str, max_results: int = 5) -> dict[str, object]:
    return {"name": SEARCH, "arguments": {"query": query, "max_results": max_results}}


def stand_names() -> list[str]:
    names: list[str] = []
    for name in StandToolName:
        names.append(name.full)

    return sorted(names)


def tool_names(request: dict[str, object]) -> list[str]:
    tools = request.get("tools")
    assert isinstance(tools, list)
    names: list[str] = []
    for tool in tools:
        names.append(tool["function"]["name"])

    return names


def attachments(records: Sequence[Record], kind: str) -> list[AttachmentRecord]:
    found: list[AttachmentRecord] = []
    for record in records:
        if isinstance(record, AttachmentRecord) and record.attachment.type == kind:
            found.append(record)

    return found


def results(records: Sequence[Record]) -> list[ToolResultRecord]:
    found: list[ToolResultRecord] = []
    for record in records:
        if isinstance(record, ToolResultRecord):
            found.append(record)

    return found


def tool_messages(request: dict[str, object]) -> list[str]:
    messages = request["messages"]
    assert isinstance(messages, list)
    texts: list[str] = []
    for message in messages:
        if message["role"] == "tool":
            texts.append(str(message["content"]))

    return texts


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


class TestDeferredConversation:
    @pytest.mark.parametrize("protocol", list(Protocol))
    async def test_names_then_select_then_definition_then_call(
        self, server: FakeServer, agents: StandAgents, protocol: Protocol
    ) -> None:
        """Модель видит отложенные одними именами, загружает lookup через
        ToolSearch `select:`, со следующего запроса определение lookup стоит
        в конце `tools`, вызов идёт серверу, начало списка не меняется."""
        stand = StandTools()
        served = Served(
            agents.agent(
                agents.profile(protocol), Collected(), tools=stand, always_load=()
            )
        )
        session = await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(call({"name": StandToolName.LOOKUP.full})))
        await served.agent.settled()
        await served.close()

        self.check_history(session.records(), session.conversation())
        self.check_requests(server.requests(), stand)
        assert served.agent.outcomes == [TurnOutcome.COMPLETED]

        snapshot = Announced(session.conversation()).snapshot()
        assert snapshot is not None
        built = RequestBuilder(agents.profile(protocol), AgentTemplate()).build(
            session.conversation(), snapshot
        )
        assert built.tools[-1].name == StandToolName.LOOKUP.full
        assert built.tools[-1].deferred
        assert not built.tools[0].deferred
        tool_turns = [m for m in built.messages if m.tool_references]
        assert list(tool_turns[0].tool_references) == [StandToolName.LOOKUP.full]

    @staticmethod
    def check_history(
        records: Sequence[Record], conversation: Sequence[ChainRecord]
    ) -> None:
        """Вложение с именами, снимок без отложенных, ссылка в результате
        ToolSearch и запись определения после неё."""
        records = list(records)
        delta = attachments(records, "deferred_tools_delta")
        assert len(delta) == 1
        payload = DeferredToolsDeltaAttachment.model_validate(
            delta[0].attachment.model_dump()
        )
        assert list(payload.added_names) == stand_names()
        assert not payload.tool_search_absent
        assert delta[0].rendered is not None
        text = delta[0].rendered[0].content
        assert DEFERRED_HEAD in text
        assert StandToolName.LOOKUP.full in text
        assert "stand tool lookup" not in text

        snapshot = Announced(conversation).snapshot()
        assert snapshot is not None
        inline: list[str] = []
        for tool in snapshot.tools:
            inline.append(tool.name)
        assert sorted(inline) == ["AskUserQuestion", SEARCH]

        found = results(records)
        assert len(found) == 2
        searched = found[0].message.tool_results()[0]
        assert list(searched.content) == [
            ToolReferenceBlock(tool_name=StandToolName.LOOKUP.full)
        ]
        output = ToolSearchOutput.model_validate(
            found[0].tool_use_result["structuredContent"]
        )
        assert list(output.matches) == [StandToolName.LOOKUP.full]
        assert output.total_deferred_tools == len(stand_names())

        recorded = attachments(records, "deferred_tools_record")
        assert len(recorded) == 1
        assert recorded[0].rendered is None
        entries = DeferredToolsRecordAttachment.model_validate(
            recorded[0].attachment.model_dump()
        ).entries
        assert [tool.name for tool in entries] == [StandToolName.LOOKUP.full]
        assert records.index(found[0]) < records.index(recorded[0])

    @staticmethod
    def check_requests(
        requests: Sequence[dict[str, object]], stand: StandTools
    ) -> None:
        """Определение lookup в конце `tools` со второго запроса, строка о
        загрузке в результате, вызов дошёл до сервера."""
        assert len(requests) == 3
        first, second, third = (tool_names(request) for request in requests)
        assert first == ["AskUserQuestion", SEARCH]
        assert second == [*first, StandToolName.LOOKUP.full]
        assert third == second
        assert DEFERRED_HEAD in json.dumps(requests[0]["messages"], ensure_ascii=False)
        assert tool_messages(requests[1]) == [
            f"Tool loaded: {StandToolName.LOOKUP.full}"
        ]
        assert [c.name for c in stand.calls] == [StandToolName.LOOKUP.full]

    async def test_keyword_search_ranks_by_name_and_reports_no_match(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        served = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI),
                Collected(),
                tools=StandTools(),
                always_load=(),
            )
        )
        session = await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(call(search("sleep", 1))))
        await served.agent.settled()
        served.agent.queue.enqueue(prompt(call(search("+nowhere tables"))))
        await served.agent.settled()
        await served.close()

        found = results(session.records())
        assert found[0].message.tool_results()[0].references() == [
            StandToolName.SLEEP.full
        ]
        missed = found[1].message.tool_results()[0]
        assert missed.references() == ()
        assert missed.text() == "No matching deferred tools found"
        assert not missed.is_error

        requests = server.requests()
        assert tool_names(requests[-1])[-1] == StandToolName.SLEEP.full


class TestAlwaysLoad:
    async def test_listed_tool_goes_inline_and_is_called_directly(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        stand = StandTools()
        served = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI),
                Collected(),
                tools=stand,
                always_load=[StandToolName.LOOKUP.full],
            )
        )
        session = await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(call({"name": StandToolName.LOOKUP.full})))
        await served.agent.settled()
        await served.close()

        delta = DeferredToolsDeltaAttachment.model_validate(
            attachments(session.records(), "deferred_tools_delta")[
                0
            ].attachment.model_dump()
        )
        assert StandToolName.LOOKUP.full not in delta.added_names
        assert len(delta.added_names) == len(stand_names()) - 1

        requests = server.requests()
        assert len(requests) == 2
        assert tool_names(requests[0]) == sorted(
            ["AskUserQuestion", SEARCH, StandToolName.LOOKUP.full]
        )
        assert tool_names(requests[1]) == tool_names(requests[0])
        assert [c.name for c in stand.calls] == [StandToolName.LOOKUP.full]
        assert attachments(session.records(), "deferred_tools_record") == []

    async def test_everything_listed_keeps_tool_search_out(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        served = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI), Collected(), tools=StandTools()
            )
        )
        session = await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        assert attachments(session.records(), "deferred_tools_delta") == []
        names = tool_names(server.requests()[0])
        assert SEARCH not in names
        assert StandToolName.LOOKUP.full in names


class TestNotLoaded:
    async def test_call_of_an_unloaded_tool_is_an_error_with_a_hint(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        """ToolSearch и сам инструмент одним ответом: к решению о вызове
        инструмент ещё не загружен — ошибка вызова, сервер его не видит."""
        stand = StandTools()
        served = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI),
                Collected(),
                tools=stand,
                always_load=(),
            )
        )
        session = await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(
            prompt(
                call(
                    search(f"select:{StandToolName.LOOKUP.full}"),
                    {"name": StandToolName.LOOKUP.full},
                )
            )
        )
        await served.agent.settled()
        await served.close()

        found = results(session.records())
        refused = found[1].message.tool_results()[0]
        assert refused.is_error
        assert "is a deferred tool and its schema is not loaded" in refused.text()
        assert f'"select:{StandToolName.LOOKUP.full}"' in refused.text()
        assert stand.calls == []
        assert (
            "<tool_use_error>InputValidationError"
            in tool_messages(server.requests()[1])[1]
        )


class TestResume:
    async def test_resume_sends_the_same_tools_byte_for_byte(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        """Возобновление собирает запрос из истории: тот же сервер — те же
        байты и ни одной новой записи определений. Сервер, описавший
        инструмент иначе, — новая запись (test_request_replay,
        TestDefinitionChanges)."""
        served = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI),
                Collected(),
                tools=StandTools(),
                always_load=(),
            )
        )
        await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(call({"name": StandToolName.LOOKUP.full})))
        await served.agent.settled()
        await served.close()
        before = server.requests()[-1]

        resumed = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI),
                Collected(),
                tools=StandTools(),
                always_load=(),
            )
        )
        session = await resumed.open(SESSION, resume=True)
        resumed.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await resumed.agent.settled()
        await resumed.close()

        after = server.requests()[-1]
        assert json.dumps(after["tools"]) == json.dumps(before["tools"])
        messages = after["messages"]
        assert isinstance(messages, list)
        earlier = before["messages"]
        assert isinstance(earlier, list)
        assert messages[: len(earlier)] == earlier
        assert len(attachments(session.records(), "deferred_tools_delta")) == 1
        assert len(attachments(session.records(), "deferred_tools_record")) == 1


class TestCompaction:
    async def test_loaded_tools_cross_the_boundary(self, tmp_path: Path) -> None:
        """Сжатие пишет загруженные в границу, объявляет отложенные заново
        и записывает определения в новом отрезке: lookup остаётся в конце
        `tools` первого запроса после сжатия."""
        fake = FakeServer(estimate_usage=True)
        await fake.start()
        stand = StandAgents(fake, tmp_path / "history")
        try:
            await self._compact(fake, stand, tmp_path / "history")
        finally:
            await stand.aclose()
            await fake.stop()

    async def _compact(
        self, server: FakeServer, agents: StandAgents, root: Path
    ) -> None:
        profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
        served = Served(
            agents.agent(profile, Collected(), tools=StandTools(), always_load=())
        )
        session = await served.open(SESSION, resume=False)
        large: dict[str, Any] = {
            "name": StandToolName.LARGE.full,
            "arguments": {"size": LARGE_CHARS, "fill": "x"},
        }
        served.agent.queue.enqueue(prompt(call(large)))
        await served.agent.settled()
        before = len(await journal(root, SESSION))
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        after = (await journal(root, SESSION))[before:]
        boundaries = [r for r in after if isinstance(r, CompactBoundaryRecord)]
        assert len(boundaries) == 1
        assert list(boundaries[0].compact_metadata.pre_compact_discovered_tools) == [
            StandToolName.LARGE.full
        ]
        assert len(attachments(after, "deferred_tools_delta")) == 1
        assert len(attachments(after, "deferred_tools_record")) == 1
        conversation = session.conversation()
        assert [t.name for t in Announced(conversation).deferred_loaded()] == [
            StandToolName.LARGE.full
        ]
        last = server.requests()[-1]
        assert tool_names(last)[-1] == StandToolName.LARGE.full
        assert served.agent.outcomes == [TurnOutcome.COMPLETED] * 2


class TestSummaryTools:
    async def test_every_ladder_step_offers_the_tools_of_the_turn(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        """Запрос пересказа несёт те же инструменты, что основной, и после
        отбрасывания групп, где записано определение загруженного: список
        берётся по всему разговору, а не по отрезку."""
        server.app.limit_window(WindowSettings(context_window=5_000, chars_per_token=4))
        profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
        served = Served(
            agents.agent(profile, Collected(), tools=StandTools(), always_load=())
        )
        await served.open(SESSION, resume=False)
        large: dict[str, Any] = {
            "name": StandToolName.LARGE.full,
            "arguments": {"size": 14_000, "fill": "x"},
        }
        served.agent.queue.enqueue(prompt(call(large)))
        await served.agent.settled()
        before = len(server.requests())
        served.agent.queue.enqueue(prompt(f"{ScenarioName.ANSWER.value} {'y' * 6_000}"))
        await served.agent.settled()
        await served.close()

        turn = server.requests()[before:]
        main = tool_names(turn[0])
        summaries = turn[1:]
        assert StandToolName.LARGE.full in main
        assert len(summaries) == 3
        for request in summaries:
            assert tool_names(request) == main
        assert served.agent.outcomes == [
            TurnOutcome.COMPLETED,
            TurnOutcome.BLOCKING_LIMIT,
        ]


class TestMatcher:
    """Поиск по словам — BM25 по имени, описанию и параметрам; `select:` —
    точные имена."""

    QUERY_SCHEMA: ClassVar[dict[str, object]] = {
        "type": "object",
        "properties": {
            "connection": {
                "type": "string",
                "description": "Warehouse connection to read from",
            },
            "options": {
                "type": "object",
                "properties": {"timeoutSec": {"type": "number"}},
            },
        },
    }

    def pool(self) -> list[SnapshotTool]:
        return [
            SnapshotTool(
                name="mcp__slack__send_message",
                description="Send a message to a Slack channel",
            ),
            SnapshotTool(name="mcp__slack__list_channels", description="List channels"),
            SnapshotTool(name="mcp__mail__send", description="Send an e-mail"),
            SnapshotTool(
                name="mcp__jupyter__notebookEdit", description="Edit a notebook cell"
            ),
            SnapshotTool(
                name="mcp__db__run",
                description="Run a statement",
                schema=self.QUERY_SCHEMA,
            ),
        ]

    def matched(self, query: str, max_results: int = 5) -> list[str]:
        asked = ToolSearchInput(query=query, max_results=max_results)

        return list(ToolMatcher().matches(asked, self.pool()))

    def test_select_keeps_the_order_and_drops_unknown(self) -> None:
        query = "select:mcp__mail__send, nope,mcp__slack__list_channels"
        assert self.matched(query) == [
            "mcp__mail__send",
            "mcp__slack__list_channels",
        ]

    def test_required_word_must_be_in_the_name(self) -> None:
        assert self.matched("+slack send") == [
            "mcp__slack__send_message",
            "mcp__slack__list_channels",
        ]

    def test_bm25_prefers_the_shorter_tool_and_rare_words(self) -> None:
        assert self.matched("send") == [
            "mcp__mail__send",
            "mcp__slack__send_message",
        ]
        assert self.matched("slack send")[0] == "mcp__slack__send_message"
        assert self.matched("notebook jupyter", 1) == ["mcp__jupyter__notebookEdit"]
        assert self.matched("nothing") == []

    def test_parameter_names_and_descriptions_are_searched(self) -> None:
        assert self.matched("warehouse") == ["mcp__db__run"]
        assert self.matched("connection") == ["mcp__db__run"]
        assert self.matched("timeout") == ["mcp__db__run"]

    def test_default_limit_is_five_and_the_index_follows_the_pool(self) -> None:
        matcher = ToolMatcher()
        many: list[SnapshotTool] = []
        for index in range(7):
            many.append(SnapshotTool(name=f"mcp__s__t{index}", description="report"))

        found = matcher.matches(ToolSearchInput(query="report"), many)
        assert len(found) == ToolSearchInput(query="x").max_results == 5

        renamed = [SnapshotTool(name="mcp__s__other", description="report")]
        assert list(matcher.matches(ToolSearchInput(query="report"), renamed)) == [
            "mcp__s__other"
        ]
