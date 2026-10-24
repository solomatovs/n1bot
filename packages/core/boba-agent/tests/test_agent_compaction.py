"""Сжатие на стенде: порог даёт границу и пересказ одной пачкой, следующий
запрос начинается с пересказа и несёт хвост, возобновление даёт тот же
запрос; неудачи пересказа и предохранители; лестница при переполнении на
запросе пересказа; отмена и `now` во время сжатия; обрыв журнала за
границей; правило и режим переживают два сжатия; порог без учёта токенов на
кириллице; системный промпт больше половины порога — отказ на старте."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import ClassVar

import pytest

from boba.agent.attachments import ConnectedServer, InvokedSkillsAttachment
from boba.agent.control import AllowAnswer
from boba.agent.events import (
    RecordEvent,
    TurnOutcome,
)
from boba.agent.history import SessionId, StoredLine
from boba.agent.ids import IdMint, SystemClock
from boba.agent.loader import HistoryLoader
from boba.agent.profile import CompactionSettings, ProfileError
from boba.agent.queue import QueueEntry, QueueKind, QueuePriority
from boba.agent.records import (
    ApiErrorRecord,
    AssistantRecord,
    AttachmentRecord,
    ChainRecord,
    CompactBoundaryRecord,
    CompactionPhase,
    CompactionRecord,
    CompactTrigger,
    PermissionBehavior,
    PermissionMode,
    PermissionRule,
    Record,
    RecordCodec,
    TextBlock,
    UserRecord,
)
from boba.agent.session import Session, SessionState
from boba.agent.skills import Skill, SkillTool
from boba.agent.template import AgentTemplate, AgentTemplateFile
from boba.history.jsonl import JsonlHistoryStore
from boba.stand.agent import (
    Collected,
    FakeServer,
    Protocol,
    Served,
    StandAgents,
    StandServers,
    StandToolName,
    StandTools,
)
from boba.stand.ui.fake_llm import (
    FailureName,
    ScenarioName,
    SummaryName,
    WindowSettings,
    WireText,
)

pytestmark = pytest.mark.anyio

SESSION = SessionId(value="agent-compaction-1")
VERSION = "0.0.26.dev4"
CONTINUED = "This session is being continued from a previous conversation"
INSTRUCTION_MARK = "an <analysis> block followed by a <summary> block"
NOTE = "Look the rows up before you drop them."
TRUNCATED_FOR_RETRY = "[earlier conversation truncated for compaction retry]"
THREAD_EXHAUSTED = (
    "This conversation no longer fits the context window of the model, even after "
    "compaction"
)
HEAD_NOT_COVERED = "is NOT covered by this summary"
SKILL_BODY = "Always answer the stand in exactly three bullet points."
INVOKED_MARK = "The following skills were invoked EARLIER in this session"
LARGE_CHARS = 24_000
"""Результат, после которого учёт следующего ответа переходит порог
(80 % окна 8000 минус 512), но не предел (95 %)."""


def prompt(text: str, priority: QueuePriority | None = None) -> QueueEntry:
    return QueueEntry(kind=QueueKind.PROMPT, text=text, priority=priority)


def call(*specs: dict[str, object]) -> str:
    if len(specs) == 1:
        return f"{ScenarioName.CALL.value} {json.dumps(specs[0])}"

    return f"{ScenarioName.CALL.value} {json.dumps(list(specs))}"


def large(size: int = LARGE_CHARS, fill: str = "x") -> dict[str, object]:
    return {
        "name": StandToolName.LARGE.full,
        "arguments": {"size": size, "fill": fill},
    }


def loop(size: int) -> str:
    return f"{ScenarioName.LOOP.value} {json.dumps(large(size))}"


def kinds(records: Sequence[Record] | Sequence[ChainRecord]) -> list[str]:
    names: list[str] = []
    for record in records:
        name = record.type.value
        if isinstance(record, AttachmentRecord):
            name = f"attachment:{record.attachment.type}"

        if isinstance(record, AssistantRecord):
            name = f"assistant:{record.message.content[0].type}"
            if record.is_synthetic():
                name = "assistant:synthetic"

        if isinstance(record, UserRecord):
            if record.message.tool_results():
                name = "user:tool_result"

            if record.is_compact_summary:
                name = "user:summary"

        if isinstance(record, CompactBoundaryRecord):
            name = "system:compact_boundary"

        if isinstance(record, ApiErrorRecord):
            name = "system:api_error"

        if isinstance(record, CompactionRecord):
            name = "system:compaction"

        names.append(name)

    return names


def boundaries(records: Sequence[Record]) -> list[CompactBoundaryRecord]:
    found: list[CompactBoundaryRecord] = []
    for record in records:
        if isinstance(record, CompactBoundaryRecord):
            found.append(record)

    return found


def summaries(records: Sequence[Record]) -> list[UserRecord]:
    found: list[UserRecord] = []
    for record in records:
        if isinstance(record, UserRecord) and record.is_compact_summary:
            found.append(record)

    return found


def messages(server: FakeServer, index: int) -> list[dict[str, object]]:
    found = server.requests()[index]["messages"]
    assert isinstance(found, list)

    return found


def is_summary_request(request: dict[str, object]) -> bool:
    found = request["messages"]
    assert isinstance(found, list)
    last = found[-1]
    if last["role"] != "user":
        return False

    content = WireText().of(last["content"])

    return content is not None and INSTRUCTION_MARK in content


def summary_requests(server: FakeServer) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for request in server.requests():
        if is_summary_request(request):
            found.append(request)

    return found


def compaction_records(records: Sequence[Record]) -> list[CompactionRecord]:
    found: list[CompactionRecord] = []
    for record in records:
        if isinstance(record, CompactionRecord):
            found.append(record)

    return found


def phases(records: Sequence[Record]) -> list[tuple[str, CompactionPhase]]:
    """Записи хода сжатия по порядку: (id сжатия, фаза)."""
    found: list[tuple[str, CompactionPhase]] = []
    for record in compaction_records(records):
        found.append((record.compaction_id, record.phase))

    return found


def synthetic_texts(records: Sequence[Record]) -> list[str]:
    texts: list[str] = []
    for record in records:
        if not isinstance(record, AssistantRecord):
            continue

        if not record.is_api_error_message:
            continue

        for block in record.message.content:
            if isinstance(block, TextBlock):
                texts.append(block.text)

    return texts


async def reloaded(root: Path, session_id: SessionId) -> Session:
    store = JsonlHistoryStore(root)
    session = Session(session_id)
    await HistoryLoader(store, AgentTemplate(), IdMint(), SystemClock(), VERSION).load(
        session
    )

    return session


async def journal(root: Path, session_id: SessionId) -> list[Record]:
    """Все записи журнала сессии по порядку. Память сессии после сжатия держит
    только нужное модели, поэтому историю целиком тесты читают отсюда."""
    store = JsonlHistoryStore(root)
    codec = RecordCodec()
    found: list[Record] = []
    async for line in store.read(session_id):
        assert isinstance(line, StoredLine), line
        found.append(codec.parse(line.raw))

    await store.close()

    return found


async def wait_state(session: Session, state: SessionState) -> None:
    deadline = time.monotonic() + 3.0
    while session.state is not state:
        assert time.monotonic() < deadline, f"session never entered {state.value}"
        await asyncio.sleep(0.005)


@pytest.fixture
async def server() -> AsyncIterator[FakeServer]:
    """Стенд с учётом токенов по размеру запроса: якорь заполненности —
    настоящий учёт, как у провайдера."""
    fake = FakeServer(estimate_usage=True)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def plain_server() -> AsyncIterator[FakeServer]:
    """Стенд с постоянным учётом 11 + 7: заполненность без учёта — через
    `failure:no-usage`."""
    fake = FakeServer()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def hanging_server() -> AsyncIterator[FakeServer]:
    """Стенд с учётом по размеру и коротким зависанием: пересказ по маркеру
    `summary:hang` отвечает через 0,3 с — тест успевает прервать ход, а
    следующий ход без прерывания получает пересказ."""
    fake = FakeServer(estimate_usage=True, hang_sec=0.3)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def hanging_agents(
    hanging_server: FakeServer, tmp_path: Path
) -> AsyncIterator[StandAgents]:
    stand = StandAgents(hanging_server, tmp_path / "history")
    try:
        yield stand
    finally:
        await stand.aclose()


@pytest.fixture
async def agents(server: FakeServer, tmp_path: Path) -> AsyncIterator[StandAgents]:
    stand = StandAgents(server, tmp_path / "history")
    try:
        yield stand
    finally:
        await stand.aclose()


@pytest.fixture
async def plain_agents(
    plain_server: FakeServer, tmp_path: Path
) -> AsyncIterator[StandAgents]:
    stand = StandAgents(plain_server, tmp_path / "history")
    try:
        yield stand
    finally:
        await stand.aclose()


class TestThreshold:
    @pytest.mark.parametrize("protocol", list(Protocol))
    async def test_crossing_the_threshold_writes_boundary_and_summary(
        self,
        server: FakeServer,
        agents: StandAgents,
        tmp_path: Path,
        protocol: Protocol,
    ) -> None:
        sink = Collected()
        profile = agents.profile(protocol, chars_per_token=4)
        served = Served(agents.agent(profile, sink, tools=StandTools()))
        session = await served.open(SESSION, resume=False)
        root = tmp_path / "history"

        served.agent.queue.enqueue(prompt(call(large())))
        await served.agent.settled()
        before = len(await journal(root, SESSION))
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.COMPLETED] * 2
        self.check_records(session, await journal(root, SESSION), before)
        events = [e for e in sink.of(RecordEvent) if isinstance(e, RecordEvent)]
        assert any(isinstance(e.record, CompactBoundaryRecord) for e in events)
        written = await journal(root, SESSION)
        summary = summaries(written)[0]
        assert phases(written) == [(summary.uuid, CompactionPhase.STARTED)]
        started = compaction_records(written)[0]
        assert started.trigger is CompactTrigger.AUTO
        assert started.command_uuid is None
        after = self.check_requests(server)

        again = await reloaded(root, SESSION)
        assert list(again.records()) == list(session.records())
        assert list(again.conversation()) == list(session.conversation())

        resumed = Served(agents.agent(profile, Collected(), tools=StandTools()))
        await resumed.open(SESSION, resume=True)
        resumed.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await resumed.agent.settled()
        await resumed.close()

        assert len(server.requests()) == 5
        assert messages(server, 4)[: len(after)] == after
        assert len(boundaries(resumed.agent.session.records())) == 1

    async def test_compaction_announces_the_server_instructions_again(
        self, agents: StandAgents, tmp_path: Path
    ) -> None:
        """Пачка сжатия объявляет инструкции подключённых серверов MCP
        заново вслед за моделью: прежнее вложение осталось за границей.
        Каждый ход перед объявлениями освежает серверы."""
        sink = Collected()
        profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
        servers = StandServers([ConnectedServer(name="stand", instructions=NOTE)])
        agent = agents.agent(profile, sink, tools=StandTools(), servers=servers)
        served = Served(agent)
        await served.open(SESSION, resume=False)
        root = tmp_path / "history"

        served.agent.queue.enqueue(prompt(call(large())))
        await served.agent.settled()
        before = len(await journal(root, SESSION))
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        records = await journal(root, SESSION)
        announced = kinds(records[before:])
        first = kinds(records[:before])
        assert first.count("attachment:mcp_instructions_delta") == 1, first
        assert announced[3:9] == [
            "system:compaction",
            "system:compact_boundary",
            "attachment:date",
            "user:summary",
            "attachment:mcp_instructions_delta",
            "attachment:model",
        ], announced
        assert servers.refreshes == 2

    @staticmethod
    def check_records(session: Session, records: Sequence[Record], before: int) -> None:
        """Пачка журнала после второго промпта, границы и проекция разговора."""
        assert kinds(records[before:]) == [
            "queue-operation",
            "queue-operation",
            "user",
            "system:compaction",
            "system:compact_boundary",
            "attachment:date",
            "user:summary",
            "attachment:model",
            "attachment:prompt_snapshot",
            "assistant:text",
        ]

        boundary = boundaries(records)[0]
        summary = summaries(records)[0]
        prompt_two = records[before + 2]
        started = records[before + 3]
        date = records[before + 5]
        assert isinstance(prompt_two, UserRecord)
        assert isinstance(started, CompactionRecord)
        assert started.parent_uuid == prompt_two.uuid
        assert started.compaction_id == summary.uuid
        assert isinstance(date, AttachmentRecord)
        assert boundary.parent_uuid is None
        assert boundary.logical_parent_uuid == prompt_two.uuid
        assert summary.parent_uuid == date.uuid
        assert summary.is_visible_in_transcript_only is True
        assert isinstance(summary.message.content, str)
        assert summary.message.content.startswith(CONTINUED)
        assert "Summary:\n1. Primary Request and Intent:" in summary.message.content
        assert "analysis" not in summary.message.content
        metadata = boundary.compact_metadata
        assert metadata.preserved_messages.anchor_uuid == summary.uuid
        chain: list[ChainRecord] = []
        for record in records:
            if isinstance(record, ChainRecord):
                chain.append(record)

        answer_one = chain[chain.index(prompt_two) - 1]
        assert isinstance(answer_one, AssistantRecord)
        assert list(metadata.preserved_messages.uuids) == [
            answer_one.uuid,
            prompt_two.uuid,
        ]
        assert metadata.pre_tokens > metadata.post_tokens > 0
        assert metadata.duration_ms >= 0

        conversation = session.conversation()
        assert kinds(conversation[:7]) == [
            "system:compact_boundary",
            "attachment:date",
            "user:summary",
            "assistant:text",
            "user",
            "attachment:model",
            "attachment:prompt_snapshot",
        ]
        tail_answer = conversation[3]
        assert isinstance(tail_answer, AssistantRecord)
        assert tail_answer.message.usage.is_zero()

    @staticmethod
    def check_requests(server: FakeServer) -> list[dict[str, object]]:
        """Запрос пересказа и первый запрос после сжатия; возвращает второй."""
        requests = server.requests()
        assert len(requests) == 4
        summary_request = requests[2]
        assert is_summary_request(summary_request)
        assert summary_request["tools"] == requests[0]["tools"]
        summary_messages = messages(server, 2)
        assert summary_messages[0] == messages(server, 0)[0]
        assert [m["role"] for m in summary_messages[-2:]] == ["tool", "user"]

        after = messages(server, 3)
        assert after[0] == messages(server, 0)[0]
        assert [m["role"] for m in after] == ["system", "user", "assistant", "user"]
        first_user = WireText().of(after[1]["content"])
        assert first_user is not None
        assert first_user.index("Today's date is") < first_user.index(CONTINUED)
        assert ScenarioName.CALL.value in first_user
        assert after[2]["content"] == "the tool has answered"
        last_user = WireText().of(after[3]["content"])
        assert last_user is not None
        assert last_user.endswith(ScenarioName.ANSWER.value)

        return after

    async def test_fill_after_the_boundary_counts_the_prompt_and_tools(
        self, agents: StandAgents
    ) -> None:
        """Сразу после сжатия ответа с учётом ещё нет: заполненность — оценка
        записей после границы вместе с системным промптом и инструментами.
        Запрос после сжатия падает статусом, и оценка остаётся на границе."""
        profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
        system_prompt = ("x" * 4_000,)
        served = Served(
            agents.agent(
                profile,
                Collected(),
                tools=StandTools(),
                system_prompt=system_prompt,
            )
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(call(large(20_000))))
        await served.agent.settled()
        failing = f"{FailureName.STATUS_500.value} {ScenarioName.ANSWER.value}"
        served.agent.queue.enqueue(prompt(failing))
        await served.agent.settled()
        usage = served.agent.context_usage()
        await served.close()

        assert list(served.agent.outcomes) == [
            TurnOutcome.COMPLETED,
            TurnOutcome.API_ERROR,
        ]
        conversation = session.conversation()
        assert isinstance(conversation[0], CompactBoundaryRecord)
        post = conversation[0].compact_metadata.post_tokens
        assert usage.used >= post + profile.estimate(system_prompt[0])

    async def test_cyrillic_text_without_usage_compacts_before_the_limit(
        self, plain_server: FakeServer, plain_agents: StandAgents
    ) -> None:
        sink = Collected()
        served = Served(
            plain_agents.agent(
                plain_agents.profile(Protocol.OPENAI), sink, tools=StandTools()
            )
        )
        session = await served.open(SESSION, resume=False)

        first = f"{FailureName.NO_USAGE.value} {call(large(15_500, 'ы'))}"
        served.agent.queue.enqueue(prompt(first))
        await served.agent.settled()
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.COMPLETED] * 2
        records = session.records()
        assert len(boundaries(records)) == 1
        assert not synthetic_texts(records)
        assert len(summary_requests(plain_server)) == 1
        assert boundaries(records)[0].compact_metadata.pre_tokens > 5_900


class TestBreakers:
    @pytest.mark.parametrize(
        "failure",
        [
            SummaryName.TOOL_CALL,
            SummaryName.EMPTY,
            SummaryName.LONG,
            SummaryName.LENGTH,
        ],
    )
    async def test_three_failed_summaries_open_the_breaker(
        self, plain_server: FakeServer, plain_agents: StandAgents, failure: SummaryName
    ) -> None:
        """Каждая неудача пересказа считается; после трёх подряд попытки
        прекращаются, и ход, дошедший до предела, кончается названным исходом.

        Шаг цикла — четверть порога сжатия: два раунда занимают не больше его
        половины, поэтому порог переходится не раньше третьего раунда, когда
        пересказу уже есть что сжимать, а три попытки остаются ниже предела."""
        sink = Collected()
        profile = plain_agents.profile(Protocol.OPENAI)
        settings = CompactionSettings(compact_at=0.3)
        instruction = AgentTemplate().read(AgentTemplateFile.SUMMARIZE_INSTRUCTION)
        size = settings.threshold(profile, instruction) * profile.chars_per_token // 4
        served = Served(
            plain_agents.agent(profile, sink, tools=StandTools(), compaction=settings)
        )
        session = await served.open(SESSION, resume=False)

        text = f"{failure.value} {FailureName.NO_USAGE.value} {loop(size)}"
        served.agent.queue.enqueue(prompt(text))
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.COMPACT_FAILURE_BREAKER]
        records = session.records()
        steps = [phase for _, phase in phases(records)]
        assert steps == [CompactionPhase.STARTED, CompactionPhase.FAILED] * 3
        assert all(r.error for r in compaction_records(records)[1::2])
        assert not boundaries(records)
        assert len(summary_requests(plain_server)) == 3
        texts = synthetic_texts(records)
        assert len(texts) == 1
        assert texts[0].startswith(
            "This conversation no longer fits in the model's memory, and "
            "shortening it automatically didn't help."
        )
        errors: list[str] = []
        for record in records:
            if isinstance(record, ApiErrorRecord):
                errors.append(record.error)

        if failure is SummaryName.LONG:
            assert errors[-1].startswith(THREAD_EXHAUSTED), errors
        else:
            assert errors[-1].startswith(
                "Prompt is too long. Automatic compaction failed:"
            ), errors

        assert kinds(records[-3:]) == [
            "user:tool_result",
            "system:api_error",
            "assistant:synthetic",
        ]

    async def test_rapid_refill_trips_the_breaker(
        self, plain_server: FakeServer, plain_agents: StandAgents, tmp_path: Path
    ) -> None:
        """Сжатие удаётся, но результат каждого шага сам больше порога: три
        быстрых перезаполнения подряд кончают ход."""
        sink = Collected()
        served = Served(
            plain_agents.agent(
                plain_agents.profile(Protocol.OPENAI),
                sink,
                tools=StandTools(),
                compaction=CompactionSettings(compact_at=0.3),
            )
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(
            prompt(f"{FailureName.NO_USAGE.value} {loop(8_000)}")
        )
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.RAPID_REFILL_BREAKER]
        records = await journal(tmp_path / "history", SESSION)
        assert len(boundaries(records)) == 3
        assert len(summaries(records)) == 3
        assert len(summary_requests(plain_server)) == 3
        texts = synthetic_texts(records)
        assert len(texts) == 1
        assert texts[0].startswith(
            "This conversation fills the model's memory again right after each "
            "shortening"
        )
        conversation = session.conversation()
        assert isinstance(conversation[0], CompactBoundaryRecord)
        assert conversation[0].uuid == boundaries(records)[-1].uuid


class TestLadder:
    """Лестница пересказа (docs/prompt_assembly/specs/04_compaction.md, 2.2):
    переполнение запроса пересказа переносит последние группы в хвост, голова
    не выбрасывается; последний рубеж — всё до последнего вопроса человека, и
    только он отбрасывает старшие группы."""

    async def test_overflowing_summary_request_moves_rounds_into_the_tail(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        """Беседа из многих раундов упирается в окно сервера; пересказ всего,
        кроме последней группы, не влезает, и названное сервером превышение
        переносит в хвост столько последних групп, сколько его покрывает:
        второй запрос пересказа — начало первого, пометки об усечении нет."""
        server.app.limit_window(WindowSettings(context_window=5_000, chars_per_token=4))
        profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
        served = Served(agents.agent(profile, Collected(), tools=StandTools()))
        session = await served.open(SESSION, resume=False)

        for _turn in range(12):
            served.agent.queue.enqueue(prompt(call(large(2_400))))
            await served.agent.settled()
            if boundaries(session.records()):
                break

        await served.close()

        assert set(served.agent.outcomes) == {TurnOutcome.COMPLETED}
        attempts = summary_requests(server)
        assert len(attempts) == 2
        first = attempts[0]["messages"]
        second = attempts[1]["messages"]
        assert isinstance(first, list)
        assert isinstance(second, list)
        assert len(second) < len(first)
        assert second[:-1] == first[: len(second) - 1]
        assert TRUNCATED_FOR_RETRY not in json.dumps(second)
        records = session.records()
        summary = summaries(records)[0].message.content
        assert isinstance(summary, str)
        assert HEAD_NOT_COVERED not in summary
        kept = boundaries(records)[0].compact_metadata.preserved_messages.uuids
        assert len(kept) > 2

    async def test_last_resort_keeps_the_prompt_and_drops_the_head(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        """Сервер отверг запрос, а пересказ по раундам не влезает, пока в нём
        есть ответ модели: последний рубеж пересказывает всё до последнего
        вопроса и отбрасывает старшие группы с пометкой; вопрос остаётся
        хвостом, и ход доходит до ответа."""
        server.app.limit_window(WindowSettings(context_window=5_000, chars_per_token=4))
        profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
        served = Served(agents.agent(profile, Collected(), tools=StandTools()))
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(call(large(14_000))))
        await served.agent.settled()
        long_prompt = f"{ScenarioName.ANSWER.value} {'y' * 6_000}"
        served.agent.queue.enqueue(prompt(long_prompt))
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.COMPLETED] * 2
        attempts = summary_requests(server)
        last = attempts[-1]["messages"]
        assert isinstance(last, list)
        assert WireText().of(last[1]["content"]) == TRUNCATED_FOR_RETRY
        records = session.records()
        assert len(boundaries(records)) == 1
        summary = summaries(records)[0].message.content
        assert isinstance(summary, str)
        assert HEAD_NOT_COVERED in summary
        kept = boundaries(records)[0].compact_metadata.preserved_messages.uuids
        prompts = [
            r
            for r in records
            if isinstance(r, UserRecord) and r.message.content == long_prompt
        ]
        assert kept[0] == prompts[0].uuid


class TestWindow:
    """Стенд с окном модели: запрос больше окна сервер отвергает ошибкой
    переполнения, как OpenAI-совместимый сервер."""

    async def test_summary_request_fits_the_window_at_the_threshold(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        """Заполненность растёт шагами, сервер считает так же, как агент, и его
        окно — действующее окно профиля: порог оставляет место под
        инструкцию пересказа, и запрос пересказа влезает с первой попытки —
        лестница не отбрасывает начало разговора."""
        profile = agents.profile(Protocol.OPENAI)
        server.app.limit_window(
            WindowSettings(
                context_window=profile.effective_window(),
                chars_per_token=profile.chars_per_token,
            )
        )
        served = Served(agents.agent(profile, Collected(), tools=StandTools()))
        session = await served.open(SESSION, resume=False)

        for text in [
            call(large(8_000)),
            call(large(8_000)),
            ScenarioName.ANSWER.value,
            call(large(1_500)),
            ScenarioName.ANSWER.value,
        ]:
            served.agent.queue.enqueue(prompt(text))
            await served.agent.settled()

        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.COMPLETED] * 5
        records = session.records()
        assert boundaries(records)
        assert len(summary_requests(server)) == len(boundaries(records))
        for summary in summaries(records):
            assert isinstance(summary.message.content, str)
            assert HEAD_NOT_COVERED not in summary.message.content

    async def test_overflow_mid_turn_compacts_and_repeats_the_request(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        """Оценка агента ниже счёта сервера: перед запросом порог не перейдён,
        сервер отвечает переполнением — сжатие и один повтор, ход доходит до
        ответа без записи об ошибке."""
        server.app.limit_window(WindowSettings(context_window=6_000, chars_per_token=2))
        profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
        served = Served(agents.agent(profile, Collected(), tools=StandTools()))
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(call(large(3_000))))
        await served.agent.settled()
        before = len(server.requests())
        served.agent.queue.enqueue(prompt(call(large(9_000))))
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.COMPLETED] * 2
        records = session.records()
        assert len(boundaries(records)) == 1
        assert not synthetic_texts(records)
        assert "system:api_error" not in kinds(records)
        turn = server.requests()[before:]
        assert [is_summary_request(r) for r in turn] == [False, False, True, False]
        overflowed, retried = turn[1], turn[3]
        assert overflowed["tools"] == retried["tools"]
        retried_messages = retried["messages"]
        overflowed_messages = overflowed["messages"]
        assert isinstance(retried_messages, list)
        assert isinstance(overflowed_messages, list)
        assert retried_messages[0] == overflowed_messages[0]
        first_user = WireText().of(retried_messages[1]["content"])
        assert first_user is not None
        assert CONTINUED in first_user
        assert [m["role"] for m in retried_messages[-2:]] == ["assistant", "tool"]

    async def test_tail_over_the_window_ends_each_turn_without_a_loop(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        """Последняя группа — результат инструмента больше окна: сжатие
        переносит его хвостом, повтор снова не влезает. Ход кончается
        пределом после одного сжатия и одного повтора; следующий ход — после
        одного запроса: пересказывать нечего, и повторов нет."""
        server.app.limit_window(WindowSettings(context_window=6_000, chars_per_token=2))
        profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
        served = Served(agents.agent(profile, Collected(), tools=StandTools()))
        session = await served.open(SESSION, resume=False)

        counts: list[int] = []
        for text in [
            ScenarioName.ANSWER.value,
            call(large(14_000)),
            ScenarioName.ANSWER.value,
        ]:
            before = len(server.requests())
            served.agent.queue.enqueue(prompt(text))
            await served.agent.settled()
            counts.append(len(server.requests()) - before)

        await served.close()

        assert list(served.agent.outcomes) == [
            TurnOutcome.COMPLETED,
            TurnOutcome.BLOCKING_LIMIT,
            TurnOutcome.BLOCKING_LIMIT,
        ]
        assert counts == [1, 4, 2]
        records = session.records()
        assert len(boundaries(records)) == 1
        texts = synthetic_texts(records)
        assert texts == [
            "This conversation no longer fits in the model's memory. Start a new "
            "chat to continue.",
            "This conversation no longer fits in the model's memory, and shortening "
            "it automatically didn't help. Start a new chat to continue.",
        ]
        causes: list[str | None] = []
        for record in records:
            if isinstance(record, AssistantRecord) and record.is_api_error_message:
                causes.append(record.error)

        assert causes[0] is None
        assert causes[1] is not None
        assert causes[1].startswith(
            "the summary request does not fit the context window even after "
            "dropping the oldest rounds"
        ), causes


class TestInvokedSkills:
    async def test_skill_called_before_the_boundary_comes_back_after_it(
        self, server: FakeServer, agents: StandAgents, tmp_path: Path
    ) -> None:
        """Скилл, вызванный в пересказанной части, возвращается вложением
        invoked_skills с текстом своего результата — вслед за пересказом, до
        модели; второе сжатие переносит его из прежнего вложения."""
        profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
        skill = Skill(name="stand-skill", description="Stand skill", content=SKILL_BODY)
        served = Served(
            agents.agent(profile, Collected(), tools=StandTools(), skills=[skill])
        )
        await served.open(SESSION, resume=False)

        skill_call = {"name": SkillTool.NAME, "arguments": {"skill": skill.name}}
        for text in [
            call(skill_call),
            call(large()),
            ScenarioName.ANSWER.value,
            call(large()),
            ScenarioName.ANSWER.value,
        ]:
            served.agent.queue.enqueue(prompt(text))
            await served.agent.settled()

        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.COMPLETED] * 5
        records = await journal(tmp_path / "history", SESSION)
        found = boundaries(records)
        assert len(found) >= 2
        for boundary in found:
            pack = kinds(records[records.index(boundary) :])
            assert pack[:4] == [
                "system:compact_boundary",
                "attachment:date",
                "user:summary",
                "attachment:invoked_skills",
            ], pack
            invoked = records[records.index(boundary) + 3]
            assert isinstance(invoked, AttachmentRecord)
            attachment = InvokedSkillsAttachment.model_validate(
                invoked.attachment.model_dump()
            )
            assert [s.name for s in attachment.skills] == [skill.name]
            assert SKILL_BODY in attachment.skills[0].content

        after = json.dumps(server.requests()[-1]["messages"], ensure_ascii=False)
        assert INVOKED_MARK in after
        assert SKILL_BODY in after


CAVEAT = (
    "<local-command-caveat>The command below was run directly in Boba, not sent "
    "to you as a request, and its output goes straight to the user. It's recorded "
    "here as context for later messages.</local-command-caveat>"
)
"""Пометка локальной команды для модели — как в эталоне
(texts/compact/local_command_records_manual.txt), с именем агента."""


class TestManual:
    """Сжатие по команде пользователя: элемент очереди compact — свой ход
    без записи вопроса; указания пользователя уходят в инструкцию блоком
    «Additional Instructions:»; граница — с триггером manual."""

    NOTE: ClassVar[str] = "Keep only the table names."
    CALL: ClassVar[str] = (
        "<command-name>/compact</command-name>\n"
        "            <command-message>compact</command-message>\n"
        f"            <command-args>{NOTE}</command-args>"
    )

    def _check_command_records(
        self, records: Sequence[Record], before: int, command_id: str
    ) -> None:
        """За прежними before записями — пометка и вызов команды с uuid
        command_id, до границы; запись начала сжатия ссылается на вызов, id
        сжатия — uuid пересказа."""
        written = [r for r in records[before:] if isinstance(r, UserRecord)]
        caveat, called = written[:2]
        assert caveat.is_meta
        assert caveat.message.content == CAVEAT
        assert called.uuid == command_id
        assert called.message.content == self.CALL
        assert records.index(called) < records.index(boundaries(records)[0])
        started = compaction_records(records)
        assert [r.phase for r in started] == [CompactionPhase.STARTED]
        assert started[0].trigger is CompactTrigger.MANUAL
        assert started[0].command_uuid == command_id
        assert started[0].compaction_id == summaries(records)[0].uuid

    async def test_command_compacts_with_the_instructions(
        self, server: FakeServer, agents: StandAgents, tmp_path: Path
    ) -> None:
        sink = Collected()
        profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
        served = Served(agents.agent(profile, sink, tools=StandTools()))
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(call(large(2_000))))
        await served.agent.settled()
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        before = len(await journal(tmp_path / "history", SESSION))
        command = served.agent.queue.enqueue(
            QueueEntry(kind=QueueKind.COMPACT, text=self.NOTE)
        )
        await served.agent.settled()
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.COMPLETED] * 4
        records = await journal(tmp_path / "history", SESSION)
        self._check_command_records(records, before, command.id)
        found = boundaries(records)
        assert len(found) == 1
        assert found[0].compact_metadata.trigger is CompactTrigger.MANUAL
        attempts = summary_requests(server)
        assert len(attempts) == 1
        sent = attempts[0]["messages"]
        assert isinstance(sent, list)
        instruction = json.dumps(sent[-1], ensure_ascii=False)
        assert f"Additional Instructions:\\n{self.NOTE}\\n\\nREMINDER:" in instruction
        after = messages(server, len(server.requests()) - 1)
        first_user = WireText().of(after[1]["content"])
        assert first_user is not None
        assert CONTINUED in first_user
        sent_after = json.dumps(after, ensure_ascii=False)
        assert json.dumps(self.CALL, ensure_ascii=False)[1:-1] in sent_after
        stdout = "<local-command-stdout>Compacted</local-command-stdout>"
        assert stdout in sent_after
        assert sent_after.index(stdout) > sent_after.index(CONTINUED)
        conversation = session.conversation()
        assert isinstance(conversation[0], CompactBoundaryRecord)
        assert conversation[0].uuid == found[0].uuid

    async def test_reply_without_the_summary_block_is_the_summary(
        self, agents: StandAgents
    ) -> None:
        """Ответ без блока <summary> — пересказ целиком, без разбора
        <analysis> (локальные модели блок не пишут)."""
        profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
        served = Served(agents.agent(profile, Collected(), tools=StandTools()))
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(call(large(2_000))))
        await served.agent.settled()
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        served.agent.queue.enqueue(
            QueueEntry(kind=QueueKind.COMPACT, text=SummaryName.NO_BLOCK.value)
        )
        await served.agent.settled()
        await served.close()

        assert served.agent.outcomes[-1] is TurnOutcome.COMPLETED
        summaries = [
            r
            for r in session.conversation()
            if isinstance(r, UserRecord) and r.is_compact_summary
        ]
        assert len(summaries) == 1
        content = summaries[0].message.content
        assert isinstance(content, str)
        assert "and no summary block" in content
        assert "only analysis" not in content

    async def test_command_without_anything_to_summarize_fails(
        self, agents: StandAgents
    ) -> None:
        """В новой сессии пересказывать нечего: вызов команды записан, исход
        — отказ с причиной."""
        profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
        served = Served(agents.agent(profile, Collected(), tools=StandTools()))
        session = await served.open(SESSION, resume=False)

        command = served.agent.queue.enqueue(
            QueueEntry(kind=QueueKind.COMPACT, text="")
        )
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.API_ERROR]
        records = session.records()
        assert not boundaries(records)
        users = [r.uuid for r in records if isinstance(r, UserRecord)]
        assert users[-1] == command.id
        assert synthetic_texts(records) == [
            "Compaction failed: there is nothing to summarize yet: the conversation "
            "holds fewer than two rounds with a model reply"
        ]


class TestCancellation:
    async def test_interrupt_during_compaction_leaves_no_records(
        self, hanging_agents: StandAgents
    ) -> None:
        sink = Collected()
        served = Served(
            hanging_agents.agent(
                hanging_agents.profile(Protocol.OPENAI, chars_per_token=4),
                sink,
                tools=StandTools(),
            )
        )
        session = await served.open(SESSION, resume=False)

        first = f"{SummaryName.HANG.value} {call(large())}"
        served.agent.queue.enqueue(prompt(first))
        await served.agent.settled()
        before = len(session.records())
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await wait_state(session, SessionState.COMPACTING)
        assert served.agent.interrupt()
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [
            TurnOutcome.COMPLETED,
            TurnOutcome.ABORTED_STREAMING,
        ]
        records = list(session.records())
        steps = [phase for _, phase in phases(records)]
        assert steps == [CompactionPhase.STARTED, CompactionPhase.CANCELLED]
        assert kinds(records[before:]) == [
            "queue-operation",
            "queue-operation",
            "user",
            "system:compaction",
            "system:compaction",
        ]
        assert not boundaries(records)
        assert session.state is SessionState.STOPPING

    async def test_now_message_during_compaction_starts_its_own_turn(
        self, hanging_agents: StandAgents, tmp_path: Path
    ) -> None:
        sink = Collected()
        served = Served(
            hanging_agents.agent(
                hanging_agents.profile(Protocol.OPENAI, chars_per_token=4),
                sink,
                tools=StandTools(),
            )
        )
        session = await served.open(SESSION, resume=False)

        first = f"{SummaryName.HANG.value} {call(large())}"
        served.agent.queue.enqueue(prompt(first))
        await served.agent.settled()
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await wait_state(session, SessionState.COMPACTING)
        urgent = f"{SummaryName.OK.value} {ScenarioName.THINKING.value}"
        served.agent.queue.enqueue(prompt(urgent, QueuePriority.NOW))
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [
            TurnOutcome.COMPLETED,
            TurnOutcome.ABORTED_STREAMING,
            TurnOutcome.COMPLETED,
        ]
        records = await journal(tmp_path / "history", SESSION)
        assert len(boundaries(records)) == 1
        prompts = [
            r
            for r in records
            if isinstance(r, UserRecord)
            and isinstance(r.message.content, str)
            and not r.is_compact_summary
        ]
        assert [p.message.content for p in prompts] == [
            first,
            ScenarioName.ANSWER.value,
            urgent,
        ]
        kept = boundaries(records)[0].compact_metadata.preserved_messages.uuids
        assert prompts[1].uuid in kept
        assert prompts[2].uuid in kept


class TestResume:
    async def test_cut_after_the_boundary_resumes_without_it_and_compacts_again(
        self, server: FakeServer, agents: StandAgents, tmp_path: Path
    ) -> None:
        profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
        served = Served(agents.agent(profile, Collected(), tools=StandTools()))
        await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(call(large())))
        await served.agent.settled()
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        source = tmp_path / "history" / f"{SESSION.value}.jsonl"
        lines = source.read_text(encoding="utf-8").splitlines()
        cut = 0
        for index, line in enumerate(lines):
            document = json.loads(line)
            if document.get("subtype") == "compact_boundary":
                cut = index + 1

        assert cut
        source.write_text("\n".join(lines[:cut]) + "\n", encoding="utf-8")

        resumed = Served(agents.agent(profile, Collected(), tools=StandTools()))
        session = await resumed.open(SESSION, resume=True)
        assert len(boundaries(session.records())) == 1
        assert not summaries(session.records())
        assert not isinstance(session.conversation()[0], CompactBoundaryRecord)

        resumed.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await resumed.agent.settled()
        await resumed.close()

        assert list(resumed.agent.outcomes) == [TurnOutcome.COMPLETED]
        records = session.records()
        assert len(boundaries(records)) == 2
        assert len(summaries(records)) == 1
        conversation = session.conversation()
        assert conversation[0] is boundaries(records)[1]
        again = await reloaded(tmp_path / "history", SESSION)
        assert list(again.conversation()) == list(conversation)

    async def test_rule_and_mode_survive_two_compactions_and_resume(
        self, server: FakeServer, agents: StandAgents, tmp_path: Path
    ) -> None:
        tools = StandTools()
        sink = Collected()
        profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
        served = Served(agents.agent(profile, sink, tools=tools))
        await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(call({"name": StandToolName.DROP.full})))
        question = await sink.next_question(0)
        rule = PermissionRule(
            tool="mcp__stand__drop", behavior=PermissionBehavior.ALLOW
        )
        assert served.agent.answer(
            question.request_id, AllowAnswer(updated_permissions=[rule])
        )
        await served.agent.settled()
        served.agent.set_permission_mode(PermissionMode.ACCEPT_EDITS)
        served.agent.queue.enqueue(prompt(call(large())))
        await served.agent.settled()
        served.agent.queue.enqueue(prompt(call(large())))
        await served.agent.settled()
        both = call(
            {"name": StandToolName.DROP.full}, {"name": StandToolName.NOTE.full}
        )
        served.agent.queue.enqueue(prompt(both))
        await served.agent.settled()
        await served.close()

        assert len(boundaries(await journal(tmp_path / "history", SESSION))) >= 2
        assert len(sink.questions()) == 1
        assert [c.name for c in tools.calls] == [
            "mcp__stand__drop",
            "mcp__stand__large",
            "mcp__stand__large",
            "mcp__stand__drop",
            "mcp__stand__note",
        ]

        again = Collected()
        resumed = Served(agents.agent(profile, again, tools=tools))
        await resumed.open(SESSION, resume=True)
        assert resumed.agent.permission_mode is PermissionMode.ACCEPT_EDITS
        resumed.agent.queue.enqueue(prompt(both))
        await resumed.agent.settled()
        await resumed.close()

        assert not again.questions()
        assert [c.name for c in tools.calls][-2:] == [
            "mcp__stand__drop",
            "mcp__stand__note",
        ]
        assert list(resumed.agent.outcomes) == [TurnOutcome.COMPLETED]


class TestStart:
    async def test_prefix_over_half_the_threshold_fails_at_start(
        self, agents: StandAgents
    ) -> None:
        agent = agents.agent(
            agents.profile(Protocol.OPENAI),
            Collected(),
            system_prompt=("x" * 12_000,),
        )

        with pytest.raises(ProfileError, match="half"):
            await agent.open(SESSION, resume=False)
