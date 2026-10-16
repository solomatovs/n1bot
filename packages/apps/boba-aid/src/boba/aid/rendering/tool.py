"""Элементы ленты, которым нужен объект chainlit: график plotly из фигуры
блока ответа и карточка вьювера канваса.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import chainlit as cl

from boba.toolkit.result import VisualResult

__all__ = ["ChatElements"]


class ChatElements:
    """Элементы ленты: cl.Plotly по фигуре plotly и jsx-компонент
    public/elements/<имя>.jsx по карточке вьювера канваса (VisualResult).
    Создаёт его лента (ChatView) и зовёт из отрисовки блоков ответа."""

    def of_result(self, result: VisualResult) -> cl.CustomElement:
        """Карточка вьювера у шага ответа."""
        return cl.CustomElement(
            name=result.element, props=dict(result.props), display="inline"
        )

    def plotly(self, figure: Mapping[str, Any], title: str) -> cl.Plotly:
        """cl.Plotly из фигуры — единственное место, знающее про plotly."""
        from plotly import graph_objects as go  # noqa: PLC0415

        plotted = go.Figure(dict(figure))

        return cl.Plotly(name=title, figure=plotted, display="inline")
