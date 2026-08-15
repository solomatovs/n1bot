"""Ручной прогон visualize: функция вызывается напрямую, спека — в RunArgs."""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from boba.tool.chart.tools import visualize
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    FIGURE: ClassVar[dict[str, Any]] = {
        "data": [{"type": "bar", "x": ["a", "b", "c"], "y": [3, 1, 2]}],
        "layout": {"title": "run"},
    }

    @classmethod
    def spec(cls) -> str:
        return json.dumps(cls.FIGURE, ensure_ascii=False)


async def test_run_visualize() -> None:
    body = ToolMain.toolset(visualize)[0].coroutine
    assert body is not None

    content, artifact = await body(spec=RunArgs.spec())

    print(content)
    print(artifact)
