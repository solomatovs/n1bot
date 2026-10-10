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

import pytest
from agent_stand import (
    Collected,
    FakeServer,
    Protocol,
    Served,
    StandAgents,
    StandToolName,
    StandTools,
)

from boba.agent.control import AllowAnswer
from boba.agent.events import RecordEvent, TurnOutcome
from boba.agent.history import SessionId
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
    PermissionBehavior,
    PermissionMode,
    PermissionRule,
    Record,
    UserRecord,
)
from boba.agent.session import Session, SessionState
from boba.agent.template import AgentTemplate
from boba.history.jsonl import JsonlHistoryStore
from boba.stand.ui.fake_llm import FailureName, ScenarioName, SummaryName

pytestmark = pytest.mark.anyio

SESSION = SessionId(value="agent-compaction-1")
VERSION = "0.0.26.dev4"
CONTINUED = "This session is being continued from a previous conversation"
INSTRUCTION_MARK = "an <analysis> block followed by a <summary> block"
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
        "name": StandToolName.LARGE.value,
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

    content = last["content"]

    return isinstance(content, str) and INSTRUCTION_MARK in content


def summary_requests(server: FakeServer) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for request in server.requests():
        if is_summary_request(request):
            found.append(request)

    return found


def synthetic_texts(records: Sequence[Record]) -> list[str]:
    texts: list[str] = []
    for record in records:
        if isinstance(record, AssistantRecord) and record.is_synthetic():
            texts.append(record.error or "")

    return texts


async def reloaded(root: Path, session_id: SessionId) -> Session:
    store = JsonlHistoryStore(root)
    session = Session(session_id)
    await HistoryLoader(store, AgentTemplate(), IdMint(), SystemClock(), VERSION).load(
        session
    )

    return session


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

        served.agent.queue.enqueue(prompt(call(large())))
        await served.agent.settled()
        before = len(session.records())
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.COMPLETED] * 2
        self.check_records(session, before)
        events = [e for e in sink.of(RecordEvent) if isinstance(e, RecordEvent)]
        assert any(isinstance(e.record, CompactBoundaryRecord) for e in events)
        after = self.check_requests(server)

        again = await reloaded(tmp_path / "history", SESSION)
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

    @staticmethod
    def check_records(session: Session, before: int) -> None:
        """Пачка после второго промпта, границы и проекция разговора."""
        records = list(session.records())
        assert kinds(records[before:]) == [
            "queue-operation",
            "queue-operation",
            "user",
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
        date = records[before + 4]
        assert isinstance(prompt_two, UserRecord)
        assert isinstance(date, AttachmentRecord)
        assert boundary.parent_uuid is None
        assert boundary.logical_parent_uuid == prompt_two.uuid
        assert summary.parent_uuid == date.uuid
        assert summary.is_visible_in_transcript_only is True
        assert isinstance(summary.message.content, str)
        assert summary.message.content.startswith(CONTINUED)
        metadata = boundary.compact_metadata
        assert metadata.preserved_messages.anchor_uuid == summary.uuid
        chain = session.chain()
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
        first_user = after[1]["content"]
        assert isinstance(first_user, str)
        assert CONTINUED in first_user
        assert "Today's date is" in first_user
        assert ScenarioName.CALL.value in first_user
        assert after[2]["content"] == "the tool has answered"
        last_user = after[3]["content"]
        assert isinstance(last_user, str)
        assert last_user.endswith(ScenarioName.ANSWER.value)

        return after

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

        first = f"{FailureName.NO_USAGE.value} {call(large(17_500, 'ы'))}"
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
            SummaryName.NO_BLOCK,
            SummaryName.EMPTY,
            SummaryName.LONG,
            SummaryName.LENGTH,
        ],
    )
    async def test_three_failed_summaries_open_the_breaker(
        self, plain_server: FakeServer, plain_agents: StandAgents, failure: SummaryName
    ) -> None:
        """Каждая неудача пересказа считается; после трёх подряд попытки
        прекращаются, и ход, дошедший до предела, кончается названным исходом."""
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

        text = f"{failure.value} {FailureName.NO_USAGE.value} {loop(3_000)}"
        served.agent.queue.enqueue(prompt(text))
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.COMPACT_FAILURE_BREAKER]
        records = session.records()
        assert not boundaries(records)
        assert len(summary_requests(plain_server)) == 3
        texts = synthetic_texts(records)
        assert len(texts) == 1
        assert texts[0].startswith("Prompt is too long. Automatic compaction failed:")
        assert kinds(records[-3:]) == [
            "user:tool_result",
            "system:api_error",
            "assistant:synthetic",
        ]

    async def test_rapid_refill_trips_the_breaker(
        self, plain_server: FakeServer, plain_agents: StandAgents
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
        records = session.records()
        assert len(boundaries(records)) == 3
        assert len(summaries(records)) == 3
        assert len(summary_requests(plain_server)) == 3
        texts = synthetic_texts(records)
        assert len(texts) == 1
        assert texts[0].startswith("Autocompact is thrashing")
        conversation = session.conversation()
        assert isinstance(conversation[0], CompactBoundaryRecord)
        assert conversation[0] is boundaries(records)[-1]


class TestLadder:
    async def test_overflowing_summary_request_drops_the_oldest_round(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        sink = Collected()
        served = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI, chars_per_token=4),
                sink,
                tools=StandTools(),
            )
        )
        session = await served.open(SESSION, resume=False)

        first = f"{SummaryName.OVERFLOW.value} {call(large())}"
        served.agent.queue.enqueue(prompt(first))
        await served.agent.settled()
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.COMPLETED] * 2
        attempts = summary_requests(server)
        assert len(attempts) == 2
        first_texts = json.dumps(attempts[0]["messages"])
        second_texts = json.dumps(attempts[1]["messages"])
        assert SummaryName.OVERFLOW.value in first_texts
        assert SummaryName.OVERFLOW.value not in second_texts
        second_messages = attempts[1]["messages"]
        assert isinstance(second_messages, list)
        assert [m["role"] for m in second_messages[:2]] == ["system", "assistant"]
        records = session.records()
        assert len(boundaries(records)) == 1
        assert not synthetic_texts(records)


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
        assert kinds(records[before:]) == ["queue-operation", "queue-operation", "user"]
        assert not boundaries(records)
        assert session.state is SessionState.STOPPING

    async def test_now_message_during_compaction_starts_its_own_turn(
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
        records = session.records()
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
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        tools = StandTools()
        sink = Collected()
        profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
        served = Served(agents.agent(profile, sink, tools=tools))
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(call({"name": StandToolName.DROP.value})))
        question = await sink.next_question(0)
        rule = PermissionRule(tool="drop", behavior=PermissionBehavior.ALLOW)
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
            {"name": StandToolName.DROP.value}, {"name": StandToolName.NOTE.value}
        )
        served.agent.queue.enqueue(prompt(both))
        await served.agent.settled()
        await served.close()

        assert len(boundaries(session.records())) >= 2
        assert len(sink.questions()) == 1
        assert [c.tool for c in tools.calls] == [
            "drop",
            "large",
            "large",
            "drop",
            "note",
        ]

        again = Collected()
        resumed = Served(agents.agent(profile, again, tools=tools))
        await resumed.open(SESSION, resume=True)
        assert resumed.agent.permission_mode is PermissionMode.ACCEPT_EDITS
        resumed.agent.queue.enqueue(prompt(both))
        await resumed.agent.settled()
        await resumed.close()

        assert not again.questions()
        assert [c.tool for c in tools.calls][-2:] == ["drop", "note"]
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
