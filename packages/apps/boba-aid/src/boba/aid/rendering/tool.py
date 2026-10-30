"""Элементы ленты, которым нужен объект chainlit: карточка вьювера канваса,
карточка приложения MCP Apps в ленте и вопрос агента пользователю, а также
разбор настроек MCP Apps инструмента из его _meta.ui и источник вызова
инструмента (ToolSource) — то, по чему лента рисует вызов без обращения к
серверу.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, TypeAlias

import chainlit as cl
import mcp_types as mt
from fastmcp.apps.config import AppConfig
from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.result import VisualResult

__all__ = [
    "AppMeta",
    "AppProp",
    "AppResource",
    "AppResourceKind",
    "AppSource",
    "AppUnread",
    "ChatElements",
    "ElementName",
    "ToolAppView",
    "ToolSource",
]


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


class AppResourceKind(StrEnum):
    """Виды ресурса приложения в источнике вызова."""

    READ = "read"
    FAILED = "failed"


class AppResource(BaseModel):
    """Ресурс приложения MCP Apps на момент вызова — итог resources/read со
    всеми contents и их _meta: по нему приложение рисуется без сервера."""

    kind: Literal[AppResourceKind.READ] = AppResourceKind.READ
    resource: mt.ReadResourceResult


class AppUnread(BaseModel):
    """Ресурс приложения на момент вызова не прочитан: причина для человека,
    её показывает карточка приложения."""

    kind: Literal[AppResourceKind.FAILED] = AppResourceKind.FAILED
    error: str = Field(min_length=1)


AppSource: TypeAlias = Annotated[AppResource | AppUnread, Field(discriminator="kind")]
"""Ресурс приложения в источнике вызова по виду kind."""


class ToolSource(BaseModel):
    """Источник вызова инструмента сервера MCP на момент вызова: всё, по
    чему лента рисует вызов, не спрашивая сервер, — имя сервера профиля,
    карточка инструмента, описание сервера (server/discover с его
    расширениями журнала и вложенных вызовов), карточки инструментов, которые
    зовут узлы вложенного вызова, и ресурс приложения MCP Apps (None — у
    инструмента нет приложения либо чат не рисует приложений сервера).

    Собирает ретранслятор (McpRelay.source) по запросу агента
    `_boba/tool_source`; агент хранит его в истории и отдаёт содержимым
    первого уведомления вызова, ленту по нему рисует UpdateView — вживую и
    при воспроизведении одинаково.
    """

    server: str = Field(min_length=1)
    tool: mt.Tool
    description: mt.DiscoverResult
    nodes: list[mt.Tool] = Field(default_factory=list)
    app: AppSource | None = None


class ToolAppView(BaseModel):
    """Приложение MCP Apps инструмента: сервер профиля, описание
    инструмента на сервере (имя — в шапке карточки, описание целиком —
    toolInfo контекста хоста), адрес ui:// ресурса приложения, ресурс
    целиком на момент вызова (или причина, по которой его нет) и
    страница-прокси песочницы сервера. Знает его показ ленты (ToolMedia) по
    секции apps сервера и _meta.ui инструмента; элемент карточки строит
    ChatElements.app и рисует ресурс без resources/read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    server: str
    tool: mt.Tool
    resource_uri: str
    resource: AppSource
    sandbox_url: str


class AppProp(StrEnum):
    """Поля props элемента ToolApp сверх ToolAppView: вызов, его аргументы
    и исход — итог tools/call либо причина, по которой итога не будет."""

    CALL_ID = "call_id"
    INPUT = "input"
    RESULT = "result"
    CANCELLED = "cancelled"


class ElementName(StrEnum):
    """Свои jsx-элементы ленты: public/elements/<имя>.jsx."""

    APP = "ToolApp"
    QUESTION = "Question"


class ChatElements:
    """Элементы ленты: jsx-компонент public/elements/<имя>.jsx по карточке
    вьювера канваса (VisualResult),
    карточка приложения MCP Apps и вопрос агента пользователю
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
        """Карточка приложения вызова call_id с его аргументами; итог вызова
        (AppProp.RESULT) либо причину его отмены (AppProp.CANCELLED) элемент
        получает, когда вызов закончится."""
        props: dict[str, Any] = view.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        props[AppProp.CALL_ID.value] = call_id
        props[AppProp.INPUT.value] = dict(arguments)

        return cl.CustomElement(
            name=ElementName.APP.value, props=props, display="inline"
        )

    def question(self, props: Mapping[str, Any]) -> cl.CustomElement:
        """Вопрос агента: панель висящего вопроса либо запись ответа."""
        return cl.CustomElement(
            name=ElementName.QUESTION.value, props=dict(props), display="inline"
        )
