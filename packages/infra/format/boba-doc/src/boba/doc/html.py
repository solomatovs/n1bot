"""HTML в текст: разбор недоверенной разметки BeautifulSoup и конверсия
дерева в markdown. Единственное место, где html становится текстом: web-
страницы читает HtmlDocument роутера, тела страниц Confluence — их парсер,
конвертер у обоих один.

Ошибки:
DocumentError — тело не разобралось как HTML (только через HtmlDocument).
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from typing import ClassVar

from bs4 import BeautifulSoup
from bs4.element import Tag
from markdownify import MarkdownConverter

from boba.doc.document import (
    ByteStream,
    DocumentKind,
    MemoryFile,
    PagedDocument,
    PageWindow,
    ParsedPage,
)

__all__ = ["HeadingStyle", "HtmlDocument", "HtmlMarkdown"]


class HeadingStyle(StrEnum):
    """Стили заголовков markdownify."""

    ATX = "ATX"
    ATX_CLOSED = "ATX_CLOSED"
    UNDERLINED = "UNDERLINED"


class HtmlMarkdown:
    """Дерево bs4 в markdown заданным стилем заголовков.

    Экранирование `_` и `*` нужно тексту для человека; текст индекса идёт
    без него, иначе идентификаторы вида dm.order_lines теряют вид.
    """

    def __init__(self, heading_style: HeadingStyle, *, escape: bool) -> None:
        self._converter = MarkdownConverter(
            heading_style=heading_style.value,
            escape_underscores=escape,
            escape_asterisks=escape,
        )

    def render(self, node: Tag) -> str:
        return str(self._converter.convert_soup(node)).strip()


class HtmlDocument(PagedDocument):
    """Страница HTML одной страницей markdown: кодировку даёт объявление
    в разметке или её байты (bs4), в текст идёт тело документа, скрипты и
    стили markdownify отбрасывает."""

    KIND: ClassVar[DocumentKind] = DocumentKind.HTML
    PARSER: ClassVar[str] = "lxml"

    def __init__(self, soup: BeautifulSoup, markdown: HtmlMarkdown) -> None:
        self._soup = soup
        self._markdown = markdown

    @classmethod
    def open(cls, stream: ByteStream, heading_style: HeadingStyle) -> HtmlDocument:
        raw = MemoryFile.data(stream)
        try:
            soup = BeautifulSoup(raw, cls.PARSER)
        except Exception as exc:
            raise cls.open_failure(exc) from exc

        return cls(soup, HtmlMarkdown(heading_style, escape=False))

    def page_count(self) -> int:
        return 1

    def pages(self, window: PageWindow) -> Iterator[ParsedPage]:
        for number in window.numbers(1):
            yield ParsedPage(number=number, text=self._markdown.render(self._body()))

    def close(self) -> None:
        self._soup.decompose()

    def _body(self) -> Tag:
        found = self._soup.body
        if found is None:
            return self._soup

        return found
