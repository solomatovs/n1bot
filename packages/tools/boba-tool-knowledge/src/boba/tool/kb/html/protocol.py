"""Контракт html-payload'а: HTML на вход, markdown на выход."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ConfluenceSection",
    "ConfluenceSectionsAnswer",
    "ConfluenceSectionsRequest",
    "HtmlToMarkdownAnswer",
    "HtmlToMarkdownRequest",
    "PlainTextAnswer",
    "PlainTextRequest",
]


class HtmlToMarkdownRequest(BaseModel):
    """Конвертировать HTML в markdown."""

    model_config = ConfigDict(extra="forbid")

    OP: ClassVar[str] = "to_markdown"

    op: str = Field(min_length=1, description="Операция payload'а.")
    html: str = Field(description="Исходный HTML страницы.")
    heading_style: str = Field(
        min_length=1,
        description="Стиль заголовков markdownify: 'ATX'.",
    )

    @classmethod
    def of(cls, html: str, heading_style: str) -> HtmlToMarkdownRequest:
        return cls(op=cls.OP, html=html, heading_style=heading_style)


class HtmlToMarkdownAnswer(BaseModel):
    """Результат конверсии."""

    model_config = ConfigDict(extra="forbid")

    markdown: str


class PlainTextRequest(BaseModel):
    """Достать из HTML только текст, без макросов Confluence."""

    model_config = ConfigDict(extra="forbid")

    OP: ClassVar[str] = "plain_text"

    op: str = Field(min_length=1, description="Операция payload'а.")
    html: str = Field(description="Исходный HTML.")

    @classmethod
    def of(cls, html: str) -> PlainTextRequest:
        return cls(op=cls.OP, html=html)


class PlainTextAnswer(BaseModel):
    """Текст страницы одной строкой."""

    model_config = ConfigDict(extra="forbid")

    text: str


class ConfluenceSectionsRequest(BaseModel):
    """Нарезать Confluence-страницу по заголовкам."""

    model_config = ConfigDict(extra="forbid")

    OP: ClassVar[str] = "confluence_sections"

    op: str = Field(min_length=1, description="Операция payload'а.")
    html: str = Field(description="Export-HTML страницы.")
    title: str = Field(description="Заголовок страницы; корень breadcrumb'а.")

    @classmethod
    def of(cls, html: str, title: str) -> ConfluenceSectionsRequest:
        return cls(op=cls.OP, html=html, title=title)


class ConfluenceSection(BaseModel):
    """Кусок страницы: текст и место в дереве заголовков; пустая строка = значения нет."""

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
