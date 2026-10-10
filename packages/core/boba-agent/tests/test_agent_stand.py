"""Агент на стенде модели: ходы без инструментов по обоим протоколам,
возобновление, предел длины ответа, ошибки модели, отмена, обрывы журнала."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from itertools import pairwise
from pathlib import Path

import pytest
from agent_stand import (
    Collected,
    FakeServer,
    Protocol,
    Served,
    StandAgents,
)

from boba.agent.events import (
    RecordEvent,
    ResultEvent,
    StreamEvent,
    TurnOutcome,
)
from boba.agent.history import SessionId
from boba.agent.ids import IdMint, SystemClock
from boba.agent.loader import HistoryLoader
from boba.agent.queue import QueueEntry, QueueKind
from boba.agent.records import (
    ApiErrorRecord,
    AssistantRecord,
    AttachmentRecord,
    ChainRecord,
    Record,
    ReplyStopReason,
    Synthetic,
    UserRecord,
)
from boba.agent.session import Session
from boba.agent.template import AgentTemplate
from boba.cancellation import StopReason
from boba.history.jsonl import JsonlHistoryStore
from boba.stand.ui.fake_llm import FailureName, ScenarioName

pytestmark = pytest.mark.anyio

SESSION = SessionId(value="agent-stand-1")
VERSION = "0.0.26.dev4"


def kinds(records: Sequence[Record] | Sequence[ChainRecord]) -> list[str]:
    names: list[str] = []
    for record in records:
        name = record.type.value
        if isinstance(record, AttachmentRecord):
            name = f"attachment:{record.attachment.type}"

        if isinstance(record, AssistantRecord):
            name = f"assistant:{record.message.content[0].type}"

        names.append(name)

    return names


def assert_linked(chain: Sequence[ChainRecord]) -> None:
    previous: str | None = None
    for record in chain:
        assert record.parent_uuid == previous, (
            record.uuid,
            record.parent_uuid,
            previous,
        )
        previous = record.uuid


async def reloaded(root: Path, session_id: SessionId) -> Session:
    store = JsonlHistoryStore(root)
    session = Session(session_id)
    await HistoryLoader(store, AgentTemplate(), IdMint(), SystemClock(), VERSION).load(
        session
    )

    return session


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


def prompt(text: str) -> QueueEntry:
    return QueueEntry(kind=QueueKind.PROMPT, text=text)


class TestTwoTurns:
    @pytest.mark.parametrize("protocol", list(Protocol))
    async def test_journal_and_request_prefix(
        self,
        server: FakeServer,
        agents: StandAgents,
        tmp_path: Path,
        protocol: Protocol,
    ) -> None:
        sink = Collected()
        served = Served(agents.agent(agents.profile(protocol), sink))
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        served.agent.queue.enqueue(prompt(ScenarioName.THINKING_ANSWER.value))
        await served.agent.settled()
        await served.close()

        assert kinds(session.records()) == [
            "permission-mode",
            "queue-operation",
            "queue-operation",
            "user",
            "attachment:model",
            "attachment:date",
            "attachment:prompt_snapshot",
            "assistant:text",
            "queue-operation",
            "queue-operation",
            "user",
            "assistant:thinking",
            "assistant:text",
        ]
        assert_linked(session.chain())
        assert list(served.agent.outcomes) == [TurnOutcome.COMPLETED] * 2
        results = sink.of(ResultEvent)
        assert len(results) == 2
        assert all(isinstance(e, ResultEvent) and e.num_turns == 1 for e in results)
        assert len(sink.of(StreamEvent)) > 0
        assert len(sink.of(RecordEvent)) == 5

        requests = server.requests()
        assert len(requests) == 2
        first = requests[0]["messages"]
        second = requests[1]["messages"]
        assert isinstance(first, list)
        assert isinstance(second, list)
        assert second[: len(first)] == first
        assert first[0]["role"] == "system"
        assert "scenario:answer" in first[1]["content"]
        assert second[2]["role"] == "assistant"

        again = await reloaded(tmp_path / "history", SESSION)
        assert list(again.records()) == list(session.records())

    async def test_system_turns_mode_keeps_the_user_text_clean(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        sink = Collected()
        served = Served(
            agents.agent(agents.profile(Protocol.OPENAI, system_turns=True), sink)
        )
        await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        messages = server.requests()[0]["messages"]
        assert isinstance(messages, list)
        assert [m["role"] for m in messages] == ["system", "user", "system"]
        assert messages[1]["content"] == ScenarioName.ANSWER.value
        assert "Today's date is" in messages[2]["content"]


class TestResume:
    async def test_third_turn_continues_the_chain(
        self, server: FakeServer, agents: StandAgents, tmp_path: Path
    ) -> None:
        sink = Collected()
        served = Served(agents.agent(agents.profile(Protocol.OPENAI), sink))
        session = await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()
        before = list(session.records())
        last_request = server.requests()[-1]["messages"]

        resumed = Served(agents.agent(agents.profile(Protocol.OPENAI), Collected()))
        session_again = await resumed.open(SESSION, resume=True)
        resumed.agent.queue.enqueue(prompt(ScenarioName.THINKING.value))
        await resumed.agent.settled()
        await resumed.close()

        records = list(session_again.records())
        assert records[: len(before)] == before
        assert kinds(records[len(before) :]) == [
            "queue-operation",
            "queue-operation",
            "user",
            "assistant:thinking",
            "assistant:text",
        ]
        assert_linked(session_again.chain())
        after_request = server.requests()[-1]["messages"]
        assert isinstance(last_request, list)
        assert isinstance(after_request, list)
        assert after_request[: len(last_request)] == last_request
        assert server.requests()[-1].get("tools") == server.requests()[0].get("tools")
        again = await reloaded(tmp_path / "history", SESSION)
        assert list(again.records()) == records

    async def test_resume_of_an_unknown_session_fails(
        self, agents: StandAgents
    ) -> None:
        agent = agents.agent(agents.profile(Protocol.OPENAI), Collected())

        with pytest.raises(Exception, match="no journal to resume"):
            await agent.open(SessionId(value="nobody"), resume=True)


class TestOutputLimit:
    @pytest.mark.parametrize("protocol", list(Protocol))
    async def test_partial_records_meta_message_and_retries(
        self, server: FakeServer, agents: StandAgents, protocol: Protocol
    ) -> None:
        sink = Collected()
        served = Served(agents.agent(agents.profile(protocol), sink))
        session = await served.open(SESSION, resume=False)

        marker = f"{ScenarioName.ANSWER.value} {FailureName.LENGTH.value}"
        served.agent.queue.enqueue(prompt(marker))
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.API_ERROR]
        chain = session.chain()
        partials = [
            r
            for r in chain
            if isinstance(r, AssistantRecord)
            and r.message.stop_reason is ReplyStopReason.MAX_TOKENS
        ]
        metas = [r for r in chain if isinstance(r, UserRecord) and r.is_meta]
        assert len(partials) == 4
        assert len(metas) == 3
        assert metas[0].message.content.startswith("Output token limit hit.")  # type: ignore[union-attr]
        assert partials[0].message.content[0].type == "text"
        final = chain[-1]
        assert isinstance(final, AssistantRecord)
        assert final.is_api_error_message
        assert final.message.model == Synthetic.MODEL
        assert len(server.requests()) == 4
        last_messages = server.requests()[-1]["messages"]
        assert isinstance(last_messages, list)
        assert last_messages[-1]["role"] == "user"
        assert "Output token limit hit" in last_messages[-1]["content"]
        assert_linked(chain)


class TestApiErrors:
    @pytest.mark.parametrize(
        "failure", [FailureName.CUT, FailureName.STATUS_500, FailureName.HANG]
    )
    async def test_errors_are_recorded_and_the_next_turn_merges_users(
        self, server: FakeServer, agents: StandAgents, failure: FailureName
    ) -> None:
        sink = Collected()
        served = Served(agents.agent(agents.profile(Protocol.OPENAI), sink))
        session = await served.open(SESSION, resume=False)

        marker = f"{ScenarioName.ANSWER.value} {failure.value}"
        served.agent.queue.enqueue(prompt(marker))
        await served.agent.settled()
        failed_at = len(session.records())
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [
            TurnOutcome.API_ERROR,
            TurnOutcome.COMPLETED,
        ]
        first_turn = session.records()[:failed_at]
        errors = [r for r in first_turn if isinstance(r, ApiErrorRecord)]
        assert [e.retry_attempt for e in errors] == [1, 2, 3, 4]
        assert all(e.max_retries == 3 for e in errors)
        synthetic = first_turn[-1]
        assert isinstance(synthetic, AssistantRecord)
        assert synthetic.is_api_error_message

        roles = [m["role"] for m in server.requests()[-1]["messages"]]  # type: ignore[index]
        assert roles in (["system", "user", "assistant"], ["system", "user"])
        for left, right in pairwise(roles):
            assert not (left == "user" and right == "user")

        merged = server.requests()[-1]["messages"][1]["content"]  # type: ignore[index]
        assert marker in merged
        assert merged.endswith(ScenarioName.ANSWER.value)


class TestCancellation:
    async def test_interrupt_mid_stream_leaves_memory_and_journal_consistent(
        self, tmp_path: Path
    ) -> None:
        server = FakeServer(token_delay_sec=0.05)
        await server.start()
        agents = StandAgents(server, tmp_path / "history")
        sink = Collected()
        served = Served(agents.agent(agents.profile(Protocol.OPENAI), sink))
        try:
            session = await served.open(SESSION, resume=False)
            served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
            while not sink.of(StreamEvent):
                await asyncio.sleep(0.01)

            assert served.agent.interrupt(StopReason.USER_STOP)
            await served.agent.settled()
            await served.close()
        finally:
            await agents.aclose()
            await server.stop()

        assert list(served.agent.outcomes) == [TurnOutcome.ABORTED_STREAMING]
        chain = session.chain()
        partial = chain[-2]
        marker = chain[-1]
        assert isinstance(partial, AssistantRecord)
        assert partial.is_aborted_mid_stream
        assert isinstance(marker, UserRecord)
        assert marker.message.content == "[Request interrupted by user]"
        assert marker.interrupted_by_shutdown is None
        again = await reloaded(tmp_path / "history", SESSION)
        stored = len(session.records())
        assert list(again.records())[:stored] == list(session.records())
        repaired = again.records()[stored:]
        assert kinds(repaired) == ["assistant:text"]

    async def test_close_mid_turn_flushes_the_journal(self, tmp_path: Path) -> None:
        server = FakeServer(token_delay_sec=0.05)
        await server.start()
        agents = StandAgents(server, tmp_path / "history")
        sink = Collected()
        served = Served(agents.agent(agents.profile(Protocol.OPENAI), sink))
        try:
            session = await served.open(SESSION, resume=False)
            served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
            while not sink.of(StreamEvent):
                await asyncio.sleep(0.01)

            await served.close()
        finally:
            await agents.aclose()
            await server.stop()

        assert list(served.agent.outcomes) == [TurnOutcome.SHUTDOWN]
        marker = session.chain()[-1]
        assert isinstance(marker, UserRecord)
        assert marker.interrupted_by_shutdown is True
        again = await reloaded(tmp_path / "history", SESSION)
        stored = len(session.records())
        assert list(again.records())[:stored] == list(session.records())
        assert kinds(again.records()[stored:]) == ["assistant:text"]
        assert served.agent.writer.unflushed == 0


class TestTruncation:
    async def test_every_cut_point_restores_a_sound_session(
        self, server: FakeServer, agents: StandAgents, tmp_path: Path
    ) -> None:
        """Обрыв процесса на границе любой записи: возобновление держит
        инварианты раздела 5.19 и повторное возобновление ничего не меняет."""
        served = Served(agents.agent(agents.profile(Protocol.OPENAI), Collected()))
        await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(ScenarioName.THINKING_ANSWER.value))
        await served.agent.settled()
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        source = tmp_path / "history" / f"{SESSION.value}.jsonl"
        lines = source.read_text(encoding="utf-8").splitlines()
        for kept in range(1, len(lines) + 1):
            root = tmp_path / f"cut-{kept}"
            root.mkdir()
            (root / source.name).write_text(
                "\n".join(lines[:kept]) + "\n", encoding="utf-8"
            )

            session = await reloaded(root, SESSION)
            chain = session.chain()
            assert_linked(chain)
            if not chain:
                continue

            by_message: dict[str, set[str]] = {}
            for record in chain:
                if isinstance(record, AssistantRecord):
                    by_message.setdefault(record.message.id, set()).add(
                        record.request_id
                    )

            assert all(len(ids) == 1 for ids in by_message.values()), kept
            last = chain[-1]
            assert isinstance(last, AssistantRecord), (kept, kinds(chain))

            store = JsonlHistoryStore(root)
            lease = await store.acquire(SESSION)
            try:
                await store.append(lease, session.since(kept))
            finally:
                await store.release(lease)

            second = await reloaded(root, SESSION)
            assert list(second.records()) == list(session.records()), kept

        document = json.loads(lines[3])
        assert document["type"] == "user"
