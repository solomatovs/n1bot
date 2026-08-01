"""Тесты рендера ToolResult в tracer (AgentTracer._finalize_tool_result)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pytest
from chainlit.step import Step

from boba.chainlit2.chat.agent_tracer import AgentTracer
from boba.chainlit2.rendering.tool_result import (
    ChartResult,
    ErrorResult,
    TextResult,
)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Юнит-тест рендера — контекст chainlit не нужен (object.__new__)."""


class FakeStep:
    """Минимальный двойник chainlit Step: только поля, которые ставит рендер."""

    def __init__(self) -> None:
        self.output: Any = None
        self.language: Any = None
        self.is_error = False
        self.end: Any = None
        self.updated = False

    async def update(self) -> None:
        self.updated = True


def _make_tracer(chart_calls: list) -> AgentTracer:
    """Экземпляр без __init__ (context_var не трогаем), мок-отправка графика."""
    tracer = cast(AgentTracer, object.__new__(AgentTracer))

    async def _send_chart(title: str | None, spec: Mapping[str, Any]) -> None:
        chart_calls.append((title, spec))

    tracer._send_chart_message = _send_chart  # type: ignore[attr-defined]
    return tracer


async def _finalize(tracer: AgentTracer, step: FakeStep, artifact: Any) -> None:
    """Прокинуть двойник step'а в финализацию результата."""
    await tracer._finalize_tool_result(cast(Step, step), artifact)


class TestFinalizeToolResult:
    def test_markdown_text(self) -> None:
        step = FakeStep()
        tracer = _make_tracer([])
        await_result(_finalize(tracer, step, TextResult(text="hi")))
        assert step.output == "hi"
        # language не ставится: chainlit рендерит output как markdown только
        # при language=None (иначе — сырой код-блок)
        assert step.language is None
        assert step.is_error is False
        assert step.updated is True

    def test_error_result_marks_step(self) -> None:
        step = FakeStep()
        tracer = _make_tracer([])
        await_result(
            _finalize(tracer, step, ErrorResult(message="boom", error_kind="e"))
        )
        assert step.is_error is True
        assert "boom" in step.output

    def test_chart_sends_message(self) -> None:
        step = FakeStep()
        chart_calls: list = []
        tracer = _make_tracer(chart_calls)
        result = ChartResult(spec={"data": []}, title="T")
        await_result(_finalize(tracer, step, result))
        assert step.output == "график отрисован: T"
        assert chart_calls == [("T", {"data": []})]

    def test_non_tool_result_falls_through(self) -> None:
        step = FakeStep()
        tracer = _make_tracer([])
        # артефакт — не ToolResult (например, сырой dict от стороннего инструмента)
        await_result(_finalize(tracer, step, {"kind": "x"}))
        assert step.output is not None  # рендер через process_content


def await_result(coro):
    """Запустить корутину без pytest.anyio (тест-модуль не помечен anyio)."""
    import asyncio

    return asyncio.run(coro)
