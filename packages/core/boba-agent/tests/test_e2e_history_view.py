"""E2E smoke: AgentBuilder с fake-LLM прогоняет миграцию на HistoryService.

Проверяет ключевую инвариантность миграции: после Agent.stream(query)
журнал `HistoryService` содержит достаточно событий, чтобы
`HistoryDialogView` восстановил полный диалог, а следующий вызов
`Agent.stream(query2)` увидел эту историю в `LLMContext.request.dialog_messages`.
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.agent import (
    AgentBuilder,
    AnswerComplete,
    HistoryDialogView,
    InMemoryHistoryService,
    TurnBuilder,
    UserQueryReceived,
)
from boba.llm.builder import LLM
from boba.llm.events import (
    FinishReason,
    LLMAnswerStarted,
    LLMAnswerToken,
    LLMEvent,
    LLMGenerationDone,
    LLMGenerationStarted,
    LLMRequestStarted,
    LLMResponseStarted,
)
from boba.llm.middleware import AssistantAggregator
from boba.llm.models import (
    AssistantMessage,
    DialogMessage,
    LLMContext,
    UserMessage,
)
from boba.patterns import StreamSource
from boba.tools.domain import ToolSourceId
from boba.tools.framework import StaticToolSource, ToolRegistry


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
        yield LLMRequestStarted(
            request_id=rid,
            model=ctx.request.model,
            messages_count=len(ctx.request.dialog_messages),
            has_tools=ctx.request.has_tools(),
            monotonic_ns=0,
        )
        yield LLMResponseStarted(request_id=rid, monotonic_ns=1)
        yield LLMGenerationStarted(request_id=rid)
        yield LLMAnswerStarted(request_id=rid)
        for token in answer:
            yield LLMAnswerToken(request_id=rid, token=token)
        yield LLMGenerationDone(request_id=rid, finish_reason=FinishReason.STOP)


def _empty_registry() -> ToolRegistry:
    return ToolRegistry.from_sources([StaticToolSource(ToolSourceId("empty"), [])])


def _dialog_texts(messages: tuple[DialogMessage, ...]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in messages:
        if isinstance(m, UserMessage):
            out.append(("user", m.content))
        elif isinstance(m, AssistantMessage):
            out.append(("assistant", m.content))
    return out


def test_history_journal_contains_user_query_and_assistant_snapshots():
    """Журнал должен содержать UserQueryReceived + AnswerComplete после одного хода."""
    stub = _StubLLMSource(answers=["hi back"])
    llm = LLM(source=AssistantAggregator(stub))
    history = InMemoryHistoryService()

    agent = (
        AgentBuilder()
        .with_llm(llm)
        .with_history(history)
        .with_tools(_empty_registry())
        .use_turn(TurnBuilder("stub-model"))
        .build()
    )

    events = list(agent.stream("hi"))

    types = [type(e).__name__ for e in events]
    assert "UserQueryReceived" in types
    assert "AnswerComplete" in types

    user_queries = [e for e in history.events() if isinstance(e, UserQueryReceived)]
    assert len(user_queries) == 1
    assert user_queries[0].query == "hi"

    answer_completes = [e for e in history.events() if isinstance(e, AnswerComplete)]
    assert len(answer_completes) == 1
    assert answer_completes[0].content == "hi back"


def test_history_view_reconstructs_full_dialog_after_run():
    """HistoryDialogView отдаёт UserMessage + AssistantMessage из журнала одного хода."""
    stub = _StubLLMSource(answers=["pong"])
    llm = LLM(source=AssistantAggregator(stub))
    history = InMemoryHistoryService()

    agent = (
        AgentBuilder()
        .with_llm(llm)
        .with_history(history)
        .with_tools(_empty_registry())
        .use_turn(TurnBuilder("stub-model"))
        .build()
    )

    list(agent.stream("ping"))

    view = HistoryDialogView(history)
    dialog = list(view.dialog_message_iter())
    assert _dialog_texts(tuple(dialog)) == [("user", "ping"), ("assistant", "pong")]


def test_second_turn_sees_prior_dialog_in_llm_request():
    """Второй .stream(...) должен видеть весь предыдущий диалог в LLMContext."""
    stub = _StubLLMSource(answers=["A1", "A2"])
    llm = LLM(source=AssistantAggregator(stub))
    history = InMemoryHistoryService()

    agent = (
        AgentBuilder()
        .with_llm(llm)
        .with_history(history)
        .with_tools(_empty_registry())
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
    llm = LLM(source=AssistantAggregator(stub))
    history = InMemoryHistoryService()

    agent = (
        AgentBuilder()
        .with_llm(llm)
        .with_history(history)
        .with_tools(_empty_registry())
        .use_turn(TurnBuilder("stub-model"))
        .build()
    )

    list(agent.stream("q1"))
    history.clear()
    list(agent.stream("q2"))

    assert _dialog_texts(stub.contexts[1].request.dialog_messages) == [("user", "q2")]
