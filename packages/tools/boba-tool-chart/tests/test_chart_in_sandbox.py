"""Инструмент visualize в настоящей песочнице: plotly проверяет спеку там."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import reload
from typing import Any

import pytest

from boba.sandbox import SandboxToolConfig
from boba.sandbox.zygote import ZygotePolicy, ZygoteToolCaller
from boba.stand.sandbox import needs_sandbox, needs_userns, sandbox_profile
from boba.stand.zygote import ZygoteStand
from boba.toolkit.chain import CallAmbient
from boba.toolkit.launcher import PayloadFailureError
from boba.toolkit.result import VisualResult
from boba.toolkit.wrap import ToolProcessWrap

ZYGOTE = ZygotePolicy(
    start_timeout_sec=60.0,
    max_start_attempts=1,
    restart_backoff_sec=0.05,
    healthy_after_sec=0.5,
    stop_wait_sec=5.0,
    call_poll_sec=0.05,
)


def _tool(zygote_stand: ZygoteStand) -> Any:
    """visualize с обёрткой запуска на профиле тестовой песочницы."""
    import boba.tool.chart.tools as chart_module

    module = reload(chart_module)

    sandbox = SandboxToolConfig.model_validate(
        {"profile": sandbox_profile("boba-tool-chart")}
    )
    profile = sandbox.profile
    supervisor = zygote_stand.registry().obtain(
        "chart-test", profile, [chart_module.__name__], ZYGOTE
    )
    launcher = ZygoteToolCaller("chart-test", supervisor, profile, CallAmbient())

    ToolProcessWrap(CallAmbient()).guard_all(module.TOOLS, launcher)
    return module.visualize


@dataclass(frozen=True)
class Rendered:
    """Ответ фасадного инструмента: текст для модели и артефакт."""

    content: str
    artifact: Any


def _invoke(zygote_stand: ZygoteStand, spec: str) -> Rendered:
    """Вызов тела фасада: langchain в пакете инструмента не участвует."""
    tool = _tool(zygote_stand)

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

    @pytest.fixture(autouse=True)
    def zygotes_stopped(self, zygote_stand: ZygoteStand) -> Iterator[None]:
        """Зиготы секций гасятся после каждого теста класса."""
        yield
        zygote_stand.stop()

    def test_valid_spec_returns_title(self, zygote_stand: ZygoteStand) -> None:
        spec = (
            '{"data": [{"type": "bar", "x": ["a", "b"], "y": [1, 2]}], '
            '"layout": {"title": {"text": "Продажи"}}}'
        )
        message = _invoke(zygote_stand, spec)

        if not (isinstance(message.artifact, VisualResult)):
            raise AssertionError("isinstance(message.artifact, VisualResult)")
        if message.artifact.title != "Продажи":
            raise AssertionError('message.artifact.title == "Продажи"')

    def test_title_may_be_a_plain_string(self, zygote_stand: ZygoteStand) -> None:
        message = _invoke(zygote_stand, '{"data": [], "layout": {"title": "Отчёт"}}')

        if not (isinstance(message.artifact, VisualResult)):
            raise AssertionError("isinstance(message.artifact, VisualResult)")
        if message.artifact.title != "Отчёт":
            raise AssertionError('message.artifact.title == "Отчёт"')

    def test_spec_without_title(self, zygote_stand: ZygoteStand) -> None:
        message = _invoke(
            zygote_stand, '{"data": [{"type": "bar", "x": ["a"], "y": [1]}]}'
        )

        if not (isinstance(message.artifact, VisualResult)):
            raise AssertionError("isinstance(message.artifact, VisualResult)")
        if message.artifact.title is not None:
            raise AssertionError("message.artifact.title is None")

    def test_chart_result_carries_spec_and_content(
        self, zygote_stand: ZygoteStand
    ) -> None:
        spec = (
            '{"data": [{"type": "bar", "x": [1, 2], "y": [3, 1]}], '
            '"layout": {"title": "T"}}'
        )
        message = _invoke(zygote_stand, spec)

        if not (isinstance(message.artifact, VisualResult)):
            raise AssertionError("isinstance(message.artifact, VisualResult)")
        if message.artifact.title != "T":
            raise AssertionError('message.artifact.title == "T"')
        spec_back = message.artifact.props[VisualResult.PLOTLY_SPEC]
        if spec_back["data"][0]["type"] != "bar":
            raise AssertionError('spec_back["data"][0]["type"] == "bar"')
        if message.content != "[plotly rendered: T]":
            raise AssertionError('message.content == "[plotly rendered: T]"')

    def test_invalid_spec_reaches_the_caller(self, zygote_stand: ZygoteStand) -> None:
        with pytest.raises(PayloadFailureError, match="rejected by plotly") as failure:
            _invoke(zygote_stand, '{"data": 42}')

        if failure.value.failure().error_kind != "InvalidFigureSpecError":
            raise AssertionError(f"failure: {failure.value.failure()!r}")
        if "Traceback" in str(failure.value):
            raise AssertionError('"Traceback" not in str(failure.value)')

    def test_broken_json_is_an_expected_failure(
        self, zygote_stand: ZygoteStand
    ) -> None:
        with pytest.raises(PayloadFailureError) as failure:
            _invoke(zygote_stand, "{not json")

        if failure.value.failure().error_kind != "InvalidFigureSpecError":
            raise AssertionError(f"failure: {failure.value.failure()!r}")
