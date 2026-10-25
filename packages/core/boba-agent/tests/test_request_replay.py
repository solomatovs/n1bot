"""Запрос к модели собирается из истории заново байт в байт: агент пишет
каждый запрос записью api-request с digest, тест восстанавливает сессию из
записей журнала, стоящих до неё, собирает основной запрос сборщиком и
сравнивает digest. Запросы пересказа собираются с инструкцией, которой в
истории нет, — их digest не проверяется, но они считаются. Сценарии идут
по всему, что входит в запрос: инструменты серверов MCP и их инструкции,
отложенные инструменты и ToolSearch, скиллы, контекст приложения MCP Apps
и команды, которые клиент исполнил сам, промпты серверов MCP; смена
определения инструмента или формы запроса профиля между ходами пишется
новой записью, и запрос по-прежнему повторяется из истории."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path

import mcp_types as mt
import pytest

from boba.agent.attachments import Announced, ConnectedServer
from boba.agent.builder import RequestBuilder
from boba.agent.history import SessionId, StoredLine
from boba.agent.mcp import ToolCard, ToolReplies
from boba.agent.profile import ModelProfile, ReasoningReturn, TurnLimits
from boba.agent.queue import QueueEntry, QueueKind
from boba.agent.records import (
    ApiRequestRecord,
    AttachmentKind,
    AttachmentRecord,
    DeferredToolsRecordAttachment,
    PromptSnapshot,
    Record,
    RecordCodec,
    ToolResultRecord,
    TurnEndRecord,
    TurnOutcome,
)
from boba.agent.session import Session
from boba.agent.skills import Skill, SkillTool
from boba.agent.template import AgentTemplate, AgentTemplateFile
from boba.agent.tools import ToolSearchTool
from boba.agent.writer import ClientCommand
from boba.history.jsonl import JsonlHistoryStore
from boba.llm.chat import ChatRequest
from boba.stand.agent import (
    Collected,
    FakeServer,
    Protocol,
    Served,
    StandAgents,
    StandServer,
    StandServers,
    StandToolName,
    StandTools,
)
from boba.stand.ui.fake_llm import FailureName, ScenarioName

pytestmark = pytest.mark.anyio

SESSION = SessionId(value="request-replay-1")


APP_TEXT = "The user picked rows 3 and 5 in the table app."
APP_DATA = '{\n  "selected": [\n    3,\n    5\n  ]\n}'
INSTRUCTIONS = "Use the stand tools for lookups."
SKILL = Skill(
    name="deploy",
    description="Use deploy for deploy work",
    content="# deploy\n\nDo the deploy steps.",
)
REDESCRIBED = "stand tool lookup, described anew"


@dataclass(frozen=True)
class Scenario:
    """Ход разговора сценария: что ставится в очередь по порядку (и какие
    команды клиент исполнил сам между ходами), профиль модели, части
    агента и сколько запросов должно получиться. always_load None — все
    инструменты стенда уходят полным определением."""

    entries: Sequence[QueueEntry | ClientCommand]
    profile: Callable[[StandAgents], ModelProfile]
    main_requests: int
    summary_requests: int
    always_load: Sequence[str] | None = None
    skills: Sequence[Skill] = ()
    servers: Sequence[ConnectedServer] = ()


def prompt(text: str) -> QueueEntry:
    return QueueEntry(kind=QueueKind.PROMPT, text=text)


def call(name: str, arguments: dict[str, object]) -> str:
    spec = {"name": name, "arguments": arguments}

    return f"{ScenarioName.CALL.value} {json.dumps(spec)}"


def search(name: StandToolName) -> str:
    query = f"select:{name.full}"

    return call(ToolSearchTool.NAME, {"query": query, "max_results": 5})


def with_app_context() -> QueueEntry:
    """Вопрос вкладки с контекстом приложения MCP Apps (ui/update-model-context:
    текст и структурные данные — блоками только для модели) и ссылкой на
    файл пользователя."""
    link = mt.ResourceLink(
        name="rows.csv", uri="file:///workspace/rows.csv", mime_type="text/csv"
    )

    return QueueEntry(
        kind=QueueKind.PROMPT,
        text=ScenarioName.ANSWER.value,
        links=[link],
        context=[APP_TEXT, APP_DATA],
    )


def app_command() -> ClientCommand:
    """Окно приложения MCP, которое чат открыл сам и записал в историю."""
    return ClientCommand(
        message_id="app-command-1",
        name=StandToolName.LOOKUP.full,
        text=f"/{StandToolName.LOOKUP.full}",
        outcome={"content": [{"type": "text", "text": "lookup done"}]},
    )


def with_server_prompt() -> QueueEntry:
    """Вопрос командой промпта сервера MCP: текст prompts/get идёт
    служебным контекстом вопроса."""
    reply = mt.GetPromptResult(
        description="Review the change",
        messages=[
            mt.PromptMessage(role="user", content=mt.TextContent(text="Review: X")),
            mt.PromptMessage(
                role="assistant", content=mt.TextContent(text="I will review X.")
            ),
        ],
    )

    return QueueEntry(
        kind=QueueKind.PROMPT,
        text=ScenarioName.ANSWER.value,
        context=[ToolReplies().prompt(reply)],
    )


def openai(agents: StandAgents) -> ModelProfile:
    return agents.profile(Protocol.OPENAI)


def small_window(agents: StandAgents) -> ModelProfile:
    return agents.profile(Protocol.OPENAI, chars_per_token=4)


def system_turns(agents: StandAgents) -> ModelProfile:
    return agents.profile(
        Protocol.OLLAMA, system_turns=True, reasoning=ReasoningReturn.ALWAYS
    )


SCENARIOS = {
    "turns": Scenario(
        entries=(
            prompt(ScenarioName.ANSWER.value),
            prompt(ScenarioName.THINKING_ANSWER.value),
        ),
        profile=system_turns,
        main_requests=2,
        summary_requests=0,
    ),
    "tools": Scenario(
        entries=(
            prompt(call(StandToolName.LOOKUP.full, {"key": "a"})),
            prompt(call(StandToolName.BROKEN.full, {})),
            prompt(ScenarioName.ANSWER.value),
        ),
        profile=openai,
        main_requests=5,
        summary_requests=0,
    ),
    "retries": Scenario(
        entries=(
            prompt(f"{ScenarioName.ANSWER.value} {FailureName.STATUS_500.value}"),
            prompt(ScenarioName.ANSWER.value),
        ),
        profile=openai,
        main_requests=2,
        summary_requests=0,
    ),
    "output_limit": Scenario(
        entries=(prompt(f"{ScenarioName.ANSWER.value} {FailureName.LENGTH.value}"),),
        profile=openai,
        main_requests=4,
        summary_requests=0,
    ),
    "auto_compaction": Scenario(
        entries=(
            prompt(call(StandToolName.LARGE.full, {"size": 24_000, "fill": "x"})),
            prompt(ScenarioName.ANSWER.value),
            prompt(ScenarioName.ANSWER.value),
        ),
        profile=small_window,
        main_requests=4,
        summary_requests=1,
    ),
    "manual_compaction": Scenario(
        entries=(
            prompt(call(StandToolName.LARGE.full, {"size": 2_000, "fill": "x"})),
            prompt(ScenarioName.ANSWER.value),
            QueueEntry(kind=QueueKind.COMPACT, text="Keep the rows."),
            prompt(ScenarioName.ANSWER.value),
        ),
        profile=small_window,
        main_requests=4,
        summary_requests=1,
    ),
    "mcp_tools": Scenario(
        entries=(
            prompt(
                call(
                    StandToolName.QUERY.full,
                    {"connection": "pg", "statement": "select 1"},
                )
            ),
            prompt(call(StandToolName.LOOKUP.full, {"key": "a"})),
            prompt(ScenarioName.ANSWER.value),
        ),
        profile=openai,
        main_requests=5,
        summary_requests=0,
        servers=(
            ConnectedServer(name=StandServer.NAME.value, instructions=INSTRUCTIONS),
        ),
    ),
    "tool_search": Scenario(
        entries=(
            prompt(search(StandToolName.LOOKUP)),
            prompt(call(StandToolName.LOOKUP.full, {"key": "a"})),
            prompt(ScenarioName.ANSWER.value),
        ),
        profile=openai,
        main_requests=5,
        summary_requests=0,
        always_load=(),
    ),
    "skills": Scenario(
        entries=(
            prompt(call(SkillTool.NAME, {"skill": SKILL.name})),
            prompt(ScenarioName.ANSWER.value),
        ),
        profile=openai,
        main_requests=3,
        summary_requests=0,
        skills=(SKILL,),
    ),
    "app_context": Scenario(
        entries=(
            with_app_context(),
            app_command(),
            prompt(ScenarioName.ANSWER.value),
        ),
        profile=openai,
        main_requests=2,
        summary_requests=0,
    ),
    "server_prompt": Scenario(
        entries=(with_server_prompt(), prompt(ScenarioName.ANSWER.value)),
        profile=system_turns,
        main_requests=2,
        summary_requests=0,
    ),
}


class RedescribedTools(StandTools):
    """Сервер стенда, который посреди сессии меняет описание lookup, не
    меняя имени."""

    def redescribe(self) -> None:
        cards: list[ToolCard] = []
        for card in self._cards:
            if card.name != StandToolName.LOOKUP.full:
                cards.append(card)
                continue

            cards.append(
                ToolCard(
                    name=card.name,
                    description=REDESCRIBED,
                    parameters=card.parameters,
                    hints=card.hints,
                )
            )

        self._cards = cards


@pytest.fixture
async def server() -> AsyncIterator[FakeServer]:
    fake = FakeServer(estimate_usage=True)
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


class Replay:
    """Сборка основного запроса по записям журнала до записи api-request."""

    def __init__(self, profile: ModelProfile) -> None:
        self._templates = AgentTemplate()
        self._builder = RequestBuilder(profile)
        self._instruction = self._templates.read(
            AgentTemplateFile.SUMMARIZE_INSTRUCTION
        )

    def digest(self, before: Sequence[Record]) -> str:
        session = Session(SESSION)
        session.restore(before)
        conversation = session.conversation()
        snapshot = Announced(conversation).snapshot()
        assert snapshot is not None

        return self._digest_of(self._builder.build(conversation, snapshot))

    def is_summary(self, record: ApiRequestRecord) -> bool:
        """Запрос пересказа: последнее сообщение несёт инструкцию пересказа."""
        text = json.dumps(record.params, ensure_ascii=False)
        head = self._instruction.splitlines()[0]

        return json.dumps(head, ensure_ascii=False)[1:-1] in text

    @staticmethod
    def _digest_of(request: ChatRequest) -> str:
        params = request.model_dump(mode="json", exclude_none=True)
        canonical = json.dumps(params, ensure_ascii=False, sort_keys=True)

        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def journal(root: Path) -> list[Record]:
    codec = RecordCodec()
    records: list[Record] = []
    async with aclosing(JsonlHistoryStore(root).read(SESSION)) as lines:
        async for line in lines:
            assert isinstance(line, StoredLine)
            records.append(codec.parse(line.raw))

    return records


def checked(records: Sequence[Record], profile: ModelProfile) -> tuple[int, int]:
    """Каждый основной запрос журнала собран заново из записей до него с
    тем же digest; итог — число основных запросов и запросов пересказа."""
    replay = Replay(profile)
    main = 0
    summaries = 0
    for index, record in enumerate(records):
        if not isinstance(record, ApiRequestRecord):
            continue

        if replay.is_summary(record):
            summaries += 1
            continue

        main += 1
        assert replay.digest(records[:index]) == record.digest, index

    return main, summaries


def attachments(
    records: Sequence[Record], kind: AttachmentKind
) -> list[AttachmentRecord]:
    found: list[AttachmentRecord] = []
    for record in records:
        if not isinstance(record, AttachmentRecord):
            continue

        if record.attachment.type != kind:
            continue

        found.append(record)

    return found


def snapshots(records: Sequence[Record]) -> list[PromptSnapshot]:
    found: list[PromptSnapshot] = []
    for record in attachments(records, AttachmentKind.PROMPT_SNAPSHOT):
        assert isinstance(record.attachment, PromptSnapshot)
        found.append(record.attachment)

    return found


def definitions(records: Sequence[Record]) -> list[DeferredToolsRecordAttachment]:
    found: list[DeferredToolsRecordAttachment] = []
    for record in attachments(records, AttachmentKind.DEFERRED_TOOLS_RECORD):
        assert isinstance(record.attachment, DeferredToolsRecordAttachment)
        found.append(record.attachment)

    return found


def described(request: dict[str, object], name: str) -> str:
    """Описание инструмента name в теле запроса к модели."""
    tools = request["tools"]
    assert isinstance(tools, list)
    for tool in tools:
        function = tool["function"]
        if function["name"] == name:
            return function["description"]

    msg = f"request tools: expected {name!r} among them"
    raise AssertionError(msg)


class TestRequestsFromHistory:
    @pytest.mark.parametrize("name", list(SCENARIOS))
    async def test_every_request_is_rebuilt_from_the_records_before_it(
        self, agents: StandAgents, tmp_path: Path, name: str
    ) -> None:
        scenario = SCENARIOS[name]
        profile = scenario.profile(agents)
        servers = None
        if scenario.servers:
            servers = StandServers(scenario.servers)

        served = Served(
            agents.agent(
                profile,
                Collected(),
                tools=StandTools(),
                servers=servers,
                skills=scenario.skills,
                always_load=scenario.always_load,
                record_requests=True,
            )
        )
        await served.open(SESSION, resume=False)
        for entry in scenario.entries:
            if isinstance(entry, ClientCommand):
                await served.agent.record_command(entry)
                continue

            served.agent.queue.enqueue(entry)
            await served.agent.settled()

        await served.close()

        records = await journal(tmp_path / "history")
        assert checked(records, profile) == (
            scenario.main_requests,
            scenario.summary_requests,
        ), name


class TestDefinitionChanges:
    async def test_a_tool_described_anew_is_a_new_snapshot(
        self, server: FakeServer, agents: StandAgents, tmp_path: Path
    ) -> None:
        """Сервер сменил описание инструмента с полным определением, не
        меняя имени: следующий ход пишет новый снимок с прежним системным
        промптом, и запрос несёт новое описание."""
        profile = openai(agents)
        tools = RedescribedTools()
        served = Served(
            agents.agent(profile, Collected(), tools=tools, record_requests=True)
        )
        await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(call(StandToolName.LOOKUP.full, {})))
        await served.agent.settled()
        tools.redescribe()
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        records = await journal(tmp_path / "history")
        written = snapshots(records)
        assert len(written) == 2
        assert written[1].system_prompt == written[0].system_prompt
        lookup = StandToolName.LOOKUP.full
        requests = server.requests()
        assert (
            described(requests[1], lookup) == f"stand tool {StandToolName.LOOKUP.value}"
        )
        assert described(requests[-1], lookup) == REDESCRIBED
        assert checked(records, profile) == (3, 0)

    async def test_a_new_request_shape_of_the_profile_is_a_new_snapshot(
        self, server: FakeServer, agents: StandAgents, tmp_path: Path
    ) -> None:
        """Смена профиля между ходами меняет форму запроса — подачу
        вложений и возврат рассуждений: она пишется новым снимком, и
        сборщик берёт её из снимка, а не из профиля."""
        first = openai(agents)
        served = Served(
            agents.agent(first, Collected(), tools=StandTools(), record_requests=True)
        )
        await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(ScenarioName.THINKING_ANSWER.value))
        await served.agent.settled()
        second = agents.profile(
            Protocol.OPENAI, system_turns=True, reasoning=ReasoningReturn.ALWAYS
        )
        served.agent.set_model(second, agents.model(second))
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        records = await journal(tmp_path / "history")
        written = snapshots(records)
        assert [(s.system_turns, s.reasoning_return) for s in written] == [
            (False, ReasoningReturn.NEVER),
            (True, ReasoningReturn.ALWAYS),
        ]
        messages = server.requests()[-1]["messages"]
        assert isinstance(messages, list)
        assert [m["role"] for m in messages].count("system") > 1
        assert checked(records, first) == (2, 0)

    async def test_a_search_records_what_it_loaded_before_any_request(
        self, server: FakeServer, agents: StandAgents, tmp_path: Path
    ) -> None:
        """ToolSearch загрузил lookup, и ход кончился раньше следующего
        запроса: определение уже записано тем, каким его нашёл поиск. При
        возобновлении сервер описывает lookup иначе — это новая запись, и
        запрос собирается из истории."""
        profile = openai(agents)
        limits = TurnLimits(
            max_turns=1, retry_delays_sec=(0.0, 0.0, 0.0), tool_result_window_share=1.0
        )
        served = Served(
            agents.agent(
                profile,
                Collected(),
                limits=limits,
                tools=StandTools(),
                always_load=(),
                record_requests=True,
            )
        )
        await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(search(StandToolName.LOOKUP)))
        await served.agent.settled()
        await served.close()

        cut = await journal(tmp_path / "history")
        ends = [r for r in cut if isinstance(r, TurnEndRecord)]
        assert [end.outcome for end in ends] == [TurnOutcome.MAX_TURNS]
        results = [r for r in cut if isinstance(r, ToolResultRecord)]
        assert len(results) == 1
        loaded = definitions(cut[cut.index(results[0]) :])
        assert len(loaded) == 1
        assert [(t.name, t.description) for t in loaded[0].entries] == [
            (StandToolName.LOOKUP.full, f"stand tool {StandToolName.LOOKUP.value}")
        ]

        tools = RedescribedTools()
        tools.redescribe()
        resumed = Served(
            agents.agent(
                profile,
                Collected(),
                tools=tools,
                always_load=(),
                record_requests=True,
            )
        )
        await resumed.open(SESSION, resume=True)
        resumed.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await resumed.agent.settled()
        await resumed.close()

        records = await journal(tmp_path / "history")
        recorded = definitions(records)
        assert [t.description for t in recorded[-1].entries] == [REDESCRIBED]
        assert len(recorded) == 2
        assert (
            described(server.requests()[-1], StandToolName.LOOKUP.full) == REDESCRIBED
        )
        assert checked(records, profile) == (2, 0)
