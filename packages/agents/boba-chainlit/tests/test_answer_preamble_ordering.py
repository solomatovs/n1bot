"""Преамбула (контент turn'а с tool_calls) не должна вставать над tool-step'ами.

Регресс: turn, где модель ОДНОВРЕМЕННО выдаёт текст и вызывает инструмент,
рендерился assistant-баблом раньше своих же tool-step'ов (лента упорядочена
порядком вставки). Чиним: финальным ответом считаем только turn без tool_calls,
контент turn'а с tool_calls показываем свёрнутым preamble-step'ом.
"""

from __future__ import annotations

import asyncio

from boba.agent.events import (
    AnswerMessage,
    ToolCallMessage,
    ToolExecutionStarted,
    ToolResultReady,
    TotalMessage,
)
from boba.agent.models import ToolCallResult
from boba.chainlit.models import ThreadId
from boba.chainlit.rendering.dispatcher import AgentEventDispatcher
from boba.chainlit.rendering.replay import StepDictTarget
from boba.llm.events import FinishReason
from boba.llm.models import AssistantMessage, ToolCall, new_request_id
from boba.tools.domain import TextResult


def _replay(events: list[object]) -> list[dict]:
    target = StepDictTarget(ThreadId("t1"))
    dispatcher = AgentEventDispatcher(target)

    async def run() -> None:
        for e in events:
            await dispatcher.handle(e)  # type: ignore[arg-type]

    asyncio.run(run())
    return target.steps()


def _preamble_turn(rid: object, call: ToolCall, text: str) -> list[object]:
    """Turn с текстом-преамбулой И вызовом инструмента."""
    return [
        AnswerMessage(request_id=rid, content=text),
        ToolCallMessage(request_id=rid, call=call),
        TotalMessage(
            request_id=rid,
            message=AssistantMessage(content=text, tool_calls=(call,)),
            finish_reason=FinishReason.TOOL_CALLS,
        ),
        ToolExecutionStarted(request_id=rid, call=call),
        ToolResultReady(
            request_id=rid,
            call=call,
            result=ToolCallResult(result=TextResult(text="result")),
        ),
    ]


def test_preamble_renders_as_step_above_tool_not_as_answer() -> None:
    rid = new_request_id()
    call = ToolCall(id="c1", type="function", name="grep", args={"q": "x"})
    steps = _replay(_preamble_turn(rid, call, "Сейчас поищу в файлах…"))

    # преамбула — свёрнутый run-step, НЕ assistant_message
    assert not any(s["type"] == "assistant_message" for s in steps)
    preamble = next(s for s in steps if s["name"] == "preamble")
    assert preamble["type"] == "run"
    assert "поищу" in preamble["output"]

    # порядок: преамбула стоит ВЫШЕ своего tool-step'а (она была до вызова)
    names = [s["name"] for s in steps]
    assert names.index("preamble") < names.index("grep")


def test_final_answer_is_assistant_message_after_all_tools() -> None:
    rid1 = new_request_id()
    call = ToolCall(id="c1", type="function", name="grep", args={"q": "x"})
    rid2 = new_request_id()
    final = "Готовый ответ пользователю."
    events = [
        *_preamble_turn(rid1, call, "Сейчас поищу…"),
        AnswerMessage(request_id=rid2, content=final),
        TotalMessage(
            request_id=rid2,
            message=AssistantMessage(content=final),
            finish_reason=FinishReason.STOP,
        ),
    ]
    steps = _replay(events)

    # ровно один assistant_message — финальный ответ
    answers = [s for s in steps if s["type"] == "assistant_message"]
    assert len(answers) == 1
    assert final in answers[0]["output"]

    # он последний в ленте — ниже преамбулы и tool-step'а
    assert steps[-1] is answers[0]
    assert [s["name"] for s in steps] == ["preamble", "grep", "assistant"]


def test_whitespace_preamble_is_dropped() -> None:
    rid = new_request_id()
    call = ToolCall(id="c1", type="function", name="grep", args={"q": "x"})
    steps = _replay(_preamble_turn(rid, call, "  \n  "))
    # пустой/whitespace контент не создаёт лишний preamble-step
    assert not any(s["name"] == "preamble" for s in steps)
    assert [s["name"] for s in steps] == ["grep"]
