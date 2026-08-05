"""Операции над HTML в песочнице: недоверенную разметку разбирают только здесь."""

from __future__ import annotations

import sys
from typing import Any, ClassVar

import markdownify
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from boba.toolkit.launcher import RowStream
from boba.toolkit.payload import ChunkEmitter, PayloadEntry


class ConfluenceHtml:
    """Confluence-aware разбор через BeautifulSoup: structural-парсеры не берут ac:/ri:."""

    HEADING_TAGS: ClassVar[tuple[str, ...]] = ("h1", "h2", "h3", "h4", "h5", "h6")
    HEADING_TAG_NAMES: ClassVar[frozenset[str]] = frozenset(HEADING_TAGS)

    GOBACK: ClassVar[str] = "_GoBack"
    """Служебный anchor Confluence-экспорта, игнорируется при extraction'е."""

    @staticmethod
    def parse_html(data: str) -> BeautifulSoup:
        return BeautifulSoup(data, "lxml")

    @classmethod
    def collect_headings(cls, soup: BeautifulSoup) -> list[dict[str, Any]]:
        """Заголовки страницы: index 1-based, anchor или пустая строка."""
        headings: list[dict[str, Any]] = []
        for i, tag in enumerate(soup.find_all(list(cls.HEADING_TAGS)), start=1):
            anchor = cls.heading_anchor(tag)
            headings.append(
                {
                    "index": i,
                    "level": int(tag.name[1]),
                    "text": cls.heading_text(tag),
                    "anchor": anchor,
                    "tag": tag,
                }
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
    """Операции над HTML; вызываются диспетчером payload'а по имени op."""

    BREADCRUMB_SEPARATOR: ClassVar[str] = " › "
    TITLE_LEVEL: ClassVar[int] = 0

    OPS: ClassVar[tuple[str, ...]] = (
        "to_markdown",
        "plain_text",
        "confluence_sections",
    )

    @classmethod
    async def dispatch(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> dict[str, Any]:
        """Текст уходит кадрами-кусками, секции — кадрами-записями."""
        op = request["op"]
        if op == "to_markdown":
            PayloadEntry.emit_text(emit, cls.to_markdown(request)["markdown"])
            return {}
        if op == "plain_text":
            PayloadEntry.emit_text(emit, cls.plain_text(request)["text"])
            return {}
        if op == "confluence_sections":
            for section in cls.confluence_sections(request)["sections"]:
                emit(RowStream.encode(section))
            return {}
        msg = f"unknown page op: {op!r}"
        raise ValueError(msg)

    @staticmethod
    def to_markdown(request: dict[str, Any]) -> dict[str, Any]:
        markdown = markdownify.markdownify(
            request["html"], heading_style=request["heading_style"]
        )
        return {"markdown": markdown}

    @staticmethod
    def plain_text(request: dict[str, Any]) -> dict[str, Any]:
        soup = ConfluenceHtml.parse_html(request["html"])
        return {"text": ConfluenceHtml.plain_text(soup)}

    @classmethod
    def confluence_sections(cls, request: dict[str, Any]) -> dict[str, Any]:
        """Heading-aware нарезка страницы; без заголовков — одна секция."""
        html = request["html"]
        title = request["title"]
        if not html.strip():
            return {"sections": []}

        soup = ConfluenceHtml.parse_html(html)
        headings: list[dict[str, Any]] = []
        for heading in ConfluenceHtml.collect_headings(soup):
            if heading["text"].strip():
                headings.append(heading)
        if not headings:
            return {"sections": cls._fallback(soup, title)}

        stack: list[tuple[int, str]] = []
        if title:
            stack.append((cls.TITLE_LEVEL, title))

        sections: list[dict[str, Any]] = []
        for i, heading in enumerate(headings):
            cls._push(stack, heading["level"], heading["text"])
            next_tag = None
            if i + 1 < len(headings):
                next_tag = headings[i + 1]["tag"]
            between = ConfluenceHtml.text_between(heading["tag"], next_tag)
            text = heading["text"]
            if between:
                text = f"{text}\n\n{between}"
            anchor = heading["anchor"]
            if not anchor:
                anchor = f"idx:{heading['index']}"
            sections.append(
                {
                    "order": heading["index"],
                    "content": text.strip(),
                    "heading_level": heading["level"],
                    "heading_text": heading["text"],
                    "heading_path": cls._path(stack),
                    "anchor": anchor,
                }
            )
        return {"sections": sections}

    @classmethod
    def _fallback(cls, soup: BeautifulSoup, title: str) -> list[dict[str, Any]]:
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
        return [
            {
                "order": 0,
                "content": composed,
                "heading_level": 0,
                "heading_text": title,
                "heading_path": heading_path,
                "anchor": "",
            }
        ]

    @staticmethod
    def _push(stack: list[tuple[int, str]], level: int, text: str) -> None:
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text))

    @classmethod
    def _path(cls, stack: list[tuple[int, str]]) -> str:
        parts: list[str] = []
        for _, text in stack:
            parts.append(text)
        return cls.BREADCRUMB_SEPARATOR.join(parts)


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(PageOps.dispatch))
