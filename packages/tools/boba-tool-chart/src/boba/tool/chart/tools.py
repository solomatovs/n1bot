"""Tool visualize: интерактивный Plotly-график из figure-spec."""

from __future__ import annotations

import json
from typing import Annotated, Any

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.tool.chart.caller import ChartCaller
from boba.toolkit.launcher import LauncherFactory
from boba.toolkit.result import ChartResult, ToolResult, pack_result

__all__ = ["build_chart_tools"]


def build_chart_tools(launchers: LauncherFactory) -> list[BaseTool]:
    caller = ChartCaller("chart", launchers)

    @tool(response_format="content_and_artifact")
    def visualize(
        spec: Annotated[
            str,
            Field(
                min_length=1,
                description=(
                    "Plotly figure как JSON-объект: "
                    '{"data": [...], "layout": {...}}.'
                    "Бери данные из уже полученных результатов (SQL/файлы);"
                    "крупные выборки агрегируй заранее."
                    "Тип графика, оси, подписи и легенду "
                    "выбираешь сам через trace-объекты и layout. Заголовок "
                    "указывай в layout.title — он попадёт в подпись и в сводку."
                ),
            ),
        ],
    ) -> tuple[str, ToolResult]:
        """Отрисовать интерактивный график по Plotly figure-спецификации."""
        title = caller.validate(spec)
        parsed: Any = json.loads(spec)
        return pack_result(ChartResult(spec=parsed, title=title or None))

    return [visualize]
