"""cl.Plotly-элемент из ChartResult.spec — единственное место с plotly."""

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
    """ChartResult.spec -> cl.Plotly; невалидный spec бросает."""
    from plotly import graph_objects as go  # noqa: PLC0415 — ленивый импорт plotly

    figure = go.Figure(spec)
    return cl.Plotly(name=name, figure=figure, display=display)
