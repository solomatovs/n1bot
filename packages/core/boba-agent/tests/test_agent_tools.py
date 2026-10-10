"""Агент на стенде с инструментами: вызовы через порт сервера, разрешения
по режимам, вопросы наружу, подмешивание очереди, отмены, возобновление,
обрыв MCP, обрезка результата, предел шагов."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import ClassVar

import pytest
from agent_stand import (
    Collected,
    FakeServer,
    Protocol,
    RoutedServers,
    Served,
    StandAgents,
    StandToolName,
    StandTools,
)

from boba.agent.control import AllowAnswer, DenyAnswer
from boba.agent.events import ControlCancelEvent, ResultEvent, TurnOutcome
from boba.agent.history import SessionId
from boba.agent.ids import IdMint, SystemClock
from boba.agent.loader import HistoryLoader
from boba.agent.profile import TurnLimits
from boba.agent.queue import QueueEntry, QueueKind, QueuePriority
from boba.agent.records import (
    AssistantRecord,
    AttachmentRecord,
    ChainRecord,
    PermissionBehavior,
    PermissionMode,
    PermissionModeRecord,
    PermissionRule,
    PermissionRuleRecord,
    QueueOperationRecord,
    QueueRemoveReason,
    Record,
    ToolDenialKind,
    ToolResultBlock,
    ToolUseBlock,
    UserRecord,
)
from boba.agent.session import Session
from boba.agent.template import AgentTemplate
from boba.agent.tools import AskUserQuestionTool
from boba.history.jsonl import JsonlHistoryStore
from boba.identity.context import CallContexts
from boba.llm.chat import (
    ChatEvent,
    ChatModel,
    ChatReply,
    ChatRequest,
    LlmError,
    ToolCall,
)
from boba.mcp_client.client import (
    DroppedSignals,
    McpServerConfig,
    McpToolServer,
    NamedBlocks,
    StdioCommand,
)
from boba.stand.ui.fake_llm import FailureName, ScenarioName
from boba.toolrun.hosted import DirectCalls

pytestmark = pytest.mark.anyio

SESSION = SessionId(value="agent-tools-1")
VERSION = "0.0.26.dev4"
MCP_SERVER = (
    Path(__file__).resolve().parents[3]
    / "services"
    / "boba-mcp-client"
    / "tests"
    / "standard_server.py"
)


def prompt(text: str, priority: QueuePriority | None = None) -> QueueEntry:
    return QueueEntry(kind=QueueKind.PROMPT, text=text, priority=priority)


def call(*specs: dict[str, object]) -> str:
    """Текст сообщения, по которому стенд вызовет перечисленные инструменты."""
    if len(specs) == 1:
        return f"{ScenarioName.CALL.value} {json.dumps(specs[0])}"

    return f"{ScenarioName.CALL.value} {json.dumps(list(specs))}"


def kinds(records: Sequence[Record] | Sequence[ChainRecord]) -> list[str]:
    names: list[str] = []
    for record in records:
        name = record.type.value
        if isinstance(record, AttachmentRecord):
            name = f"attachment:{record.attachment.type}"

        if isinstance(record, AssistantRecord):
            name = f"assistant:{record.message.content[0].type}"

        if isinstance(record, UserRecord) and record.message.tool_results():
            name = "user:tool_result"

        names.append(name)

    return names


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


def assert_linked(chain: Sequence[ChainRecord]) -> None:
    previous: str | None = None
    for record in chain:
        assert record.parent_uuid == previous, (record.uuid, previous)
        previous = record.uuid


async def reloaded(root: Path, session_id: SessionId) -> Session:
    store = JsonlHistoryStore(root)
    session = Session(session_id)
    await HistoryLoader(store, AgentTemplate(), IdMint(), SystemClock(), VERSION).load(
        session
    )

    return session


class StandClassifier(ChatModel):
    """Классификатор режима `auto` со сценарием: вердикт по имени инструмента
    в вопросе; без вердикта — сбой модели."""

    def __init__(self, verdicts: dict[str, str]) -> None:
        self._verdicts = verdicts
        self.questions: list[str] = []

    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatEvent]:
        question = request.messages[-1].content
        self.questions.append(question)
        for tool, verdict in self._verdicts.items():
            if f"Tool: {tool}\n" in question:
                arguments = {"decision": verdict, "reason": f"scripted for {tool}"}
                yield ChatReply(
                    tool_calls=[
                        ToolCall(
                            id="c1", name="permission_decision", arguments=arguments
                        )
                    ]
                )
                return

        msg = "the stand classifier has no verdict for this question"
        raise LlmError(msg)


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


class TestCalls:
    @pytest.mark.parametrize("protocol", list(Protocol))
    async def test_two_tools_in_one_step_are_recorded_in_call_order(
        self,
        server: FakeServer,
        agents: StandAgents,
        tmp_path: Path,
        protocol: Protocol,
    ) -> None:
        tools = StandTools()
        sink = Collected()
        served = Served(agents.agent(agents.profile(protocol), sink, tools=tools))
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(
            prompt(
                call(
                    {"name": StandToolName.SLEEP.value, "arguments": {"seconds": 0.05}},
                    {"name": StandToolName.LOOKUP.value},
                    {"name": StandToolName.EMPTY.value},
                    {"name": StandToolName.BROKEN.value},
                )
            )
        )
        await served.agent.settled()
        await served.close()

        chain = session.chain()
        assert_linked(chain)
        assert kinds(chain) == [
            "user",
            "attachment:model",
            "attachment:date",
            "attachment:prompt_snapshot",
            "assistant:thinking",
            "assistant:tool_use",
            "assistant:tool_use",
            "assistant:tool_use",
            "assistant:tool_use",
            "user:tool_result",
            "user:tool_result",
            "user:tool_result",
            "user:tool_result",
            "assistant:text",
        ]
        uses = tool_uses(chain)
        found = results(chain)
        assert [r.tool_use_id for r in found] == [u.id for u in uses]
        assert [r.text() for r in found] == [
            "slept",
            "lookup done",
            "(empty completed with no output)",
            found[3].text(),
        ]
        assert "stand tool failed" in found[3].text()
        assert found[3].is_error is True
        assert found[2].is_error is None
        assert [len(u.id) for u in uses] == [9, 9, 9, 9]
        assert len({u.id for u in uses}) == 4
        sources = [r.source_tool_assistant_uuid for r in result_records(chain)]
        assert sources == [r.uuid for r in chain if isinstance(r, AssistantRecord)][1:5]
        assert [c.tool for c in tools.calls] == [
            "sleep",
            "lookup",
            "empty",
            "broken",
        ]
        assert sink.questions() == []

        second = server.requests()[1]["messages"]
        assert isinstance(second, list)
        tool_messages = [m for m in second if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_messages] == [u.id for u in uses]
        assert tool_messages[3]["content"].startswith("<tool_use_error>")
        again = await reloaded(tmp_path / "history", SESSION)
        assert list(again.records()) == list(session.records())

    async def test_unknown_tool_gets_an_error_result(self, agents: StandAgents) -> None:
        tools = StandTools()
        served = Served(
            agents.agent(agents.profile(Protocol.OPENAI), Collected(), tools=tools)
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(call({"name": "nope"})))
        await served.agent.settled()
        await served.close()

        found = results(session.records())
        assert [r.text() for r in found] == ["Error: No such tool available: nope"]
        assert found[0].is_error is True
        assert tools.calls == []

    async def test_large_result_is_truncated_with_a_mark(
        self, agents: StandAgents
    ) -> None:
        limits = TurnLimits(retry_delays_sec=(0.0,), tool_result_max_chars=100)
        served = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI),
                Collected(),
                limits=limits,
                tools=StandTools(),
            )
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(
            prompt(
                call({"name": StandToolName.LARGE.value, "arguments": {"size": 500}})
            )
        )
        await served.agent.settled()
        await served.close()

        text = results(session.records())[0].text()
        assert text.startswith("x" * 100)
        assert text.endswith(
            "<truncated-output>Output truncated: 100 of 500 characters shown."
            "</truncated-output>"
        )
        assert len(text) == 100 + len(
            "<truncated-output>Output truncated: 100 of 500 characters shown."
            "</truncated-output>"
        )

    async def test_duplicate_wire_ids_keep_pairs_intact(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        limits = TurnLimits(retry_delays_sec=(0.0,), max_turns=2)
        sink = Collected()
        served = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI), sink, limits=limits, tools=StandTools()
            )
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(FailureName.DUPLICATE_ID.value))
        await served.agent.settled()
        await served.close()

        uses = tool_uses(session.records())
        assert len(uses) == 4
        assert len({u.id for u in uses}) == 4
        assert {
            r.wire_tool_call_id
            for r in session.records()
            if isinstance(r, AssistantRecord) and r.wire_tool_call_id
        } == {"call_dup"}
        second = server.requests()[1]["messages"]
        assert isinstance(second, list)
        assert [m["tool_call_id"] for m in second if m["role"] == "tool"] == [
            u.id for u in uses[:2]
        ]
        assert served.agent.outcomes == [TurnOutcome.MAX_TURNS]

    async def test_looping_model_stops_at_max_turns(self, agents: StandAgents) -> None:
        limits = TurnLimits(retry_delays_sec=(0.0,), max_turns=3)
        tools = StandTools()
        sink = Collected()
        served = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI), sink, limits=limits, tools=tools
            )
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(
            prompt(
                f"{ScenarioName.LOOP.value} "
                + json.dumps({"name": StandToolName.LOOKUP.value})
            )
        )
        await served.agent.settled()
        await served.close()

        assert served.agent.outcomes == [TurnOutcome.MAX_TURNS]
        assert len(tools.calls) == 3
        assert kinds(session.chain())[-1] == "attachment:max_turns_reached"
        last = session.chain()[-1]
        assert isinstance(last, AttachmentRecord)
        assert last.rendered is None
        result_event = sink.of(ResultEvent)[0]
        assert isinstance(result_event, ResultEvent)
        assert result_event.num_turns == 4


class TestPermissions:
    async def test_denied_call_gets_the_rejection_text(
        self, agents: StandAgents
    ) -> None:
        tools = StandTools()
        sink = Collected()
        served = Served(
            agents.agent(agents.profile(Protocol.OPENAI), sink, tools=tools)
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(call({"name": StandToolName.DROP.value})))
        question = await sink.next_question(0)
        assert question.request.tool_name == "drop"
        assert question.request.permission_suggestions == [
            PermissionRule(tool="drop", behavior=PermissionBehavior.ALLOW)
        ]
        assert session.state.value == "waiting"
        assert served.agent.answer(question.request_id, DenyAnswer(message="not now"))
        await served.agent.settled()
        await served.close()

        record = result_records(session.records())[0]
        assert record.tool_denial_kind is ToolDenialKind.USER_REJECTED
        assert record.tool_use_result == "User rejected tool use"
        text = record.message.tool_results()[0].text()
        assert text.startswith("The user doesn't want to proceed with this tool use.")
        assert text.endswith("the user said: not now")
        assert tools.calls == []
        assert served.agent.outcomes == [TurnOutcome.COMPLETED]

    async def test_allow_for_the_session_is_recorded_and_not_asked_again(
        self, agents: StandAgents, tmp_path: Path
    ) -> None:
        tools = StandTools()
        sink = Collected()
        served = Served(
            agents.agent(agents.profile(Protocol.OPENAI), sink, tools=tools)
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(call({"name": StandToolName.DROP.value})))
        question = await sink.next_question(0)
        rule = PermissionRule(tool="drop", behavior=PermissionBehavior.ALLOW)
        assert served.agent.answer(
            question.request_id, AllowAnswer(updated_permissions=[rule])
        )
        await served.agent.settled()
        served.agent.queue.enqueue(prompt(call({"name": StandToolName.DROP.value})))
        await served.agent.settled()
        await served.close()

        assert len(sink.questions()) == 1
        assert [c.tool for c in tools.calls] == ["drop", "drop"]
        rules = [r for r in session.service() if isinstance(r, PermissionRuleRecord)]
        assert len(rules) == 1
        assert rules[0].rule == rule

        resumed = Served(
            agents.agent(agents.profile(Protocol.OPENAI), Collected(), tools=tools)
        )
        await resumed.open(SESSION, resume=True)
        resumed.agent.queue.enqueue(prompt(call({"name": StandToolName.DROP.value})))
        await resumed.agent.settled()
        await resumed.close()

        assert [c.tool for c in tools.calls] == ["drop", "drop", "drop"]

    async def test_mode_change_mid_session_survives_resume(
        self, agents: StandAgents
    ) -> None:
        tools = StandTools()
        sink = Collected()
        served = Served(
            agents.agent(agents.profile(Protocol.OPENAI), sink, tools=tools)
        )
        await served.open(SESSION, resume=False)
        served.agent.set_permission_mode(PermissionMode.ACCEPT_EDITS)
        served.agent.queue.enqueue(prompt(call({"name": StandToolName.NOTE.value})))
        await served.agent.settled()
        await served.close()

        assert sink.questions() == []
        assert [c.tool for c in tools.calls] == ["note"]

        again = Collected()
        resumed = Served(
            agents.agent(agents.profile(Protocol.OPENAI), again, tools=tools)
        )
        await resumed.open(SESSION, resume=True)
        assert resumed.agent.permission_mode is PermissionMode.ACCEPT_EDITS
        resumed.agent.queue.enqueue(
            prompt(
                call(
                    {"name": StandToolName.NOTE.value},
                    {"name": StandToolName.DROP.value},
                )
            )
        )
        question = await again.next_question(0)
        assert question.request.tool_name == "drop"
        resumed.agent.answer(question.request_id, DenyAnswer())
        await resumed.agent.settled()
        await resumed.close()

        assert [c.tool for c in tools.calls] == ["note", "note"]

    async def test_bypass_is_lowered_on_resume_without_the_right(
        self, agents: StandAgents
    ) -> None:
        served = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI),
                Collected(),
                tools=StandTools(),
                initial_mode=PermissionMode.BYPASS_PERMISSIONS,
                allow_bypass=True,
            )
        )
        await served.open(SESSION, resume=False)
        assert served.agent.permission_mode is PermissionMode.BYPASS_PERMISSIONS
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        resumed = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI), Collected(), tools=StandTools()
            )
        )
        session = await resumed.open(SESSION, resume=True)
        await resumed.close()

        modes = [
            r.permission_mode
            for r in session.service()
            if isinstance(r, PermissionModeRecord)
        ]
        assert modes == [PermissionMode.BYPASS_PERMISSIONS, PermissionMode.DEFAULT]
        assert resumed.agent.permission_mode is PermissionMode.DEFAULT

    async def test_auto_mode_asks_the_classifier_and_denies_on_failure(
        self, agents: StandAgents
    ) -> None:
        tools = StandTools()
        classifier = StandClassifier({"note": "allow", "drop": "deny"})
        sink = Collected()
        served = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI),
                sink,
                tools=tools,
                initial_mode=PermissionMode.AUTO,
                classifier=classifier,
            )
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(
            prompt(
                call(
                    {"name": StandToolName.NOTE.value},
                    {"name": StandToolName.DROP.value},
                    {"name": StandToolName.LOOKUP.value},
                )
            )
        )
        await served.agent.settled()
        served.agent.set_permission_mode(PermissionMode.AUTO)
        classifier_fails = StandTools()
        await served.close()

        assert sink.questions() == []
        assert [c.tool for c in tools.calls] == ["note", "lookup"]
        found = results(session.records())
        assert found[1].is_error is True
        assert found[1].text() == (
            "The permission classifier denied the call of drop: scripted for drop"
        )
        assert len(classifier.questions) == 2
        del classifier_fails

        unknown = StandClassifier({})
        other = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI),
                Collected(),
                tools=tools,
                initial_mode=PermissionMode.AUTO,
                classifier=unknown,
            )
        )
        other_session = await other.open(SessionId(value="agent-tools-2"), resume=False)
        other.agent.queue.enqueue(prompt(call({"name": StandToolName.NOTE.value})))
        await other.agent.settled()
        await other.close()

        text = results(other_session.records())[0].text()
        assert text.startswith(
            "The permission classifier could not decide on the call of note"
        )
        assert [c.tool for c in tools.calls] == ["note", "lookup"]


class TestAskUserQuestion:
    QUESTION: ClassVar[dict[str, object]] = {
        "questions": [
            {
                "question": "Which one?",
                "header": "Pick",
                "options": [
                    {"label": "A", "description": "first"},
                    {"label": "B", "description": "second"},
                ],
            }
        ]
    }

    async def test_answers_become_the_tool_result(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        sink = Collected()
        served = Served(
            agents.agent(agents.profile(Protocol.OPENAI), sink, tools=StandTools())
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(
            prompt(call({"name": AskUserQuestionTool.NAME, "arguments": self.QUESTION}))
        )
        question = await sink.next_question(0)
        assert question.request.tool_name == "AskUserQuestion"
        # стенд дописывает к аргументам вызова подпись intent
        assert question.request.input["questions"] == self.QUESTION["questions"]
        served.agent.answer(
            question.request_id,
            AllowAnswer(updated_input={"answers": {"Which one?": "A"}}),
        )
        await served.agent.settled()
        await served.close()

        record = result_records(session.records())[0]
        assert record.message.tool_results()[0].text() == (
            'Your questions have been answered: "Which one?"="A". You can now continue '
            "with these answers in mind."
        )
        assert record.message.tool_results()[0].is_error is None
        assert record.tool_use_result == {
            "questions": self.QUESTION["questions"],
            "answers": {"Which one?": "A"},
        }
        tools = server.requests()[0]["tools"]
        assert isinstance(tools, list)
        names = [t["function"]["name"] for t in tools]
        assert "AskUserQuestion" in names
        assert sorted(names) == names

    async def test_question_is_allowed_in_plan_mode(self, agents: StandAgents) -> None:
        sink = Collected()
        served = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI),
                sink,
                tools=StandTools(),
                initial_mode=PermissionMode.PLAN,
            )
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(
            prompt(
                call(
                    {"name": StandToolName.DROP.value},
                    {"name": AskUserQuestionTool.NAME, "arguments": self.QUESTION},
                )
            )
        )
        question = await sink.next_question(0)
        served.agent.answer(question.request_id, DenyAnswer())
        await served.agent.settled()
        await served.close()

        found = results(session.records())
        assert "plan mode" in found[0].text()
        assert found[1].text().startswith("The user doesn't want to proceed")


class TestQueueMidTurn:
    async def test_message_during_a_tool_is_absorbed_after_the_results(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        tools = StandTools()
        sink = Collected()
        served = Served(
            agents.agent(agents.profile(Protocol.OPENAI), sink, tools=tools)
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(
            prompt(
                call({"name": StandToolName.SLEEP.value, "arguments": {"seconds": 0.4}})
            )
        )
        while not tools.calls:
            await asyncio.sleep(0.01)

        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        assert kinds(session.chain())[-4:] == [
            "user:tool_result",
            "attachment:queued_command",
            "assistant:text",
            "assistant:text",
        ] or kinds(session.chain())[-3:] == [
            "user:tool_result",
            "attachment:queued_command",
            "assistant:text",
        ]
        assert served.agent.outcomes == [TurnOutcome.COMPLETED]
        queued = [
            r
            for r in session.chain()
            if isinstance(r, AttachmentRecord) and r.attachment.type == "queued_command"
        ]
        assert len(queued) == 1
        assert queued[0].rendered is not None
        assert (
            queued[0]
            .rendered[0]
            .content.startswith(
                "The user sent a new message while you were working:\nscenario:answer"
            )
        )
        assert "This is how Boba surfaces messages" in queued[0].rendered[0].content
        reasons = [
            r.reason
            for r in session.service()
            if isinstance(r, QueueOperationRecord) and r.reason is not None
        ]
        assert QueueRemoveReason.ABSORBED_MID_TURN in reasons
        second = server.requests()[1]["messages"]
        assert isinstance(second, list)
        assert second[-1]["role"] == "user"
        assert "scenario:answer" in second[-1]["content"]

    async def test_now_message_supersedes_the_turn(self, agents: StandAgents) -> None:
        tools = StandTools()
        sink = Collected()
        served = Served(
            agents.agent(agents.profile(Protocol.OPENAI), sink, tools=tools)
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(
            prompt(
                call({"name": StandToolName.SLEEP.value, "arguments": {"seconds": 5}})
            )
        )
        while not tools.calls:
            await asyncio.sleep(0.01)

        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value, QueuePriority.NOW))
        await served.agent.settled()
        await served.close()

        assert served.agent.outcomes == [
            TurnOutcome.ABORTED_TOOLS,
            TurnOutcome.COMPLETED,
        ]
        record = result_records(session.records())[0]
        assert record.tool_denial_kind is ToolDenialKind.INTERRUPTED
        assert (
            record.message.tool_results()[0]
            .text()
            .startswith("[Tool call did not complete: the turn was ended")
        )
        texts = [
            r.message.content
            for r in session.records()
            if isinstance(r, UserRecord) and isinstance(r.message.content, str)
        ]
        assert "[Request interrupted by user for tool use]" not in texts
        assert texts[-1] == ScenarioName.ANSWER.value

    async def test_notification_after_the_turn_starts_its_own_turn(
        self, server: FakeServer, agents: StandAgents
    ) -> None:
        sink = Collected()
        served = Served(
            agents.agent(agents.profile(Protocol.OPENAI), sink, tools=StandTools())
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        served.agent.queue.enqueue(
            QueueEntry(
                kind=QueueKind.TASK_NOTIFICATION,
                text=f"background task finished {ScenarioName.ANSWER.value}",
            )
        )
        await served.agent.settled()
        await served.close()

        assert served.agent.outcomes == [TurnOutcome.COMPLETED, TurnOutcome.COMPLETED]
        users = [r for r in session.chain() if isinstance(r, UserRecord)]
        assert users[1].is_meta is True
        assert users[1].origin is not None
        assert users[1].origin.kind.value == "task-notification"
        assert len(server.requests()) == 2


class TestCancellation:
    async def test_interrupt_while_waiting_for_permission(
        self, agents: StandAgents
    ) -> None:
        tools = StandTools()
        sink = Collected()
        served = Served(
            agents.agent(agents.profile(Protocol.OPENAI), sink, tools=tools)
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(
            prompt(
                call(
                    {"name": StandToolName.DROP.value},
                    {"name": StandToolName.NOTE.value},
                )
            )
        )
        question = await sink.next_question(0)
        assert served.agent.interrupt()
        await served.agent.settled()
        late = served.agent.answer(question.request_id, AllowAnswer())
        await served.close()

        assert late is False
        cancelled = sink.of(ControlCancelEvent)
        assert len(cancelled) == 1
        assert isinstance(cancelled[0], ControlCancelEvent)
        assert cancelled[0].request_id == question.request_id
        assert tools.calls == []
        records = result_records(session.records())
        assert [r.tool_denial_kind for r in records] == [
            ToolDenialKind.USER_REJECTED,
            ToolDenialKind.USER_REJECTED,
        ]
        last = session.chain()[-1]
        assert isinstance(last, UserRecord)
        assert last.message.content == "[Request interrupted by user for tool use]"
        assert served.agent.outcomes == [TurnOutcome.ABORTED_TOOLS]
        assert served.agent.answer("req_unknown", AllowAnswer()) is False

    async def test_second_answer_is_dropped(self, agents: StandAgents) -> None:
        sink = Collected()
        served = Served(
            agents.agent(agents.profile(Protocol.OPENAI), sink, tools=StandTools())
        )
        await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt(call({"name": StandToolName.DROP.value})))
        question = await sink.next_question(0)
        first = served.agent.answer(question.request_id, DenyAnswer())
        second = served.agent.answer(question.request_id, AllowAnswer())
        await served.agent.settled()
        await served.close()

        assert (first, second) == (True, False)

    async def test_close_during_a_tool_writes_shutdown_records(
        self, agents: StandAgents, tmp_path: Path
    ) -> None:
        tools = StandTools()
        served = Served(
            agents.agent(agents.profile(Protocol.OPENAI), Collected(), tools=tools)
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(
            prompt(
                call({"name": StandToolName.SLEEP.value, "arguments": {"seconds": 5}})
            )
        )
        while not tools.calls:
            await asyncio.sleep(0.01)

        await served.close()

        assert served.agent.outcomes == [TurnOutcome.SHUTDOWN]
        record = result_records(session.records())[0]
        assert record.tool_denial_kind is ToolDenialKind.USER_REJECTED
        last = session.chain()[-1]
        assert isinstance(last, UserRecord)
        assert last.interrupted_by_shutdown is True
        again = await reloaded(tmp_path / "history", SESSION)
        assert list(again.records())[: len(session.records())] == list(
            session.records()
        )
        assert kinds(again.chain())[-1] == "assistant:text"


class TestResume:
    async def test_process_killed_during_a_tool_does_not_rerun_it(
        self, agents: StandAgents, tmp_path: Path
    ) -> None:
        tools = StandTools()
        served = Served(
            agents.agent(agents.profile(Protocol.OPENAI), Collected(), tools=tools)
        )
        await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(call({"name": StandToolName.LOOKUP.value})))
        await served.agent.settled()
        await served.close()

        journal = tmp_path / "history" / f"{SESSION.value}.jsonl"
        lines = journal.read_text(encoding="utf-8").splitlines()
        kept: list[str] = []
        for line in lines:
            document = json.loads(line)
            if document["type"] == "user" and not isinstance(
                document["message"]["content"], str
            ):
                break

            kept.append(line)

        journal.write_text("\n".join(kept) + "\n", encoding="utf-8")
        fresh = StandTools()
        resumed = Served(
            agents.agent(agents.profile(Protocol.OPENAI), Collected(), tools=fresh)
        )
        session = await resumed.open(SESSION, resume=True)
        resumed.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await resumed.agent.settled()
        await resumed.close()

        assert fresh.calls == []
        stub = result_records(session.records())[0]
        assert stub.tool_denial_kind is ToolDenialKind.INTERRUPTED
        assert (
            stub.message.tool_results()[0]
            .text()
            .startswith("[Tool call interrupted: the session ended")
        )
        assert resumed.agent.outcomes == [TurnOutcome.COMPLETED]
        assert_linked(session.chain())


class TestMcp:
    def _server(self, prefix: str) -> McpToolServer:
        endpoint = StdioCommand(command=sys.executable, args=(str(MCP_SERVER), "stdio"))
        config = McpServerConfig(
            endpoint=endpoint,
            prefix=prefix,
            connect_timeout_sec=20.0,
            call_timeout_sec=20.0,
        )

        return McpToolServer(
            prefix.rstrip("_"),
            config,
            NamedBlocks(),
            DroppedSignals(),
            CallContexts(),
            None,
            DirectCalls(),
        )

    async def test_crash_of_one_server_fails_only_its_call(
        self, agents: StandAgents
    ) -> None:
        first = self._server("a_")
        second = self._server("b_")
        await first.open()
        await second.open()
        routed = RoutedServers([first, second])
        sink = Collected()
        served = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI),
                sink,
                tools=routed,
                initial_mode=PermissionMode.BYPASS_PERMISSIONS,
                allow_bypass=True,
            )
        )
        session = await served.open(SESSION, resume=False)
        try:
            served.agent.queue.enqueue(
                prompt(
                    call(
                        {"name": "a_add", "arguments": {"a": 1, "b": 2}},
                        {"name": "b_die"},
                    )
                )
            )
            await served.agent.settled()
        finally:
            await served.close()
            await first.close()
            await second.close()

        found = results(session.records())
        assert found[0].text() == "3"
        assert found[0].is_error is None
        assert found[1].is_error is True
        assert "b_die" in found[1].text() or "die" in found[1].text()
        assert served.agent.outcomes == [TurnOutcome.COMPLETED]
        assert sink.questions() == []
