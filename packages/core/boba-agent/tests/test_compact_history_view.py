"""Тесты CompactHistoryDialogView: сжатие прошлых request_id + sliding window."""

from __future__ import annotations

from boba.agent.events import (
    ToolResultReady,
    TotalMessage,
    UserQueryReceived,
)
from boba.agent.history import InMemoryHistoryService
from boba.agent.models import ToolCallResult
from boba.agent.turn.history_view import CompactHistoryDialogView
from boba.llm.events import FinishReason
from boba.llm.models import (
    AssistantMessage,
    DialogMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    new_request_id,
)
from boba.tools.domain import TextResult


class _TurnRecorder:
    """Тестовый хелпер: записывает один полный turn в InMemoryHistoryService."""

    @staticmethod
    def record(  # noqa: PLR0913
        history: InMemoryHistoryService,
        request_id,
        *,
        query: str,
        thinking: str | None = None,
        answer: str | None = None,
        tool_calls: tuple[ToolCall, ...] = (),
        tool_results: tuple[tuple[ToolCall, str], ...] = (),
        finish_reason: FinishReason = FinishReason.STOP,
    ) -> None:
        history.record(UserQueryReceived(request_id=request_id, query=query))
        history.record(
            TotalMessage(
                request_id=request_id,
                message=AssistantMessage(
                    content=answer or "",
                    thinking=thinking or "",
                    tool_calls=tuple(tool_calls),
                ),
                finish_reason=finish_reason,
            ),
        )
        for call, text in tool_results:
            history.record(
                ToolResultReady(
                    request_id=request_id,
                    call=call,
                    result=ToolCallResult(result=TextResult(text=text)),
                ),
            )


def _summarize(messages: list[DialogMessage]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in messages:
        if isinstance(m, UserMessage):
            out.append(("user", m.content))
        elif isinstance(m, AssistantMessage):
            out.append(("assistant", m.content))
        elif isinstance(m, ToolResultMessage):
            out.append(("tool", m.tool_call_id))
    return out


def test_single_request_id_returns_full_dialog():
    """Один request_id — view возвращает полный диалог как All-view."""
    history = InMemoryHistoryService()
    rid = new_request_id()
    _TurnRecorder.record(history, rid, query="hi", answer="hello")

    view = CompactHistoryDialogView(history, max_messages=10)
    assert _summarize(list(view.dialog_message_iter())) == [
        ("user", "hi"),
        ("assistant", "hello"),
    ]


def test_old_request_id_keeps_only_user_and_text_answer():
    """Прошлый request_id: thinking/tool_calls удалены, остаётся text-ответ."""
    history = InMemoryHistoryService()
    rid_old = new_request_id()
    rid_new = new_request_id()

    call = ToolCall(id="c1", name="echo", args={"x": 1})
    _TurnRecorder.record(
        history,
        rid_old,
        query="q-old",
        thinking="reasoning...",
        answer="final-old",
        tool_calls=(call,),
        tool_results=((call, "tool-out"),),
    )
    _TurnRecorder.record(history, rid_new, query="q-new", answer="final-new")

    view = CompactHistoryDialogView(history, max_messages=10)
    assert _summarize(list(view.dialog_message_iter())) == [
        ("user", "q-old"),
        ("assistant", "final-old"),
        ("user", "q-new"),
        ("assistant", "final-new"),
    ]


def test_current_request_id_keeps_full_tool_chain():
    """Текущий request_id оставляется как есть: thinking, tool_calls, results."""
    history = InMemoryHistoryService()
    rid_old = new_request_id()
    rid_new = new_request_id()

    _TurnRecorder.record(history, rid_old, query="q-old", answer="a-old")

    call = ToolCall(id="c1", name="echo", args={"x": 1})
    _TurnRecorder.record(
        history,
        rid_new,
        query="q-new",
        thinking="think-new",
        answer="a-new",
        tool_calls=(call,),
        tool_results=((call, "tool-out"),),
    )

    view = CompactHistoryDialogView(history, max_messages=20)
    messages = list(view.dialog_message_iter())

    summary = _summarize(messages)
    assert summary[0] == ("user", "q-old")
    assert summary[1] == ("assistant", "a-old")
    assert summary[2] == ("user", "q-new")
    # для текущего request_id ассистент содержит thinking+answer+tool_call,
    # а после него идёт tool_result
    assert summary[3][0] == "assistant"
    assert summary[3][1] == "a-new"
    assert summary[4] == ("tool", "c1")
    last_assistant = messages[3]
    assert isinstance(last_assistant, AssistantMessage)
    assert last_assistant.thinking == "think-new"
    assert last_assistant.tool_calls == (call,)


def test_old_request_id_without_answer_keeps_only_user_message():
    """Прошлый request_id без AnswerMessage -> assistant не добавляется."""
    history = InMemoryHistoryService()
    rid_old = new_request_id()
    rid_new = new_request_id()

    call = ToolCall(id="c1", name="echo", args={"x": 1})
    # старый turn оборвался: были только тулы, ответа не пришло
    history.record(UserQueryReceived(request_id=rid_old, query="q-old"))
    history.record(
        TotalMessage(
            request_id=rid_old,
            message=AssistantMessage(tool_calls=(call,)),
            finish_reason=FinishReason.TOOL_CALLS,
        ),
    )
    history.record(
        ToolResultReady(
            request_id=rid_old,
            call=call,
            result=ToolCallResult(result=TextResult(text="ok")),
        ),
    )
    _TurnRecorder.record(history, rid_new, query="q-new", answer="final")

    view = CompactHistoryDialogView(history, max_messages=20)
    assert _summarize(list(view.dialog_message_iter())) == [
        ("user", "q-old"),
        ("user", "q-new"),
        ("assistant", "final"),
    ]


def test_window_trims_oldest_messages():
    """max_messages обрезает старые сообщения, хвост сохраняется."""
    history = InMemoryHistoryService()
    rids = [new_request_id() for _ in range(4)]
    for index, rid in enumerate(rids):
        _TurnRecorder.record(history, rid, query=f"q{index}", answer=f"a{index}")

    # Без окна было бы 8 сообщений (4 пары user+assistant)
    view = CompactHistoryDialogView(history, max_messages=4)
    assert _summarize(list(view.dialog_message_iter())) == [
        ("user", "q2"),
        ("assistant", "a2"),
        ("user", "q3"),
        ("assistant", "a3"),
    ]


def test_window_shifts_to_nearest_user_when_border_breaks_pair():
    """Если граница окна попала между user и assistant — сдвиг до UserMessage."""
    history = InMemoryHistoryService()
    rids = [new_request_id() for _ in range(3)]
    for index, rid in enumerate(rids):
        _TurnRecorder.record(history, rid, query=f"q{index}", answer=f"a{index}")

    # 6 сообщений: u0, a0, u1, a1, u2, a2.
    # max_messages=3 -> срез [a1, u2, a2] -> сдвиг до u2 -> [u2, a2].
    view = CompactHistoryDialogView(history, max_messages=3)
    assert _summarize(list(view.dialog_message_iter())) == [
        ("user", "q2"),
        ("assistant", "a2"),
    ]


def test_window_drops_orphan_tool_result_in_current_request():
    """Если окно начинается с tool_result в текущем request_id — сдвиг до user."""
    history = InMemoryHistoryService()
    rid_old = new_request_id()
    rid_new = new_request_id()
    _TurnRecorder.record(history, rid_old, query="q-old", answer="a-old")

    call = ToolCall(id="c1", name="echo", args={"x": 1})
    _TurnRecorder.record(
        history,
        rid_new,
        query="q-new",
        answer="a-new",
        tool_calls=(call,),
        tool_results=((call, "tool-out"),),
    )

    # Полный список: u-old, a-old, u-new, a-new(+tool_call), tool_result
    # max_messages=2 -> срез [a-new(+tool_call), tool_result] -> нет UserMessage
    # -> пустой результат.
    view = CompactHistoryDialogView(history, max_messages=2)
    assert _summarize(list(view.dialog_message_iter())) == []


def test_empty_history_returns_empty():
    """Пустой журнал — пустой итератор."""
    history = InMemoryHistoryService()
    view = CompactHistoryDialogView(history, max_messages=10)
    assert list(view.dialog_message_iter()) == []
