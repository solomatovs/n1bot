"""Tool visualize: интерактивный Plotly-график из figure-spec.

Порт boba.tool.chart.visualize. Возвращает ChartResult — UI рендерит
интерактивный график, LLM получает только подтверждение отрисовки.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from langchain.tools import tool
from pydantic import Field

from boba.chainlit2.rendering.render import pack_result
from boba.chainlit2.rendering.tool_result import ChartResult, ToolResult

__all__ = ["visualize"]


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
    from plotly import graph_objects as go  # noqa: PLC0415

    try:
        parsed: Any = json.loads(spec)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"spec не является валидным JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"spec должен быть JSON-объектом figure, получен {type(parsed).__name__}",
        )

    # Построением go.Figure валидируем структуру Plotly до отправки в UI —
    # битый spec вернётся модели как ошибка, а не молча покажет заглушку.
    try:
        go.Figure(parsed)
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"невалидный Plotly figure-spec: {e}") from e

    # Заголовок берём из самого spec (layout.title — строка или {"text": ...}),
    # не из go.Figure: так не зависим от слабо типизированного plotly-API.
    layout = parsed.get("layout") or {}
    raw_title = layout.get("title") if isinstance(layout, dict) else None
    if isinstance(raw_title, dict):
        raw_title = raw_title.get("text")
    result = ChartResult(
        spec=parsed,
        title=str(raw_title) if raw_title else None,
    )
    return pack_result(result)
