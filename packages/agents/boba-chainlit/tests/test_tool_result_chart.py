"""ChartResult маршрутизируется в chart-канал, прочие — в markdown."""

from __future__ import annotations

import asyncio

import pytest

from boba.agent.events import ToolResultReady
from boba.agent.models import ToolCallResult
from boba.chainlit.models import ThreadId
from boba.chainlit.rendering.dispatcher import AgentEventDispatcher
from boba.chainlit.rendering.replay import StepDictTarget
from boba.chainlit.rendering.tool_result_view import (
    ChartRendering,
    MarkdownRendering,
    ToolResultView,
)
from boba.llm.models import ToolCall, new_request_id
from boba.tools.domain import ChartResult, TableResult, TextResult

_CHART = ChartResult(
    spec={"data": [{"type": "bar", "x": ["a"], "y": [1]}], "layout": {}},
    title="Продажи",
)


def test_view_routes_chart_to_chart_rendering() -> None:
    assert isinstance(ToolResultView(_CHART).render(), ChartRendering)


@pytest.mark.parametrize(
    "result",
    [TextResult(text="hi"), TableResult(rows=[{"a": 1}])],
)
def test_view_routes_others_to_markdown(result: object) -> None:
    assert isinstance(ToolResultView(result).render(), MarkdownRendering)  # type: ignore[arg-type]


def test_dispatcher_chart_result_reaches_chart_channel() -> None:
    call = ToolCall(id="c1", name="visualize", args={"spec": "{}"})
    target = StepDictTarget(ThreadId("thread-1"))
    dispatcher = AgentEventDispatcher(target)

    async def run() -> None:
        await dispatcher.handle(
            ToolResultReady(
                request_id=new_request_id(),
                call=call,
                result=ToolCallResult(result=_CHART),
            ),
        )

    asyncio.run(run())

    # tool-step с текстом + видимый контейнер-сообщение под график
    steps = target.steps()
    assert any(s.get("type") == "tool" for s in steps)
    container = next(s for s in steps if s.get("type") == "assistant_message")

    # график восстановлен из истории как plotly-элемент с data:-URI
    elements = target.elements()
    assert len(elements) == 1
    el = elements[0]
    assert el.get("type") == "plotly"
    assert el.get("forId") == container.get("id")
    assert str(el.get("url")).startswith("data:application/json;base64,")


def test_replay_skips_chart_on_broken_spec() -> None:
    call = ToolCall(id="c1", name="visualize", args={"spec": "{}"})
    broken = ChartResult(spec={"data": [{"type": "no_such_trace"}]}, title="X")
    target = StepDictTarget(ThreadId("thread-1"))
    dispatcher = AgentEventDispatcher(target)

    async def run() -> None:
        await dispatcher.handle(
            ToolResultReady(
                request_id=new_request_id(),
                call=call,
                result=ToolCallResult(result=broken),
            ),
        )

    asyncio.run(run())
    # битый spec не роняет replay и не даёт элемента
    assert target.elements() == []
