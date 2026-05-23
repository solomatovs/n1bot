"""DishkaTool.stream — единственный публичный API, два внутренних режима.

Покрывает дуальную семантику адаптера: plan.is_generator определяет, как
именно DishkaTool превратит target в поток `ToolEvent`'ов. Снаружи —
один и тот же контракт `Iterator[ToolEvent]`, заканчивающийся ровно одним
`ToolStreamCompleted` (этот терминальный yield — и есть «tool result»).

Режимы:

- **plain function** (`@tool def f(...) -> T: return ...`): `stream()`
  возвращает один-единственный `ToolStreamCompleted` с обёрнутым результатом.

- **generator function**
  (`@tool def f(...) -> Generator[ToolEvent, None, T]: ...`):
  `stream()` пробрасывает все yield'ы как `ToolProgressReported` и завершает
  поток `ToolStreamCompleted` со значением, переданным через `return`.

Error-кейсы:
- generator yield-нувший НЕ-ToolEvent → ToolExecutionError из stream().
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from dishka import make_container

from boba.tools import tool
from boba.tools.adapter import DishkaTool
from boba.tools.domain import (
    JsonResult,
    TextResult,
    ToolCall,
    ToolContext,
    ToolEvent,
    ToolExecutionError,
    ToolProgressReported,
    ToolResult,
    ToolSeverity,
    ToolSourceId,
    ToolStreamCompleted,
)
from boba.tools.framework import StaticToolSource, ToolRegistry
from boba.tools.introspect import introspect_callable


@tool
class ExecuteEcho:
    """Plain-function tool: returns TextResult immediately."""

    def __call__(self, text: str) -> ToolResult:
        return TextResult(text=text)


@tool
def streaming_counter(count: int) -> Generator[ToolEvent, None, dict]:
    """Generator tool: yields N progress events, returns summary dict."""
    for i in range(count):
        yield ToolProgressReported(
            headline=f"step {i + 1}/{count}",
            details={"i": str(i)},
            severity=ToolSeverity.INFO,
        )
    return {"total": count}


@tool
def streaming_bad_yield(dummy: int) -> Generator[object, None, dict]:
    """Generator tool с нарушением контракта: yield'ит мусор."""
    yield "not a ToolEvent"  # type: ignore[misc]
    return {"total": 1}


def _make_dishka_tool(target: object, source: ToolSourceId) -> DishkaTool:
    plan = introspect_callable(target)
    container = make_container()
    return DishkaTool(
        target=target() if isinstance(target, type) else target,
        plan=plan,
        container=container,
        component="",
        source_id=source,
    )


def _executor_with(*targets: object) -> tuple[ToolRegistry, list[str]]:
    src = ToolSourceId("src")
    tools = []
    names: list[str] = []
    for t in targets:
        dt = _make_dishka_tool(t, src)
        tools.append(dt)
        names.append(str(dt.tool_id()))
    registry = ToolRegistry.from_sources([StaticToolSource(src, tools)])
    return registry, names


# --------------------------------------------------------------------------- #
# Plain-function tool
# --------------------------------------------------------------------------- #


def test_plain_function_stream_yields_single_completed():
    """`@tool def f(...) -> X`: stream() → один ToolStreamCompleted."""
    registry, [tool_id] = _executor_with(ExecuteEcho)
    executor = registry.executor()

    events = list(
        executor.stream(
            ToolContext(),
            ToolCall(tool_id=tool_id, arguments={"text": "hi"}),  # type: ignore[arg-type]
        ),
    )

    assert len(events) == 1
    assert isinstance(events[0], ToolStreamCompleted)
    assert isinstance(events[0].result, TextResult)
    assert events[0].result.text == "hi"


# --------------------------------------------------------------------------- #
# Generator tool
# --------------------------------------------------------------------------- #


def test_generator_tool_yields_progress_then_completed():
    """Generator-tool: N ToolProgressReported + 1 ToolStreamCompleted в конце."""
    registry, [tool_id] = _executor_with(streaming_counter)
    executor = registry.executor()

    events = list(
        executor.stream(
            ToolContext(),
            ToolCall(tool_id=tool_id, arguments={"count": 3}),  # type: ignore[arg-type]
        ),
    )

    progress = [e for e in events if isinstance(e, ToolProgressReported)]
    completed = [e for e in events if isinstance(e, ToolStreamCompleted)]
    assert len(progress) == 3
    assert progress[0].headline == "step 1/3"
    assert progress[-1].headline == "step 3/3"
    assert len(completed) == 1
    assert isinstance(completed[0].result, JsonResult)
    assert completed[0].result.payload == {"total": 3}
    # Терминальное событие — всегда последнее в потоке.
    assert events[-1] is completed[0]


def test_generator_tool_return_value_coerced_to_tool_result():
    """`return final_value` (StopIteration.value) coerce-ится в ToolResult.

    Здесь target возвращает dict → DishkaTool оборачивает его в JsonResult
    внутри `ToolStreamCompleted`.
    """
    registry, [tool_id] = _executor_with(streaming_counter)
    executor = registry.executor()

    completed = next(
        e
        for e in executor.stream(
            ToolContext(),
            ToolCall(tool_id=tool_id, arguments={"count": 1}),  # type: ignore[arg-type]
        )
        if isinstance(e, ToolStreamCompleted)
    )
    assert isinstance(completed.result, JsonResult)
    assert completed.result.payload == {"total": 1}


# --------------------------------------------------------------------------- #
# Error-кейсы
# --------------------------------------------------------------------------- #


def test_generator_bad_yield_raises_tool_execution_error():
    """Generator, yield-нувший НЕ-ToolEvent — wire-контракт нарушен."""
    registry, [tool_id] = _executor_with(streaming_bad_yield)
    executor = registry.executor()

    with pytest.raises(ToolExecutionError, match=r"streaming tool .* yielded str"):
        list(
            executor.stream(
                ToolContext(),
                ToolCall(tool_id=tool_id, arguments={"dummy": 1}),  # type: ignore[arg-type]
            ),
        )


# --------------------------------------------------------------------------- #
# is_generator: factbook
# --------------------------------------------------------------------------- #


def test_plan_is_generator_flag_is_set_at_introspect():
    """`introspect_callable` ставит `is_generator` один раз на этапе сборки."""
    assert introspect_callable(ExecuteEcho).is_generator is False
    assert introspect_callable(streaming_counter).is_generator is True
