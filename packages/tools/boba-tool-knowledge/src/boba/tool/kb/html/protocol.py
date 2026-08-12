"""Контракт html-payload'а: HTML приходит потоком, продукт уходит потоком."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ConfluenceSection",
    "HtmlCall",
    "ConfluenceSectionsAnswer",
    "ConfluenceSectionsRequest",
    "HtmlNode",
    "HtmlToMarkdownRequest",
    "PlainTextRequest",
]


class HtmlNode(StrEnum):
    """Узлы реестра стадий над HTML; значение — оно же поле op запроса."""

    MARKDOWN = "html_to_markdown"
    PLAIN_TEXT = "html_plain_text"
    SECTIONS = "html_confluence_sections"


class HtmlCall(BaseModel):
    """Общая часть: разметка приезжает каналом tool_stdin и читается текстом."""

    model_config = ConfigDict(extra="forbid")

    op: HtmlNode


class HtmlToMarkdownRequest(HtmlCall):
    """Конвертировать HTML в markdown."""


class PlainTextRequest(HtmlCall):
    """Достать из HTML только текст, без макросов Confluence."""


class ConfluenceSectionsRequest(HtmlCall):
    """Нарезать Confluence-страницу по заголовкам."""

    title: str = Field(description="Заголовок страницы; корень breadcrumb'а.")


class ConfluenceSection(BaseModel):
    """Кусок страницы: текст и место в дереве заголовков; пусто = значения нет."""

    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=0)
    content: str
    heading_level: int = Field(ge=0)
    heading_text: str
    heading_path: str
    anchor: str


class ConfluenceSectionsAnswer(BaseModel):
    """Секции страницы по порядку заголовков."""

    model_config = ConfigDict(extra="forbid")

    sections: tuple[ConfluenceSection, ...]
