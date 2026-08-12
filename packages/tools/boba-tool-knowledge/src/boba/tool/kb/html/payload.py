"""Операции над HTML в песочнице: недоверенную разметку разбирают только здесь.

Разметка приезжает каналом tool_stdin, продукт уходит в tool_payload: markdown
и текст — байтами, секции — строками NDJSON.

Ошибки:
PayloadError (no_input) — узлу не подали разметку.
ChannelError — в tool_args приехал запрос чужой модели. Ожидаемых ошибок
    разбора нет: bs4 и markdownify не отказывают на битой разметке — они её
    восстанавливают, поэтому любая ошибка здесь означает дефект кода.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar

import markdownify
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag
from pydantic import BaseModel

from boba.tool.kb.html.protocol import (
    ConfluenceSection,
    ConfluenceSectionsRequest,
    HtmlCall,
    HtmlNode,
    HtmlToMarkdownRequest,
    PlainTextRequest,
)
from boba.toolkit.channels import Channel, ChannelError, StreamCodec
from boba.toolkit.payload import PayloadChannels, PayloadEntry, PayloadError
from boba.toolkit.workflow import EmptyTrailer


@dataclass(frozen=True)
class Heading:
    """Заголовок страницы вместе с его узлом разметки."""

    index: int
    level: int
    text: str
    anchor: str
    tag: Tag


class ConfluenceHtml:
    """Confluence-aware разбор: structural-парсеры не берут ac:/ri:-макросы."""

    HEADING_TAGS: ClassVar[tuple[str, ...]] = ("h1", "h2", "h3", "h4", "h5", "h6")
    HEADING_TAG_NAMES: ClassVar[frozenset[str]] = frozenset(HEADING_TAGS)

    GOBACK: ClassVar[str] = "_GoBack"
    """Служебный anchor Confluence-экспорта, игнорируется при extraction'е."""

    @staticmethod
    def parse_html(data: str) -> BeautifulSoup:
        return BeautifulSoup(data, "lxml")

    @classmethod
    def collect_headings(cls, soup: BeautifulSoup) -> list[Heading]:
        """Заголовки страницы: index 1-based, anchor или пустая строка."""
        headings: list[Heading] = []

        for i, tag in enumerate(soup.find_all(list(cls.HEADING_TAGS)), start=1):
            headings.append(
                Heading(
                    index=i,
                    level=int(tag.name[1]),
                    text=cls.heading_text(tag),
                    anchor=cls.heading_anchor(tag),
                    tag=tag,
                )
            )

        return headings

    @classmethod
    def text_between(cls, start_tag: Tag, end_tag: Tag | None) -> str:
        parts: list[str] = []
        for el in start_tag.next_elements:
            if el is end_tag:
                break
            if not isinstance(el, NavigableString):
                continue
            if cls.is_inside_heading(el):
                continue
            if cls.is_inside_macro(el):
                continue
            parts.append(str(el))
        return " ".join(" ".join(parts).split())

    @classmethod
    def plain_text(cls, node: Tag) -> str:
        parts: list[str] = []
        for el in node.descendants:
            if not isinstance(el, NavigableString):
                continue
            if cls.is_inside_macro(el):
                continue
            parts.append(str(el))
        return " ".join(" ".join(parts).split())

    @staticmethod
    def is_confluence_macro(tag: object) -> bool:
        name = getattr(tag, "name", None)
        return bool(name and name.startswith(("ac:", "ri:")))

    @classmethod
    def heading_anchor(cls, tag: Tag) -> str:
        for sm in tag.find_all(
            lambda t: t.name == "ac:structured-macro" and t.get("ac:name") == "anchor",
        ):
            param = sm.find("ac:parameter")
            if param is None:
                continue
            name = param.get_text(strip=True)
            if name and name != cls.GOBACK:
                return name
        return cls.html_id(tag)

    @classmethod
    def heading_text(cls, tag: Tag) -> str:
        parts: list[str] = []
        for el in tag.descendants:
            if not isinstance(el, NavigableString):
                continue
            if cls.is_inside_macro(el):
                continue
            parts.append(str(el))
        return " ".join(" ".join(parts).split())

    @staticmethod
    def html_id(tag: Tag) -> str:
        raw = tag.get("id")
        if isinstance(raw, list):
            if raw:
                return str(raw[0])
            return ""
        if raw:
            return str(raw)
        return ""

    @classmethod
    def is_inside_heading(cls, el: NavigableString) -> bool:
        for parent in el.parents:
            if isinstance(parent, Tag) and parent.name in cls.HEADING_TAG_NAMES:
                return True
        return False

    @classmethod
    def is_inside_macro(cls, el: NavigableString) -> bool:
        for parent in el.parents:
            if isinstance(parent, Tag) and cls.is_confluence_macro(parent):
                return True
        return False


class PageOps:
    """Операции над HTML; вызываются диспетчером payload'а по модели запроса."""

    BREADCRUMB_SEPARATOR: ClassVar[str] = " › "
    TITLE_LEVEL: ClassVar[int] = 0

    HEADING_STYLE: ClassVar[str] = "ATX"
    """Стиль заголовков markdownify: решение инструмента, а не аргумент вызова."""

    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {}
    """Разбор HTML не отказывает: сломался — значит дефект, нужен трейсбек."""

    NO_INPUT: ClassVar[str] = "no_input"
    """kind отказа: узлу не подали разметку — ни ребром, ни литералом."""

    REQUESTS: ClassVar[Mapping[str, type[BaseModel]]] = {
        HtmlNode.MARKDOWN: HtmlToMarkdownRequest,
        HtmlNode.PLAIN_TEXT: PlainTextRequest,
        HtmlNode.SECTIONS: ConfluenceSectionsRequest,
    }

    @classmethod
    async def dispatch(
        cls,
        request: BaseModel,
        channels: PayloadChannels,
    ) -> BaseModel:
        """Текст уходит байтами, секции — строками NDJSON."""
        if not isinstance(request, HtmlCall):
            msg = f"html payload got an unexpected request: {type(request).__name__}"
            raise ChannelError(msg)

        html = cls.read_html(channels)

        if isinstance(request, HtmlToMarkdownRequest):
            cls.write_text(channels, cls.to_markdown(html))
            return EmptyTrailer()

        if isinstance(request, PlainTextRequest):
            cls.write_text(channels, cls.plain_text(html))
            return EmptyTrailer()

        if isinstance(request, ConfluenceSectionsRequest):
            cls.write_sections(channels, cls.confluence_sections(html, request.title))
            return EmptyTrailer()

        msg = f"html payload got an unexpected request: {type(request).__name__}"
        raise ChannelError(msg)

    @classmethod
    def read_html(cls, channels: PayloadChannels) -> str:
        """Разметка целиком: heading-aware нарезке нужен весь документ сразу."""
        if not channels.has(Channel.TOOL_STDIN):
            raise PayloadError(cls.NO_INPUT, "html markup is not fed to the stage")

        return StreamCodec.read_text(channels.stdin())

    @staticmethod
    def write_text(channels: PayloadChannels, text: str) -> None:
        channels.payload().write(StreamCodec.encode_text(text))

    @staticmethod
    def write_sections(
        channels: PayloadChannels,
        sections: Sequence[ConfluenceSection],
    ) -> None:
        stream = channels.payload()
        for section in sections:
            stream.write(StreamCodec.encode_row(section.model_dump()))

    @classmethod
    def to_markdown(cls, html: str) -> str:
        return markdownify.markdownify(html, heading_style=cls.HEADING_STYLE)

    @staticmethod
    def plain_text(html: str) -> str:
        soup = ConfluenceHtml.parse_html(html)

        return ConfluenceHtml.plain_text(soup)

    @classmethod
    def confluence_sections(cls, html: str, title: str) -> list[ConfluenceSection]:
        """Heading-aware нарезка страницы; без заголовков — одна секция."""
        if not html.strip():
            return []

        soup = ConfluenceHtml.parse_html(html)

        headings: list[Heading] = []
        for heading in ConfluenceHtml.collect_headings(soup):
            if heading.text.strip():
                headings.append(heading)

        if not headings:
            return cls._fallback(soup, title)

        stack: list[tuple[int, str]] = []
        if title:
            stack.append((cls.TITLE_LEVEL, title))

        sections: list[ConfluenceSection] = []
        for i, heading in enumerate(headings):
            cls._push(stack, heading.level, heading.text)

            next_tag: Tag | None = None
            if i + 1 < len(headings):
                next_tag = headings[i + 1].tag

            sections.append(cls._section(heading, next_tag, cls._path(stack)))

        return sections

    @classmethod
    def _section(
        cls,
        heading: Heading,
        next_tag: Tag | None,
        heading_path: str,
    ) -> ConfluenceSection:
        between = ConfluenceHtml.text_between(heading.tag, next_tag)

        text = heading.text
        if between:
            text = f"{text}\n\n{between}"

        anchor = heading.anchor
        if not anchor:
            anchor = f"idx:{heading.index}"

        return ConfluenceSection(
            order=heading.index,
            content=text.strip(),
            heading_level=heading.level,
            heading_text=heading.text,
            heading_path=heading_path,
            anchor=anchor,
        )

    @classmethod
    def _fallback(cls, soup: BeautifulSoup, title: str) -> list[ConfluenceSection]:
        """Страница без заголовков: весь текст одной секцией."""
        body = soup.body or soup
        text = ConfluenceHtml.plain_text(body)

        if not text and not title:
            return []

        composed = text
        if title:
            composed = f"{title}\n\n{text}".strip()

        heading_path = ""
        if title:
            heading_path = title

        section = ConfluenceSection(
            order=0,
            content=composed,
            heading_level=0,
            heading_text=title,
            heading_path=heading_path,
            anchor="",
        )

        return [section]

    @staticmethod
    def _push(stack: list[tuple[int, str]], level: int, text: str) -> None:
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text))

    @classmethod
    def _path(cls, stack: Sequence[tuple[int, str]]) -> str:
        parts: list[str] = []
        for _, text in stack:
            parts.append(text)
        return cls.BREADCRUMB_SEPARATOR.join(parts)


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(PageOps))
