"""HTML Reader'ы.

- `HtmlReader`             — default: structural parser, эмитит весь поток
                              типизированных `Section`-ов через
                              `HtmlSectionParser`.
- `HtmlPlainReader`        — один `ParagraphSection` на весь body + `<title>`.
                              Для документов где структура не нужна.
- `HtmlReadabilityReader`  — `trafilatura` отбрасывает boilerplate
                              (nav/footer/sidebar) → один `ParagraphSection`.

Все reader'ы:
- удаляют `<script>`/`<style>` из выдачи (HtmlSectionParser делает сам;
  Plain/Readability явно перед сериализацией);
- кладут `<title>` (если есть) в `ReaderKeys.PAGE_TITLE`;
- пробрасывают `RawDocument.source_id` и мержат `RawDocument.metadata` в Section.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

import trafilatura
from lxml import html as lxml_html

from boba.html.parser import HtmlSectionParser
from boba.indexing import (
    Metadata,
    ParagraphSection,
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
)

__all__ = ["HtmlPlainReader", "HtmlReadabilityReader", "HtmlReader"]


_NOISE_TAGS = ("script", "style")
_DOC_TYPE = "html"


def _extract_title(root) -> str:
    title_el = root.find(".//title")
    if title_el is None:
        return ""
    return (title_el.text_content() or "").strip()


def _strip_noise(root) -> None:
    for noise in root.iter(*_NOISE_TAGS):
        noise.drop_tree()


def _build_meta(value: RawDocument, title: str) -> Metadata:
    meta = value.metadata.set(ReaderKeys.DOC_TYPE, _DOC_TYPE)
    if title:
        meta = meta.set(ReaderKeys.PAGE_TITLE, title)
    return meta


class HtmlReader(Reader[str]):
    """Default HTML reader: structural-parsing через `HtmlSectionParser`.

    Эмитит весь поток типизированных секций (`HeadingSection`,
    `ParagraphSection`, `HtmlListSection`, `HtmlTableSection`,
    `HtmlCodeBlockSection`, `HtmlBlockquoteSection`,
    `HtmlHorizontalRuleSection`) с line-precision offset-tracking.
    """

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.html")

    def __init__(self) -> None:
        self._parser = HtmlSectionParser()

    def name(self) -> str:
        return "HtmlReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        payload = value.handle.read()
        if not payload.strip():
            return
        text = payload.decode("utf-8", errors="replace")
        # Title для metadata тащим отдельно — парсер его не trackит как секцию.
        try:
            root = lxml_html.fromstring(payload)
        except (lxml_html.etree.ParserError, ValueError):  # type: ignore[attr-defined]
            return
        title = _extract_title(root)
        base_meta = _build_meta(value, title)
        yield from self._parser.parse(
            text, source_id=value.source_id, base_metadata=base_meta,
        )


class HtmlPlainReader(Reader[str]):
    """Один `ParagraphSection` на весь body + `<title>`.

    Не пытается выделять main content (для этого `HtmlReadabilityReader`),
    не режет по структуре (для этого `HtmlReader`).
    """

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.html.plain")

    def name(self) -> str:
        return "HtmlPlainReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        payload = value.handle.read()
        if not payload.strip():
            return
        try:
            root = lxml_html.fromstring(payload)
        except (lxml_html.etree.ParserError, ValueError):  # type: ignore[attr-defined]
            return
        _strip_noise(root)

        title = _extract_title(root)
        body = root.find(".//body")
        target = body if body is not None else root
        text = self._render_block_text(target)
        if not text and not title:
            return

        composed = f"{title}\n\n{text}".strip() if title else text
        yield ParagraphSection(
            source_id=value.source_id,
            content=composed,
            order=0,
            metadata=_build_meta(value, title),
        )

    @staticmethod
    def _render_block_text(target) -> str:
        """Plain-text body c сохранением структуры: блочные элементы
        разделяются `\\n\\n`, остальной inline-текст склеен пробелом.
        """
        block_tags = (
            "p", "div", "section", "article", "header", "footer", "main",
            "aside", "nav", "h1", "h2", "h3", "h4", "h5", "h6",
            "ul", "ol", "li", "table", "tr", "blockquote", "pre", "hr", "br",
        )
        parts: list[str] = []
        seen: set[int] = set()
        for el in target.iter():
            if id(el) in seen:
                continue
            tag = el.tag.lower() if isinstance(el.tag, str) else None
            if tag not in block_tags:
                continue
            chunk = " ".join((el.text_content() or "").split())
            if not chunk:
                continue
            parts.append(chunk)
            seen.update(id(d) for d in el.iter())
        return "\n\n".join(parts)


class HtmlReadabilityReader(Reader[str]):
    """Content-extraction через `trafilatura`: отбрасывает boilerplate
    (nav/header/footer/sidebar) и возвращает plain-text main content одним
    `ParagraphSection`.

    **Зависимость**: `trafilatura` (опциональная). Установка:
    `pip install boba-html[readability]`.
    """

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.html.readability")

    def name(self) -> str:
        return "HtmlReadabilityReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        payload = value.handle.read()
        if not payload.strip():
            return

        text = trafilatura.extract(
            payload, output_format="txt", include_comments=False,
        )
        if not text:
            return

        try:
            root = lxml_html.fromstring(payload)
        except (lxml_html.etree.ParserError, ValueError):  # type: ignore[attr-defined]
            title = ""
        else:
            _strip_noise(root)
            title = _extract_title(root)

        cleaned = text.strip()
        yield ParagraphSection(
            source_id=value.source_id,
            content=cleaned,
            order=0,
            metadata=_build_meta(value, title),
        )
