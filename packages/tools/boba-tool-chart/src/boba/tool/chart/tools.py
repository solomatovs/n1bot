"""Инструмент visualize: функция уровня модуля, модуль — обычная программа.

Запуск: `python -m boba.tool.chart.tools visualize --spec ...`. Спеку пишет
LLM, схему проверяет plotly — потому тело исполняется в песочнице.

Ошибки:
InvalidFigureSpecError — спека не разбирается или не проходит схему plotly;
    это ответ LLM на её же спеку, текст едет без трейсбека.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import Field

from boba.toolkit.calls import ScriptCall
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import tool
from boba.toolkit.result import ChartResult, ToolResult, pack_result


class InvalidFigureSpecError(Exception):
    """Спека не прошла разбор или схему plotly; текст готов для пользователя."""


class ChartErrorKind(StrEnum):
    """Ожидаемые отказы chart-инструмента."""

    INVALID_FIGURE_SPEC = "invalid_figure_spec"


class FigureSpec:
    """Разбор и проверка figure-спеки схемой plotly."""

    @classmethod
    def parsed(cls, spec: str) -> dict[str, Any]:
        try:
            parsed = json.loads(spec)
        except json.JSONDecodeError as exc:
            head = spec[:200]
            msg = f"chart spec expects a JSON figure object, got {head!r}: {exc}"
            raise InvalidFigureSpecError(msg) from exc

        if not isinstance(parsed, dict):
            got = type(parsed).__name__
            msg = f"chart spec expects a JSON figure object, got {got}: {spec[:200]!r}"
            raise InvalidFigureSpecError(msg)

        # plotly тяжёлый — потому тело и живёт в песочнице
        from plotly import graph_objects as go  # noqa: PLC0415

        try:
            go.Figure(parsed)
        except (ValueError, TypeError) as exc:
            keys = sorted(parsed)
            msg = f"chart spec with keys {keys} is rejected by plotly Figure: {exc}"
            raise InvalidFigureSpecError(msg) from exc

        return parsed

    @staticmethod
    def title_of(parsed: dict[str, Any]) -> str:
        """layout.title бывает и строкой, и объектом с полем text."""
        layout = parsed.get("layout")
        if not isinstance(layout, dict):
            return ""
        raw = layout.get("title")
        if isinstance(raw, dict):
            raw = raw.get("text")
        if not raw:
            return ""
        return str(raw)


@tool
async def visualize(
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
    parsed = FigureSpec.parsed(spec)
    title = FigureSpec.title_of(parsed)

    artifact = ChartResult(spec=parsed, title=title or None)
    return pack_result(artifact)


EXPECTED: Mapping[type[Exception], ChartErrorKind] = {
    InvalidFigureSpecError: ChartErrorKind.INVALID_FIGURE_SPEC,
}

TOOLS: Final = ToolMain.toolset(
    visualize,
    views={"visualize": ScriptCall(arg="spec", lang="json")},
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
