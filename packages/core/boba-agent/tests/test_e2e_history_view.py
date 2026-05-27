"""E2E smoke: AgentBuilder с fake-LLM прогоняет миграцию на HistoryService.

Проверяет ключевую инвариантность миграции: после Agent.stream(query)
журнал `HistoryService` содержит достаточно событий, чтобы
`AllHistoryDialogView` восстановил полный диалог, а следующий вызов
`Agent.stream(query2)` увидел эту историю в `LLMContext.request.dialog_messages`.
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.agent import (
    AgentBuilder,
    AllHistoryDialogView,
    AnswerMessage,
    TurnBuilder,
    UserQueryReceived,
)
from boba.agent.history import HistoryReader, HistoryWriter
from boba.llm.builder import LLM
from boba.llm.events import (
    FinishReason,
    LLMAnswerDelta,
    LLMAnswerMessage,
    LLMEvent,
    LLMGenerationResult,
)
from boba.llm.models import (
    AssistantMessage,
    AssistantMessageChunk,
    DialogMessage,
    LLMContext,
    UserMessage,
)
from boba.patterns import StreamSource


class _StubLLMSource(StreamSource[LLMContext, LLMEvent]):
    """Фейковый LLM-источник: запоминает контексты, эмитит scripted ответы."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.contexts: list[LLMContext] = []

    def name(self) -> str:
        return "StubLLM"

    def reset(self) -> None:
        pass

    def stream(self, ctx: LLMContext) -> Iterable[LLMEvent]:
        self.contexts.append(ctx)
        rid = ctx.request.request_id
        answer = self._answers.pop(0) if self._answers else ""
        chunk = AssistantMessageChunk.empty()
        for token in answer:
            chunk.append_text(token)
            yield LLMAnswerDelta(request_id=rid, token=token)
        message = chunk.finalize()
        if message.content:
            yield LLMAnswerMessage(request_id=rid, content=message.content)
        yield LLMGenerationResult(
            request_id=rid, message=message, finish_reason=FinishReason.STOP
        )


def _dialog_texts(messages: tuple[DialogMessage, ...]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in messages:
        if isinstance(m, UserMessage):
            out.append(("user", m.content))
        elif isinstance(m, AssistantMessage):
            out.append(("assistant", m.content))
    return out


def test_history_journal_contains_user_query_and_assistant_snapshots():
    """Журнал должен содержать UserQueryReceived + AnswerMessage после одного хода."""
    stub = _StubLLMSource(answers=["hi back"])
    llm = LLM(source=stub)

    agent = (
        AgentBuilder()
        .use_llm(llm)
        .use_turn(TurnBuilder("stub-model"))
        .build()
    )

    events = list(agent.stream("hi"))

    types = [type(e).__name__ for e in events]
    assert "UserQueryReceived" in types
    assert "AnswerMessage" in types

    history = agent.container.get(HistoryReader)
    user_queries = [e for e in history.events() if isinstance(e, UserQueryReceived)]
    assert len(user_queries) == 1
    assert user_queries[0].query == "hi"

    answer_completes = [e for e in history.events() if isinstance(e, AnswerMessage)]
    assert len(answer_completes) == 1
    assert answer_completes[0].content == "hi back"


def test_history_view_reconstructs_full_dialog_after_run():
    """AllHistoryDialogView отдаёт UserMessage + AssistantMessage из журнала."""
    stub = _StubLLMSource(answers=["pong"])
    llm = LLM(source=stub)

    agent = (
        AgentBuilder()
        .use_llm(llm)
        .use_turn(TurnBuilder("stub-model"))
        .build()
    )

    list(agent.stream("ping"))

    view = AllHistoryDialogView(agent.container.get(HistoryReader))
    dialog = list(view.dialog_message_iter())
    assert _dialog_texts(tuple(dialog)) == [("user", "ping"), ("assistant", "pong")]


def test_second_turn_sees_prior_dialog_in_llm_request():
    """Второй .stream(...) должен видеть весь предыдущий диалог в LLMContext."""
    stub = _StubLLMSource(answers=["A1", "A2"])
    llm = LLM(source=stub)

    agent = (
        AgentBuilder()
        .use_llm(llm)
        .use_turn(TurnBuilder("stub-model"))
        .build()
    )

    list(agent.stream("q1"))
    list(agent.stream("q2"))

    assert len(stub.contexts) == 2
    first_messages = stub.contexts[0].request.dialog_messages
    second_messages = stub.contexts[1].request.dialog_messages

    assert _dialog_texts(first_messages) == [("user", "q1")]
    assert _dialog_texts(second_messages) == [
        ("user", "q1"),
        ("assistant", "A1"),
        ("user", "q2"),
    ]


def test_clear_history_resets_dialog_for_subsequent_turn():
    """history.clear() стирает контекст; следующий ход видит только свой query."""
    stub = _StubLLMSource(answers=["A1", "A2"])
    llm = LLM(source=stub)

    agent = (
        AgentBuilder()
        .use_llm(llm)
        .use_turn(TurnBuilder("stub-model"))
        .build()
    )

    list(agent.stream("q1"))
    agent.container.get(HistoryWriter).clear()
    list(agent.stream("q2"))

    assert _dialog_texts(stub.contexts[1].request.dialog_messages) == [("user", "q2")]
