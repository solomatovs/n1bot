"""Узел visualize в настоящей песочнице: plotly проверяет спеку там."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import needs_sandbox, needs_userns, sandbox_profile

from boba.sandbox import SandboxCaller, SandboxToolConfig
from boba.sandbox.workflow import StageDef, StageRegistry
from boba.stand.journal import CallStand
from boba.tool.chart import ChartCaller, build_chart_tools
from boba.toolkit.launcher import PayloadFailureError, ToolLauncher
from boba.toolkit.result import ChartResult


class AllowAll:
    """Предикат прав тестов: узлы реестра разрешены целиком."""

    def __call__(self, tool: str, /) -> bool:
        return True


class Launchers:
    """Фабрика порта: песочница на профиле тестов, реестр — узлы пакета."""

    def __init__(self) -> None:
        sandbox = SandboxToolConfig.model_validate(
            {"profile": sandbox_profile(), "override": {}}
        )
        profile = sandbox.effective()

        defs: dict[str, StageDef] = {}
        for name, node in ChartCaller.stages().items():
            defs[name] = StageDef(
                contract=node.contract,
                profile=profile,
                entry=node.entry,
                request=node.request,
                enrich=node.enrich,
            )

        self._caller = SandboxCaller(
            StageRegistry(defs), AllowAll(), dict, CallStand.journal()
        )

    def __call__(self, tool: str, /) -> ToolLauncher:
        return self._caller


def _caller() -> ChartCaller:
    return ChartCaller("chart", Launchers())


@needs_sandbox
@needs_userns
class TestChartInSandbox:
    """Схему графика проверяет plotly внутри песочницы."""

    _SPEC = (
        '{"data": [{"type": "bar", "x": ["a", "b"], "y": [1, 2]}], '
        '"layout": {"title": {"text": "Продажи"}}}'
    )

    def test_valid_spec_returns_title(self) -> None:
        assert _caller().validate(self._SPEC) == "Продажи"

    def test_title_may_be_a_plain_string(self) -> None:
        spec = '{"data": [], "layout": {"title": "Отчёт"}}'

        assert _caller().validate(spec) == "Отчёт"

    def test_spec_without_title(self) -> None:
        spec = '{"data": [{"type": "bar", "x": ["a"], "y": [1]}]}'

        assert _caller().validate(spec) == ""

    def test_broken_json_is_reported(self) -> None:
        with pytest.raises(PayloadFailureError, match="not valid JSON"):
            _caller().validate("{не json")

    def test_unknown_trace_type_is_reported(self) -> None:
        """Схему держит plotly: выдуманный тип графика должен быть отклонён."""
        spec = '{"data": [{"type": "нет-такого-типа", "x": [1], "y": [2]}]}'

        with pytest.raises(PayloadFailureError, match="invalid Plotly figure spec"):
            _caller().validate(spec)

    def test_non_object_spec_is_reported(self) -> None:
        with pytest.raises(PayloadFailureError, match="must be a JSON figure object"):
            _caller().validate("[1, 2, 3]")


class TestChartStages:
    """Узел без продукта: канал данных он не наполняет, итог едет квитанцией."""

    def test_node_declares_no_product(self) -> None:
        node = ChartCaller.stages()["visualize"]

        assert node.contract.out is None

    def test_enricher_adds_the_op(self) -> None:
        node = ChartCaller.stages()["visualize"]

        request = node.enrich({"spec": "{}"})

        assert request["op"] == "validate_figure"


@needs_sandbox
@needs_userns
class TestChartTool:
    """Инструмент visualize целиком: LLM -> песочница -> ChartResult."""

    @staticmethod
    def _tool() -> Any:
        return build_chart_tools(Launchers())[0]

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
        with pytest.raises(PayloadFailureError, match="invalid Plotly") as failure:
            self._tool().invoke(
                {
                    "args": {"spec": '{"data": 42}'},
                    "id": "call-chart",
                    "name": "visualize",
                    "type": "tool_call",
                }
            )
        assert failure.value.kind == "invalid_figure_spec"
        assert "Traceback" not in str(failure.value)
