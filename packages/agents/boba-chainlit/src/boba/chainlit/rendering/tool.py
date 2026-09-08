"""Элемент ленты для визуального результата.

Вход и выход шага рисуют сами семейства вызова и результата (chat_view);
здесь только то, что требует chainlit-объекта: cl.Plotly и cl.CustomElement
для VisualResult.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import chainlit as cl
from boba.toolkit.result import ChatElement, VisualResult
from chainlit.element import ElementDisplay

__all__ = ["ChatElements"]


class ChatElements:
    """Элемент ленты для VisualResult: встроенный Plotly по имени
    ChatElement.PLOTLY, остальное — jsx-компонент public/elements/<имя>.jsx."""

    @classmethod
    def of(
        cls, result: VisualResult, *, display: ElementDisplay = "inline"
    ) -> cl.Plotly | cl.CustomElement:
        if result.element == ChatElement.PLOTLY:
            return cls._plotly(result, display)

        return cl.CustomElement(
            name=result.element, props=dict(result.props), display=display
        )

    @staticmethod
    def _plotly(result: VisualResult, display: ElementDisplay) -> cl.Plotly:
        """cl.Plotly из spec — единственное место, знающее про plotly."""
        from plotly import graph_objects as go  # noqa: PLC0415

        name = result.title
        if not name:
            name = "chart"

        figure = go.Figure(dict(result.props[VisualResult.PLOTLY_SPEC]))

        return cl.Plotly(name=name, figure=figure, display=display)
