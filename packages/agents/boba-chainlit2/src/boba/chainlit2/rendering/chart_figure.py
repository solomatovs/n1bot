"""Построение cl.Plotly-элемента из ChartResult.spec.

Порт boba.chainlit.rendering.chart_figure. Единственное место, знающее
о plotly: домен (ChartResult) оперирует чистым dict'ом (Plotly figure JSON),
а сборка go.Figure + cl.Plotly живёт здесь, в presentation-слое.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import chainlit as cl
from chainlit.element import ElementDisplay

__all__ = ["build_plotly_element"]


def build_plotly_element(
    name: str,
    spec: Mapping[str, Any],
    *,
    display: ElementDisplay = "inline",
) -> cl.Plotly:
    """ChartResult.spec (Plotly figure JSON) -> cl.Plotly.

    go.Figure(spec) валидирует структуру; невалидный spec бросит — вызывающий
    (tracer) ловит и откатывается на текстовую заглушку.
    """
    from plotly import graph_objects as go  # noqa: PLC0415 — ленивый импорт plotly

    figure = go.Figure(spec)
    return cl.Plotly(name=name, figure=figure, display=display)
