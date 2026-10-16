"""Элементы ленты, которым нужен объект chainlit: график plotly из фигуры
блока ответа, карточка вьювера канваса и приложение MCP Apps у шага вызова.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

import chainlit as cl
from pydantic import BaseModel, ConfigDict

from boba.toolkit.result import VisualResult

__all__ = ["ChatElements", "ToolAppView"]


class ToolAppView(BaseModel):
    """Приложение MCP Apps инструмента: сервер профиля, инструмент на
    сервере, адрес ui:// ресурса приложения и страница-прокси песочницы
    сервера. Знает его показ ленты (ToolMedia) по секции apps сервера и
    _meta.ui инструмента; элемент шага строит ChatElements.app."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    server: str
    tool: str
    resource_uri: str
    sandbox_url: str


class ChatElements:
    """Элементы ленты: cl.Plotly по фигуре plotly, jsx-компонент
    public/elements/<имя>.jsx по карточке вьювера канваса (VisualResult) и
    приложение MCP Apps у шага вызова (APP_ELEMENT). Создаёт его лента
    (ChatView) и зовёт из отрисовки шагов и блоков ответа."""

    APP_ELEMENT: ClassVar[str] = "ToolApp"
    """Элемент приложения: public/elements/ToolApp.jsx."""

    def of_result(self, result: VisualResult) -> cl.CustomElement:
        """Карточка вьювера у шага ответа."""
        return cl.CustomElement(
            name=result.element, props=dict(result.props), display="inline"
        )

    def app(
        self, view: ToolAppView, call_id: str, arguments: Mapping[str, Any]
    ) -> cl.CustomElement:
        """Приложение вызова call_id с его аргументами; итог вызова элемент
        получает в props["result"], когда вызов закончится."""
        props: dict[str, Any] = view.model_dump()
        props["call_id"] = call_id
        props["input"] = dict(arguments)

        return cl.CustomElement(name=self.APP_ELEMENT, props=props, display="inline")

    def plotly(self, figure: Mapping[str, Any], title: str) -> cl.Plotly:
        """cl.Plotly из фигуры — единственное место, знающее про plotly."""
        from plotly import graph_objects as go  # noqa: PLC0415

        plotted = go.Figure(dict(figure))

        return cl.Plotly(name=title, figure=plotted, display="inline")
