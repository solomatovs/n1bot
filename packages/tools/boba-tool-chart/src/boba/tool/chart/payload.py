"""Операции над графиками: валидация Plotly-спеки внутри песочницы.

Потока данных у узла нет: заголовок уезжает данными квитанции tool_result.

Ошибки: invalid_figure_spec — спека не разбирается или не проходит схему
plotly; это ответ LLM на её же спеку, поэтому едет текстом без трейсбека.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any, ClassVar

from plotly import graph_objects as go
from pydantic import BaseModel

from boba.tool.chart.protocol import ValidateFigureAnswer, ValidateFigureRequest
from boba.toolkit.payload import PayloadChannels, PayloadEntry, PayloadError


class ChartOps:
    """Операции над figure-спекой; вызываются диспетчером payload'а."""

    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {}
    """Отказы объявляются на месте через PayloadError: сторонних типов нет."""

    REQUESTS: ClassVar[Mapping[str, type[BaseModel]]] = {
        ValidateFigureRequest.OP: ValidateFigureRequest,
    }

    BAD_SPEC: ClassVar[str] = "invalid_figure_spec"

    @classmethod
    async def dispatch(
        cls, request: BaseModel, channels: PayloadChannels
    ) -> BaseModel:
        """Каналов данных у узла нет: проверка спеки и заголовок в квитанцию."""
        if isinstance(request, ValidateFigureRequest):
            return cls.validate_figure(request)

        msg = f"unexpected request model: {type(request).__name__}"
        raise TypeError(msg)

    @classmethod
    def validate_figure(cls, request: ValidateFigureRequest) -> ValidateFigureAnswer:
        """Проверить спеку схемой plotly и достать заголовок."""
        try:
            parsed = json.loads(request.spec)
        except json.JSONDecodeError as e:
            msg = f"spec is not valid JSON: {e}"
            raise PayloadError(cls.BAD_SPEC, msg) from e

        if not isinstance(parsed, dict):
            msg = f"spec must be a JSON figure object, got {type(parsed).__name__}"
            raise PayloadError(cls.BAD_SPEC, msg)

        try:
            go.Figure(parsed)
        except (ValueError, TypeError) as e:
            msg = f"invalid Plotly figure spec: {e}"
            raise PayloadError(cls.BAD_SPEC, msg) from e

        return ValidateFigureAnswer(title=cls.title_of(parsed))

    @staticmethod
    def title_of(parsed: Mapping[str, Any]) -> str:
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


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(ChartOps))
