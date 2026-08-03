"""Инструмент visualize в настоящей песочнице: plotly проверяет спеку там.

Схема графика разбирается payload'ом внутри bwrap; приложение plotly не
держит и получает обратно только заголовок.
"""

from __future__ import annotations

import pytest
from conftest import needs_sandbox, needs_userns, sandbox_profile

from boba.tool.chart import ChartToolsConfig, build_chart_tools
from boba.tool.chart.caller import ChartCaller
from boba.tool.chart.protocol import ValidateFigureAnswer, ValidateFigureRequest
from boba.toolkit.result import ChartResult
from boba.toolkit.sandbox import SandboxCaller, SandboxPayloadError, SandboxToolConfig


def _caller() -> SandboxCaller:
    sandbox = SandboxToolConfig.model_validate(
        {"profile": sandbox_profile(), "override": {}}
    )
    return SandboxCaller("chart-test", sandbox.effective(), dict)


@needs_sandbox
@needs_userns
class TestChartInSandbox:
    """Схему графика проверяет plotly внутри песочницы."""

    _SPEC = (
        '{"data": [{"type": "bar", "x": ["a", "b"], "y": [1, 2]}], '
        '"layout": {"title": {"text": "Продажи"}}}'
    )

    def test_valid_spec_returns_title(self) -> None:
        request = ValidateFigureRequest.of(self._SPEC)
        answer = _caller().call_json(ChartCaller.ENTRY, request, ValidateFigureAnswer)
        assert answer.title == "Продажи"

    def test_title_may_be_a_plain_string(self) -> None:
        spec = '{"data": [], "layout": {"title": "Отчёт"}}'
        answer = _caller().call_json(
            ChartCaller.ENTRY, ValidateFigureRequest.of(spec), ValidateFigureAnswer
        )
        assert answer.title == "Отчёт"

    def test_spec_without_title(self) -> None:
        spec = '{"data": [{"type": "bar", "x": ["a"], "y": [1]}]}'
        answer = _caller().call_json(
            ChartCaller.ENTRY, ValidateFigureRequest.of(spec), ValidateFigureAnswer
        )
        assert answer.title == ""

    def test_broken_json_is_reported(self) -> None:
        with pytest.raises(SandboxPayloadError, match="not valid JSON"):
            _caller().call_json(
                ChartCaller.ENTRY,
                ValidateFigureRequest.of("{не json"),
                ValidateFigureAnswer,
            )

    def test_unknown_trace_type_is_reported(self) -> None:
        """Схему держит plotly: выдуманный тип графика должен быть отклонён."""
        spec = '{"data": [{"type": "нет-такого-типа", "x": [1], "y": [2]}]}'
        with pytest.raises(SandboxPayloadError, match="invalid Plotly figure spec"):
            _caller().call_json(
                ChartCaller.ENTRY, ValidateFigureRequest.of(spec), ValidateFigureAnswer
            )

    def test_non_object_spec_is_reported(self) -> None:
        with pytest.raises(SandboxPayloadError, match="must be a JSON figure object"):
            _caller().call_json(
                ChartCaller.ENTRY,
                ValidateFigureRequest.of("[1, 2, 3]"),
                ValidateFigureAnswer,
            )


@needs_sandbox
@needs_userns
class TestChartTool:
    """Инструмент visualize целиком: LLM -> песочница -> ChartResult."""

    @staticmethod
    def _tool():
        cfg = ChartToolsConfig.model_validate(
            {
                "sandbox": {
                    "profile": sandbox_profile(),
                    "override": {},
                }
            }
        )
        return build_chart_tools(cfg, dict)[0]

    def test_chart_result_carries_spec_and_title(self) -> None:
        spec = (
            '{"data": [{"type": "bar", "x": [1, 2], "y": [3, 1]}], '
            '"layout": {"title": "T"}}'
        )
        message = self._tool().invoke(
            {
                "args": {"spec": spec},
                "id": "call-chart",
                "name": "visualize",
                "type": "tool_call",
            }
        )
        assert isinstance(message.artifact, ChartResult)
        assert message.artifact.title == "T"
        assert message.artifact.spec["data"][0]["type"] == "bar"
        assert message.content == "[chart rendered: T]"

    def test_invalid_spec_reaches_the_caller(self) -> None:
        with pytest.raises(SandboxPayloadError, match="invalid Plotly"):
            self._tool().invoke(
                {
                    "args": {"spec": '{"data": 42}'},
                    "id": "call-chart",
                    "name": "visualize",
                    "type": "tool_call",
                }
            )


