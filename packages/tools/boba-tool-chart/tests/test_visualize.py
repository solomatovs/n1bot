"""Тесты tool visualize: spec -> ChartResult, валидация, извлечение title."""

from __future__ import annotations

import pytest

from boba.tool.chart import visualize
from boba.tools.domain import ChartResult

_BAR = (
    '{"data":[{"type":"bar","x":["a","b"],"y":[1,2]}],'
    '"layout":{"title":{"text":"Продажи"}}}'
)


def test_valid_spec_returns_chart_result_with_title() -> None:
    res = visualize(spec=_BAR)
    assert isinstance(res, ChartResult)
    assert res.kind == "chart"
    assert res.title == "Продажи"
    assert res.spec["data"][0]["type"] == "bar"


def test_spec_without_title_yields_none_title() -> None:
    res = visualize(spec='{"data":[{"type":"scatter","x":[1],"y":[2]}]}')
    assert isinstance(res, ChartResult)
    assert res.title is None


def test_invalid_json_raises() -> None:
    with pytest.raises(RuntimeError, match="не является валидным JSON"):
        visualize(spec="not json")


def test_non_object_spec_raises() -> None:
    with pytest.raises(RuntimeError, match="JSON-объектом figure"):
        visualize(spec="[1, 2, 3]")


def test_invalid_plotly_structure_raises() -> None:
    # trace с несуществующим типом — go.Figure отвергает структуру.
    with pytest.raises(RuntimeError, match="невалидный Plotly figure-spec"):
        visualize(spec='{"data":[{"type":"no_such_trace"}]}')
