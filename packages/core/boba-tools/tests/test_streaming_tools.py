"""DishkaTool.stream — контракт `yield = progress, return = result`.

Покрывает три стиля tool-авторства, которые поддерживает инфраструктура:

1. **Plain function** (`def f -> X: return X`) — старый стиль. Framework
   зовёт функцию, оборачивает результат в `ToolStreamCompleted`. Прогресса нет.

2. **Generator (raw yields)** — tool yield-ит любые значения, framework
   оборачивает каждый в `ToolProgressReported`. Результат — через
   `return X` (StopIteration.value → `ToolStreamCompleted`).
   Tool-author **ничего не оборачивает руками**.

3. **Generator (explicit TPR)** — tool yield-ит `ToolProgressReported`
   явно (для richer details/severity). Framework пропускает их без
   изменений. Результат всё равно через `return`.

Error-кейс:
- Tool yield-нул `ToolStreamCompleted` — контракт нарушен (TSC должен
  приходить через `return`) → `ToolExecutionError`.
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

# --------------------------------------------------------------------------- #
# Tool fixtures: три стиля авторства
# --------------------------------------------------------------------------- #


@tool
class ExecuteEcho:
    """Plain-function tool: returns TextResult напрямую (без yield-ов)."""

    def __call__(self, text: str) -> ToolResult:
        return TextResult(text=text)


@tool
def idiomatic_counter(count: int) -> Generator[str, None, dict]:
    """Идиоматический generator: yield-ит raw values для прогресса,
    `return` для результата. Framework сам обернёт каждое.
    """
    for i in range(count):
        yield f"step {i + 1}/{count}"
    return {"total": count}


@tool
def explicit_event_counter(
    count: int,
) -> Generator[ToolProgressReported, None, dict]:
    """Generator с явными `ToolProgressReported` для richer details.

    Framework пропускает их без изменений, результат всё равно через `return`.
    """
    for i in range(count):
        yield ToolProgressReported(
            headline=f"step {i + 1}/{count}",
            details={"i": str(i)},
            severity=ToolSeverity.INFO,
        )
    return {"total": count}


# --------------------------------------------------------------------------- #
# Test infra
# --------------------------------------------------------------------------- #


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
# Plain function (no yields, just return)
# --------------------------------------------------------------------------- #


def test_plain_function_stream_yields_single_completed():
    """`@tool def f(...) -> X`: stream() → один ToolStreamCompleted без прогрессов."""
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
# Generator: raw yields = progress, return = result
# --------------------------------------------------------------------------- #


def test_generator_raw_yields_wrapped_as_progress():
    """Каждый yield → ToolProgressReported, `return X` → ToolStreamCompleted."""
    registry, [tool_id] = _executor_with(idiomatic_counter)
    executor = registry.executor()

    events = list(
        executor.stream(
            ToolContext(),
            ToolCall(tool_id=tool_id, arguments={"count": 3}),  # type: ignore[arg-type]
        ),
    )

    # 3 yields → 3 TPR + 1 TSC из return.
    progress = [e for e in events if isinstance(e, ToolProgressReported)]
    completed = [e for e in events if isinstance(e, ToolStreamCompleted)]
    assert len(progress) == 3
    assert progress[0].headline == "step 1/3"
    assert progress[-1].headline == "step 3/3"
    assert len(completed) == 1
    assert isinstance(completed[0].result, JsonResult)
    assert completed[0].result.payload == {"total": 3}
    # TSC — всегда последний в потоке.
    assert events[-1] is completed[0]


def test_generator_no_yields_only_return():
    """Generator без yield-ов (только return) → один TSC, прогресса нет."""

    @tool
    def no_progress() -> Generator[ToolEvent, None, str]:
        if False:
            yield ToolProgressReported(headline="never")
        return "done"

    registry, [tool_id] = _executor_with(no_progress)
    executor = registry.executor()
    events = list(
        executor.stream(
            ToolContext(),
            ToolCall(tool_id=tool_id, arguments={}),  # type: ignore[arg-type]
        ),
    )

    assert len(events) == 1
    assert isinstance(events[0], ToolStreamCompleted)
    assert isinstance(events[0].result, TextResult)
    assert events[0].result.text == "done"


def test_generator_with_no_return_yields_null_result():
    """Generator без `return` → result = None (TextResult("null"))."""

    @tool
    def progress_only(count: int):
        for i in range(count):
            yield f"step {i + 1}"

    registry, [tool_id] = _executor_with(progress_only)
    executor = registry.executor()
    events = list(
        executor.stream(
            ToolContext(),
            ToolCall(tool_id=tool_id, arguments={"count": 2}),  # type: ignore[arg-type]
        ),
    )

    progress = [e for e in events if isinstance(e, ToolProgressReported)]
    completed = [e for e in events if isinstance(e, ToolStreamCompleted)]
    assert len(progress) == 2
    assert len(completed) == 1
    # Нет `return` — result = None → coerce в TextResult("null").
    assert isinstance(completed[0].result, TextResult)
    assert completed[0].result.text == "null"


# --------------------------------------------------------------------------- #
# Explicit ToolProgressReported (passthrough)
# --------------------------------------------------------------------------- #


def test_explicit_progress_events_passthrough():
    """Tool yield-ит ToolProgressReported явно — framework не пересоздаёт."""
    registry, [tool_id] = _executor_with(explicit_event_counter)
    executor = registry.executor()

    events = list(
        executor.stream(
            ToolContext(),
            ToolCall(tool_id=tool_id, arguments={"count": 2}),  # type: ignore[arg-type]
        ),
    )

    progress = [e for e in events if isinstance(e, ToolProgressReported)]
    completed = [e for e in events if isinstance(e, ToolStreamCompleted)]
    assert len(progress) == 2
    # Структурные поля (details) сохранились.
    assert progress[0].details == {"i": "0"}
    assert progress[0].severity == ToolSeverity.INFO
    assert len(completed) == 1
    assert completed[0].result.payload == {"total": 2}  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Error-кейсы
# --------------------------------------------------------------------------- #


def test_yielded_tsc_is_contract_violation():
    """Tool yield-нул ToolStreamCompleted — контракт нарушен (TSC через return)."""

    @tool
    def yields_tsc() -> Generator[ToolEvent, None, None]:
        yield ToolStreamCompleted(result=TextResult(text="wrong"))

    registry, [tool_id] = _executor_with(yields_tsc)
    executor = registry.executor()

    with pytest.raises(
        ToolExecutionError,
        match=r"yielded ToolStreamCompleted",
    ):
        list(
            executor.stream(
                ToolContext(),
                ToolCall(tool_id=tool_id, arguments={}),  # type: ignore[arg-type]
            ),
        )


# --------------------------------------------------------------------------- #
# is_generator: factbook
# --------------------------------------------------------------------------- #


def test_plan_is_generator_flag_is_set_at_introspect():
    """`introspect_callable` ставит `is_generator` один раз на этапе сборки."""
    assert introspect_callable(ExecuteEcho).is_generator is False
    assert introspect_callable(idiomatic_counter).is_generator is True
    assert introspect_callable(explicit_event_counter).is_generator is True
