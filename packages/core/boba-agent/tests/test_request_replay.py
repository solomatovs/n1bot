"""Запрос к модели собирается из истории заново байт в байт: агент пишет
каждый запрос записью api-request с digest, тест восстанавливает сессию из
записей журнала, стоящих до неё, собирает основной запрос сборщиком и
сравнивает digest. Запросы пересказа собираются с инструкцией, которой в
истории нет, — их digest не проверяется, но они считаются."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path

import pytest

from boba.agent.attachments import Announced
from boba.agent.builder import RequestBuilder
from boba.agent.history import SessionId, StoredLine
from boba.agent.profile import ModelProfile, ReasoningReturn
from boba.agent.queue import QueueEntry, QueueKind
from boba.agent.records import ApiRequestRecord, Record, RecordCodec
from boba.agent.session import Session
from boba.agent.template import AgentTemplate, AgentTemplateFile
from boba.history.jsonl import JsonlHistoryStore
from boba.llm.chat import ChatRequest
from boba.stand.agent import (
    Collected,
    FakeServer,
    Protocol,
    Served,
    StandAgents,
    StandToolName,
    StandTools,
)
from boba.stand.ui.fake_llm import FailureName, ScenarioName

pytestmark = pytest.mark.anyio

SESSION = SessionId(value="request-replay-1")


@dataclass(frozen=True)
class Scenario:
    """Ход разговора сценария: что ставится в очередь по порядку, профиль
    модели и сколько запросов должно получиться."""

    entries: Sequence[QueueEntry]
    profile: Callable[[StandAgents], ModelProfile]
    main_requests: int
    summary_requests: int


def prompt(text: str) -> QueueEntry:
    return QueueEntry(kind=QueueKind.PROMPT, text=text)


def call(name: StandToolName, arguments: dict[str, object]) -> str:
    spec = {"name": name.full, "arguments": arguments}

    return f"{ScenarioName.CALL.value} {json.dumps(spec)}"


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
            prompt(call(StandToolName.LOOKUP, {"key": "a"})),
            prompt(call(StandToolName.BROKEN, {})),
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
            prompt(call(StandToolName.LARGE, {"size": 24_000, "fill": "x"})),
            prompt(ScenarioName.ANSWER.value),
            prompt(ScenarioName.ANSWER.value),
        ),
        profile=small_window,
        main_requests=4,
        summary_requests=1,
    ),
    "manual_compaction": Scenario(
        entries=(
            prompt(call(StandToolName.LARGE, {"size": 2_000, "fill": "x"})),
            prompt(ScenarioName.ANSWER.value),
            QueueEntry(kind=QueueKind.COMPACT, text="Keep the rows."),
            prompt(ScenarioName.ANSWER.value),
        ),
        profile=small_window,
        main_requests=4,
        summary_requests=1,
    ),
}


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
        self._builder = RequestBuilder(profile, self._templates)
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


class TestRequestsFromHistory:
    @pytest.mark.parametrize("name", list(SCENARIOS))
    async def test_every_request_is_rebuilt_from_the_records_before_it(
        self, agents: StandAgents, tmp_path: Path, name: str
    ) -> None:
        scenario = SCENARIOS[name]
        profile = scenario.profile(agents)
        served = Served(
            agents.agent(profile, Collected(), tools=StandTools(), record_requests=True)
        )
        await served.open(SESSION, resume=False)
        for entry in scenario.entries:
            served.agent.queue.enqueue(entry)
            await served.agent.settled()

        await served.close()

        records = await journal(tmp_path / "history")
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
            assert replay.digest(records[:index]) == record.digest, (name, index)

        assert (main, summaries) == (
            scenario.main_requests,
            scenario.summary_requests,
        )
