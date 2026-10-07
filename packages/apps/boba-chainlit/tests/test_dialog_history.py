"""История треда на стыке чата и графа: что агент сессии пишет в checkpoint'ы
и какими сообщениями диалога отдаёт их чату.

Формат записи менять нельзя: треды боевой базы читаются и дописываются тем
же кодом. Тест держит его побуквенно — ключи additional_kwargs и
response_metadata сообщений langchain — на настоящем графе с InMemorySaver.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

import pytest
from chainlit_stand import SilentStage
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from boba.chainlit.agent.bridge import LangchainMessages
from boba.chainlit.agent.flow import GraphSpec, HistoryView, PlainGraphBuilder
from boba.chainlit.agent.history import CheckpointMessages, GraphAgent
from boba.chainlit.chat.dialog import (
    AssistantMessage,
    Attachment,
    LlmUsage,
    ModelCallFinished,
    ModelCallStarted,
    ModelListener,
    ModelToken,
    ToolReply,
    TurnMark,
    TurnRecord,
    UserMessage,
)
from boba.chainlit.chat.history import (
    ConversationTranscript,
    InterruptedTurn,
    ThreadTurnHistory,
)
from boba.chainlit.rendering.chat_view import ChatView, RecordingSink
from boba.stand.refs import StandRefs
from boba.toolkit.chain import CallAmbient
from boba.toolkit.dag import (
    DagNode,
    JournalAddress,
    NodeOutcomes,
    WorkflowNodeResult,
    WorkflowResult,
)
from boba.toolkit.ports import StreamSpecs
from boba.toolkit.result import MarkdownResult
from boba.toolrun.hosted import DirectCalls, ToolHosting
from boba.toolrun.stream_calls import LocalDagService

pytestmark = pytest.mark.anyio

THREAD = "66666666-6666-6666-6666-666666666666"
CONFIG = RunnableConfig(configurable={"thread_id": THREAD})


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class ScriptedChat(GenericFakeChatModel):
    """Модель по сценарию: bind_tools у фейка не реализован."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


class HeardCalls(ModelListener):
    """Слушатель хода: запоминает события обращений к модели по порядку."""

    def __init__(self) -> None:
        self.started: list[ModelCallStarted] = []
        self.tokens: list[ModelToken] = []
        self.finished: list[ModelCallFinished] = []
        self.failed: list[str] = []

    async def model_called(self, call: ModelCallStarted) -> None:
        self.started.append(call)

    async def model_token(self, token: ModelToken) -> None:
        self.tokens.append(token)

    async def model_replied(self, finish: ModelCallFinished) -> None:
        self.finished.append(finish)

    async def model_failed(self, run: str, error: BaseException) -> None:
        self.failed.append(run)


class Session:
    """Агент сессии на настоящем графе хода: модель по сценарию, checkpoint'ы
    в памяти. Отдаёт и агента, и сырое состояние графа — тест сверяет, что
    лежит в checkpoint'е."""

    def __init__(self, answers: Sequence[AIMessage]) -> None:
        self.checkpoints = CheckpointMessages(InMemorySaver())
        spec = GraphSpec(
            chat=ScriptedChat(messages=iter(answers)),
            service=LocalDagService(
                ToolHosting().toolset([]),
                StandRefs.STREAM_CONFIG,
                (),
                StreamSpecs({}),
                CallAmbient(),
                DirectCalls(),
            ),
            system_prompt="answer briefly",
            checkpoints=self.checkpoints,
            history=HistoryView(frozenset(), 30),
        )
        self.graph: CompiledStateGraph = PlainGraphBuilder().build(spec)
        self.agent = GraphAgent(self.graph)

    async def ask(
        self, question: UserMessage, listeners: Sequence[ModelListener] = ()
    ) -> list[str]:
        """Ход до конца; токены ответа, которые отдал поток."""
        texts: list[str] = []
        stream = self.agent.answer(THREAD, question, listeners, SilentStage())
        async for chunk in stream:
            if chunk.text:
                texts.append(chunk.text)

        return texts

    async def stored(self) -> list[BaseMessage]:
        state = await self.graph.aget_state(CONFIG)

        return list(state.values["messages"])


class TestWrittenFormat:
    """Что чат пишет в checkpoint'ы: вопрос, запись оборванного хода, вызовы
    подготовки и итог вызова инструмента."""

    ATTACHMENT: ClassVar[Attachment] = Attachment(
        name="data.csv", path="/workspace/t-1/upload/data.csv"
    )

    async def test_question_carries_attachments_beside_the_text(self) -> None:
        session = Session([AIMessage(content="looked at it")])
        question = UserMessage(
            id="q1", text="разбери файл", attachments=(self.ATTACHMENT,)
        )

        await session.ask(question)

        asked = (await session.stored())[0]
        assert isinstance(asked, HumanMessage)
        assert asked.id == "q1"
        assert asked.content == "разбери файл"
        assert asked.additional_kwargs == {
            "attachments": [
                {"name": "data.csv", "path": "/workspace/t-1/upload/data.csv"}
            ]
        }

    async def test_question_without_attachments_has_no_marks(self) -> None:
        session = Session([AIMessage(content="hello")])

        await session.ask(UserMessage(id="q1", text="привет"))

        asked = (await session.stored())[0]
        assert asked.additional_kwargs == {}

    async def test_stopped_record_keeps_mark_and_reasoning(self) -> None:
        session = Session([AIMessage(content="hello")])
        await session.ask(UserMessage(id="q1", text="привет"))
        record = TurnRecord(
            content="partial\n\n_stopped by the user_",
            mark=TurnMark.STOPPED,
            reasoning="unfinished thoughts",
        )

        await ThreadTurnHistory(session.agent, THREAD).remember(record)

        written = (await session.stored())[-1]
        assert isinstance(written, AIMessage)
        assert written.content == "partial\n\n_stopped by the user_"
        assert written.additional_kwargs == {
            "stopped": True,
            "reasoning_content": "unfinished thoughts",
        }

    async def test_failed_record_keeps_only_the_mark(self) -> None:
        session = Session([AIMessage(content="hello")])
        await session.ask(UserMessage(id="q1", text="привет"))
        record = TurnRecord(content="**failed:** boom", mark=TurnMark.ERROR)

        await ThreadTurnHistory(session.agent, THREAD).remember(record)

        written = (await session.stored())[-1]
        assert written.additional_kwargs == {"error": True}

    def test_prefetch_request_carries_empty_reasoning_and_elapsed(self) -> None:
        call = DagNode(key="prefetch_1", tool="kb_search", args={"query": "kerberos"})

        request = LangchainMessages().prefetch_request([call], 6400)

        assert request.content == ""
        assert request.additional_kwargs == {
            "reasoning_content": "",
            "prefetch_elapsed_ms": 6400,
        }
        assert request.tool_calls == [
            {
                "name": "kb_search",
                "args": {"query": "kerberos"},
                "id": "prefetch_1",
                "type": "tool_call",
            }
        ]

    def test_tool_message_keeps_the_journal_address_in_metadata(self) -> None:
        call = DagNode(key="call_1", tool="bash")
        outcome = NodeOutcomes().of(call, MarkdownResult(text="done"), False)
        address = JournalAddress(server="general", run="run-7", call="api-7")
        addressed = outcome.model_copy(update={"journal": address})

        message = LangchainMessages().tool_message(addressed)

        assert message.tool_call_id == "call_1"
        assert message.name == "bash"
        assert message.status == "success"
        assert message.response_metadata == {
            "boba_journal": {"server": "general", "run": "run-7", "call": "api-7"}
        }


class TestDialogReading:
    """Какими сообщениями диалога чат видит то, что лежит в checkpoint'е."""

    async def test_turn_is_read_back_as_dialog_messages(self) -> None:
        session = Session([AIMessage(content="looked at it")])
        attachment = Attachment(name="a.csv", path="/workspace/t-1/upload/a.csv")
        question = UserMessage(id="q1", text="разбери", attachments=(attachment,))
        await session.ask(question)
        record = TurnRecord(content="_stopped_", mark=TurnMark.STOPPED)
        await session.agent.remember(THREAD, record)

        from_graph = await session.agent.load(THREAD)
        from_store = await session.checkpoints.load(THREAD)

        assert list(from_graph) == list(from_store)
        assert from_graph[0] == question
        answer = from_graph[1]
        assert isinstance(answer, AssistantMessage)
        assert answer.text == "looked at it"
        assert answer.mark is None
        stopped = from_graph[2]
        assert isinstance(stopped, AssistantMessage)
        assert stopped.text == "_stopped_"
        assert stopped.mark is TurnMark.STOPPED

    def test_assistant_message_fields_come_from_the_marks(self) -> None:
        message = AIMessage(
            content=[{"type": "text", "text": "часть "}, "ответа"],
            id="a1",
            additional_kwargs={"reasoning_content": "думаю", "prefetch_elapsed_ms": 12},
            tool_calls=[
                {"name": "bash", "args": {"cmd": "ls"}, "id": "call_1"},
                {"name": "bash", "args": {}, "id": None},
            ],
            usage_metadata={
                "input_tokens": 10,
                "output_tokens": 4,
                "total_tokens": 14,
                "output_token_details": {"reasoning": 3},
            },
        )

        read = LangchainMessages().assistant(message)

        assert read == AssistantMessage(
            id="a1",
            text="часть ответа",
            reasoning="думаю",
            calls=(DagNode(key="call_1", tool="bash", args={"cmd": "ls"}),),
            usage=LlmUsage(input_tokens=10, output_tokens=4, reasoning_tokens=3),
            mark=None,
            prefetch_elapsed_ms=12,
        )

    def test_failed_mark_wins_over_stopped(self) -> None:
        message = AIMessage(
            content="x", additional_kwargs={"stopped": True, "error": True}
        )

        assert LangchainMessages().assistant(message).mark is TurnMark.ERROR

    def test_tool_reply_of_the_family_carries_outcome_and_journal(self) -> None:
        messages = LangchainMessages()
        call = DagNode(key="call_1", tool="bash")
        outcome = NodeOutcomes().of(call, MarkdownResult(text="done"), False)
        address = JournalAddress(server="general", run="run-7", call="api-7")
        addressed = outcome.model_copy(update={"journal": address})

        reply = messages.reply(messages.tool_message(addressed))

        assert reply.call_id == "call_1"
        assert reply.name == "bash"
        assert reply.journal == address
        assert reply.outcome == addressed

    def test_workflow_reply_addresses_the_journal_of_every_node(self) -> None:
        """Запуск у связки один, а журнал у каждого узла свой — под
        идентификатором вызова узла."""
        messages = LangchainMessages()
        call = DagNode(key="call_1", tool="workflow")
        node = WorkflowNodeResult(
            key="load",
            call_id="call_1_0",
            tool="bash",
            args={},
            errored=False,
            content="done",
            result=MarkdownResult(text="done"),
        )
        outcome = NodeOutcomes().of(call, WorkflowResult(nodes=[node]), False)
        address = JournalAddress(server="general", run="run-7", call="api-7")
        addressed = outcome.model_copy(update={"journal": address})

        reply = messages.reply(messages.tool_message(addressed))

        assert reply.journals() == {
            "call_1": address,
            "call_1_0": JournalAddress(server="general", run="run-7", call="call_1_0"),
        }

    def test_old_record_without_a_family_result_is_kept_raw(self) -> None:
        messages = LangchainMessages()
        plain = ToolMessage(content="rows: 3", tool_call_id="call_1", id="t1")
        shaped = ToolMessage(
            content="rows: 3",
            tool_call_id="call_2",
            id="t2",
            artifact={"rows": [1, 2, 3]},
            response_metadata={"boba_journal": {"server": "general", "run": "r"}},
        )

        assert messages.reply(plain) == ToolReply(
            id="t1",
            call_id="call_1",
            name="",
            text="rows: 3",
            outcome=None,
            raw="rows: 3",
            journal=None,
            media=None,
        )
        read = messages.reply(shaped)
        assert read.outcome is None
        assert read.raw == {"rows": [1, 2, 3]}
        # адрес старого формата не называет вызов на сервере: журнала нет
        assert read.journal is None

    def test_service_messages_are_not_a_part_of_the_dialog(self) -> None:
        stored = [SystemMessage(content="rules"), HumanMessage(content="q", id="q1")]

        read = list(LangchainMessages().dialog(stored))

        assert read == [UserMessage(id="q1", text="q")]


class TestCallInputFromTheHistory:
    """Вход шага вызова инструмента сервера история рисует по сохранённым
    вместе с итогом типам содержимого аргументов — так же, как живой ход, и
    без сведений, которые есть только у собранной сессии."""

    ARGS: ClassVar[dict[str, Any]] = {"sql": "select 1", "limit": 5}
    MEDIA: ClassVar[dict[str, str]] = {"sql": "application/sql"}

    @staticmethod
    def _stored(media: dict[str, dict[str, str]] | None) -> list[Any]:
        call = DagNode(key="call_1", tool="remote_query_of_a_server", args={})
        outcome = NodeOutcomes().of(call, MarkdownResult(text="1 row"), False)
        outcome = outcome.model_copy(update={"media": media})
        asked = AIMessage(
            content="",
            id="a1",
            tool_calls=[
                {
                    "name": "remote_query_of_a_server",
                    "args": TestCallInputFromTheHistory.ARGS,
                    "id": "call_1",
                    "type": "tool_call",
                }
            ],
        )

        return [
            HumanMessage(content="q", id="q1"),
            asked,
            LangchainMessages().tool_message(outcome),
        ]

    @staticmethod
    async def _replayed(stored: list[Any], runtime_stand: StandRefs) -> str:
        sink = RecordingSink()
        view = ChatView(THREAD, sink, runtime_stand.journals)
        dialog = list(LangchainMessages().dialog(stored))
        await ConversationTranscript(dialog, view).replay()

        inputs: list[str] = []
        for step in sink.steps:
            shown = step.get("input")
            if shown:
                inputs.append(str(shown))

        if len(inputs) != 1:
            raise AssertionError(f"one tool step with an input: {sink.steps}")

        return inputs[0]

    async def test_history_shows_the_arguments_as_the_live_turn_does(
        self, runtime_stand: StandRefs
    ) -> None:
        live_view = ChatView(THREAD, RecordingSink(), runtime_stand.journals)
        live_view.begin_turn("turn-live")
        live = await live_view.tool_started(
            "remote_query_of_a_server", self.ARGS, "call_1", self.MEDIA
        )

        replayed = await self._replayed(
            self._stored({"call_1": self.MEDIA}), runtime_stand
        )

        if not replayed.startswith("```sql\nselect 1\n```"):
            raise AssertionError(f"the query is a code block of sql: {replayed!r}")
        if replayed != live.input:
            raise AssertionError(
                f"history differs from the live turn: {replayed!r} != {live.input!r}"
            )

    async def test_record_without_the_types_is_shown_as_json(
        self, runtime_stand: StandRefs
    ) -> None:
        replayed = await self._replayed(self._stored(None), runtime_stand)

        if not replayed.startswith("```json"):
            raise AssertionError(f"nothing stored, nothing guessed: {replayed!r}")


class TestOldRecordInTheFeed:
    """Запись старой истории без результата семейства рисуется как есть."""

    @staticmethod
    async def _finished(raw: object, runtime_stand: StandRefs) -> tuple[Any, Any]:
        view = ChatView(THREAD, RecordingSink(), runtime_stand.journals)
        step = await view.tool_started("demo", {}, "call_1", None)
        await view.tool_finished(step, raw, "call_1")

        return step.output, step.language

    async def test_text_is_shown_as_text(self, runtime_stand: StandRefs) -> None:
        shown = await self._finished("rows: 3", runtime_stand)

        # шаг chainlit хранит выход-объект текстом JSON
        assert shown == ('{\n    "content": "rows: 3"\n}', "text")

    async def test_object_is_shown_as_json(self, runtime_stand: StandRefs) -> None:
        shown = await self._finished({"rows": [1, "два"]}, runtime_stand)

        assert shown == ('{"rows": [1, "\\u0434\\u0432\\u0430"]}', "json")


class TestTurnOnTheGraph:
    """Ход на агенте сессии: поток ответа и события слушателям."""

    async def test_answer_tokens_and_model_events_reach_the_chat(self) -> None:
        session = Session([AIMessage(content="first second")])
        heard = HeardCalls()

        texts = await session.ask(UserMessage(id="q1", text="привет"), [heard])

        assert "".join(texts) == "first second"
        assert len(heard.started) == 1
        assert heard.started[0].messages == 2
        assert len(heard.finished) == 1
        assert heard.finished[0].run == heard.started[0].run
        reply = heard.finished[0].reply
        assert reply is not None
        assert reply.text == "first second"
        assert heard.failed == []

        spoken: list[str] = []
        for token in heard.tokens:
            assert token.run == heard.started[0].run
            spoken.append(token.text)

        assert "".join(spoken) == "first second"

    async def test_interrupted_turn_is_marked_once(self) -> None:
        session = Session([AIMessage(content="hello")])
        await session.ask(UserMessage(id="q1", text="привет"))
        interrupted = InterruptedTurn(session.agent, THREAD)

        first = await interrupted.remember("holder is gone")
        second = await interrupted.remember("holder is gone")

        assert (first, second) == (True, False)
        last = (await session.stored())[-1]
        assert last.content == "_holder is gone_"
        assert last.additional_kwargs == {"stopped": True}
