"""Элемент ленты для визуального виджета.

Вход и выход шага рисуют сами семейства вызова и результата (chat_view);
здесь только то, что требует chainlit-объекта: cl.Plotly и cl.CustomElement
для VisualElement из показа результата.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import chainlit as cl
from boba.toolkit.result import ChatElement, VisualElement, VisualResult
from chainlit.element import ElementDisplay

__all__ = ["ChatElements"]


class ChatElements:
    """Элемент ленты для VisualElement: встроенный Plotly по имени
    ChatElement.PLOTLY, остальное — jsx-компонент public/elements/<имя>.jsx."""

    @classmethod
    def of(
        cls, widget: VisualElement, *, display: ElementDisplay = "inline"
    ) -> cl.Plotly | cl.CustomElement:
        if widget.element == ChatElement.PLOTLY:
            return cls._plotly(widget, display)

        return cl.CustomElement(
            name=widget.element, props=dict(widget.props), display=display
        )

    @classmethod
    def of_result(cls, result: VisualResult) -> cl.Plotly | cl.CustomElement:
        """Элемент по визуальному результату: ссылка панели у шага ответа."""
        title = ""
        if result.title:
            title = result.title

        widget = VisualElement(element=result.element, props=result.props, title=title)

        return cls.of(widget)

    @staticmethod
    def _plotly(widget: VisualElement, display: ElementDisplay) -> cl.Plotly:
        """cl.Plotly из spec — единственное место, знающее про plotly."""
        from plotly import graph_objects as go  # noqa: PLC0415

        name = widget.title
        if not name:
            name = "chart"

        figure = go.Figure(dict(widget.props[VisualResult.PLOTLY_SPEC]))

        return cl.Plotly(name=name, figure=figure, display=display)
