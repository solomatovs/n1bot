"""Элементы ленты, которым нужен объект chainlit: график plotly из фигуры
блока ответа, карточка вьювера канваса, приложение MCP Apps у шага вызова и
вопрос агента пользователю, а также разбор настроек MCP Apps инструмента из
его _meta.ui.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar

import chainlit as cl
import mcp_types as mt
from fastmcp.apps.config import AppConfig
from pydantic import BaseModel, ConfigDict

from boba.toolkit.result import VisualResult

__all__ = ["AppMeta", "ChatElements", "ElementName", "ToolAppView"]


class AppMeta:
    """Настройки MCP Apps инструмента из его _meta.ui (AppConfig fastmcp):
    адрес приложения и кому инструмент виден.

    Держат полем ретранслятор (видимость инструментов модели и приложению)
    и показ ленты (адрес приложения у шага). Что делать с _meta.ui не по
    форме, решает владелец.
    """

    UI_META: ClassVar[str] = "ui"
    """Ключ _meta инструмента с настройками MCP Apps."""

    def of(self, tool: mt.Tool) -> AppConfig | None:
        """Настройки инструмента; None — у него нет _meta.ui.

        Ошибки:
        ValidationError — _meta.ui не по форме настроек MCP Apps.
        """
        meta = tool.meta
        if meta is None:
            return None

        ui = meta.get(self.UI_META)
        if ui is None:
            return None

        return AppConfig.model_validate(ui)


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


class ElementName(StrEnum):
    """Свои jsx-элементы ленты: public/elements/<имя>.jsx."""

    APP = "ToolApp"
    QUESTION = "Question"


class ChatElements:
    """Элементы ленты: cl.Plotly по фигуре plotly, jsx-компонент
    public/elements/<имя>.jsx по карточке вьювера канваса (VisualResult),
    приложение MCP Apps у шага вызова и вопрос агента пользователю
    (ElementName). Создаёт его лента (ChatView) и приёмник вкладки (вопрос,
    который ждёт ответа)."""

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

        return cl.CustomElement(
            name=ElementName.APP.value, props=props, display="inline"
        )

    def question(self, props: Mapping[str, Any]) -> cl.CustomElement:
        """Вопрос агента: панель висящего вопроса либо запись ответа."""
        return cl.CustomElement(
            name=ElementName.QUESTION.value, props=dict(props), display="inline"
        )

    def plotly(self, figure: Mapping[str, Any], title: str) -> cl.Plotly:
        """cl.Plotly из фигуры — единственное место, знающее про plotly."""
        from plotly import graph_objects as go  # noqa: PLC0415

        plotted = go.Figure(dict(figure))

        return cl.Plotly(name=title, figure=plotted, display="inline")
