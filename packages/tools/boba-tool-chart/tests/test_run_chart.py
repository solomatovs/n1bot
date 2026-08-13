"""Ручной прогон операции chart: ChartOps проверяет спеку через plotly."""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from boba.tool.chart.caller import ValidateFigureRequest
from boba.tool.chart.payload import ChartOps

pytestmark = [pytest.mark.run]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    FIGURE: ClassVar[dict[str, Any]] = {
        "data": [{"type": "bar", "x": ["a", "b", "c"], "y": [3, 1, 2]}],
        "layout": {"title": "run"},
    }

    @classmethod
    def spec(cls) -> str:
        return json.dumps(cls.FIGURE, ensure_ascii=False)


def test_run_validate_figure(payload) -> None:
    request = ValidateFigureRequest.of(RunArgs.spec())

    trailer = ChartOps.validate_figure(payload.of(request))

    print(trailer)
