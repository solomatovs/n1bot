"""Инструмент visualize в настоящей песочнице: plotly проверяет спеку там."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from importlib import reload
from typing import Any

import pytest

from boba.sandbox import SandboxToolConfig
from boba.toolkit.wrap import ToolProcessWrap
from boba.sandbox.zygote import ZygotePolicy, ZygoteRegistry, ZygoteToolCaller
from boba.stand.sandbox import needs_sandbox, needs_userns, sandbox_profile
from boba.toolkit.launcher import PayloadFailureError
from boba.toolkit.result import ChartResult

ZYGOTE = ZygotePolicy(
    start_timeout_sec=60.0,
    max_start_attempts=1,
    restart_backoff_sec=0.05,
    healthy_after_sec=0.5,
    stop_wait_sec=5.0,
    call_poll_sec=0.05,
)


def _tool() -> Any:
    """visualize с обёрткой запуска на профиле тестовой песочницы."""
    import boba.tool.chart.tools as chart_module

    module = reload(chart_module)

    sandbox = SandboxToolConfig.model_validate({"profile": sandbox_profile()})
    profile = sandbox.profile
    supervisor = ZygoteRegistry.obtain(
        "chart-test", profile, [chart_module.__name__], ZYGOTE
    )
    launcher = ZygoteToolCaller("chart-test", supervisor, profile)

    ToolProcessWrap.guard_all(module.TOOLS, launcher)
    return module.visualize


@dataclass(frozen=True)
class Rendered:
    """Ответ фасадного инструмента: текст для модели и артефакт."""

    content: str
    artifact: Any


def _invoke(spec: str) -> Rendered:
    """Вызов тела фасада: langchain в пакете инструмента не участвует."""
    tool = _tool()

    async def go() -> Any:
        body = tool.coroutine
        if body is None:
            raise AssertionError("у visualize нет асинхронного тела")

        return await body(spec=spec)

    content, artifact = asyncio.run(go())
    return Rendered(content=content, artifact=artifact)


@needs_sandbox
@needs_userns
class TestChartInSandbox:
    """Схему графика проверяет plotly внутри песочницы."""

    def teardown_method(self) -> None:
        ZygoteRegistry.stop_all()

    def test_valid_spec_returns_title(self) -> None:
        spec = (
            '{"data": [{"type": "bar", "x": ["a", "b"], "y": [1, 2]}], '
            '"layout": {"title": {"text": "Продажи"}}}'
        )
        message = _invoke(spec)

        if not (isinstance(message.artifact, ChartResult)):
            raise AssertionError("isinstance(message.artifact, ChartResult)")
        if message.artifact.title != "Продажи":
            raise AssertionError('message.artifact.title == "Продажи"')

    def test_title_may_be_a_plain_string(self) -> None:
        message = _invoke('{"data": [], "layout": {"title": "Отчёт"}}')

        if not (isinstance(message.artifact, ChartResult)):
            raise AssertionError("isinstance(message.artifact, ChartResult)")
        if message.artifact.title != "Отчёт":
            raise AssertionError('message.artifact.title == "Отчёт"')

    def test_spec_without_title(self) -> None:
        message = _invoke('{"data": [{"type": "bar", "x": ["a"], "y": [1]}]}')

        if not (isinstance(message.artifact, ChartResult)):
            raise AssertionError("isinstance(message.artifact, ChartResult)")
        if message.artifact.title is not None:
            raise AssertionError("message.artifact.title is None")

    def test_chart_result_carries_spec_and_content(self) -> None:
        spec = (
            '{"data": [{"type": "bar", "x": [1, 2], "y": [3, 1]}], '
            '"layout": {"title": "T"}}'
        )
        message = _invoke(spec)

        if not (isinstance(message.artifact, ChartResult)):
            raise AssertionError("isinstance(message.artifact, ChartResult)")
        if message.artifact.title != "T":
            raise AssertionError('message.artifact.title == "T"')
        if message.artifact.spec["data"][0]["type"] != "bar":
            raise AssertionError('message.artifact.spec["data"][0]["type"] == "bar"')
        if message.content != "[chart rendered: T]":
            raise AssertionError('message.content == "[chart rendered: T]"')

    def test_invalid_spec_reaches_the_caller(self) -> None:
        with pytest.raises(PayloadFailureError, match="invalid Plotly") as failure:
            _invoke('{"data": 42}')

        if failure.value.kind != "invalid_figure_spec":
            raise AssertionError('failure.value.kind == "invalid_figure_spec"')
        if "Traceback" in str(failure.value):
            raise AssertionError('"Traceback" not in str(failure.value)')

    def test_broken_json_is_an_expected_failure(self) -> None:
        with pytest.raises(PayloadFailureError) as failure:
            _invoke("{not json")

        if failure.value.kind != "invalid_figure_spec":
            raise AssertionError('failure.value.kind == "invalid_figure_spec"')
