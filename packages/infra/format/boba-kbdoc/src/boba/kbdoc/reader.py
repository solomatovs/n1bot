"""KbDocReader — Reader-обёртка над форматом KB-документа.

Формат:

    # Title (опционально)

    **tags:** tag1, tag2, tag3
    **source:** https://wiki.example.com/page
    **anchor:** optional-anchor-id

    ---

    body content — передаётся в inner Reader как есть.

Header-блок (всё до строки `---`) парсится сюда — title, tags,
source, anchor, и любые `**key:** value` строки. Body отдаётся
во вложенный `inner: Reader[str]` (`MarkdownReader`, `PlainTextReader`,
`HtmlReader` — что угодно) через под-RawDocument с тем же
`source_id` и обогащённой `metadata`.

Возвращённые из inner Section[str] дополняются `tags` из header'а
(объединение, не замена).

`---` обязателен, если в файле есть header — без разделителя весь
файл считается body без metadata. Это сознательно простой формат:
fail-fast на двусмысленность не нужен.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from io import BytesIO
from typing import ClassVar

from boba.indexing import (
    Metadata,
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
    SectionKeys,
)
from boba.indexing.chunks import ChunkKeys
from boba.kbdoc.keys import KbDocKeys

__all__ = ["KbDocReader", "ParsedKbDocHeader"]


_HEADER_SEPARATOR_RE: re.Pattern[str] = re.compile(
    r"^[ \t]*---[ \t]*$",
    re.MULTILINE,
)
"""Разделитель header/body — строка из трёх дефисов (опц. с whitespace)."""

_H1_RE: re.Pattern[str] = re.compile(r"^[ \t]*#[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)
"""`# Title` на отдельной строке внутри header'а — опционально."""

_INLINE_KV_RE: re.Pattern[str] = re.compile(
    r"\*\*(?P<key>[\w][\w.\-]*?)\s*:\s*\*\*\s*(?P<value>[^\n]+)",
)
"""`**key:** value` пары внутри header'а — multi-occurrence."""

_KEY_TAGS: str = "tags"
_KEY_SOURCE: str = "source"
_KEY_SOURCE_URL: str = "source_url"
_KEY_ANCHOR: str = "anchor"


@dataclass(frozen=True)
class ParsedKbDocHeader:
    """Структурированный результат парсинга header'а KB-документа."""

    title: str | None
    tags: frozenset[str]
    source_url: str | None
    anchor: str | None
    custom: dict[str, str]
    body: str


class KbDocReader(Reader[str]):
    """Reader[str] для KB-document формата с делегированием body в inner.

    Конструктор принимает `inner: Reader[str]` — Reader для body
    (`MarkdownReader`, `PlainTextReader`, `HtmlReader`, ...). KbDocReader
    сам парсит header, формирует обогащённую metadata, отдаёт body
    во вложенный Reader, и приписывает header-tags к каждой
    возвращённой Section.
    """

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.kbdoc")
    DEFAULT_ENCODING: ClassVar[str] = "utf-8"

    def __init__(
        self,
        *,
        inner: Reader[str],
        encoding: str = DEFAULT_ENCODING,
    ) -> None:
        self._inner = inner
        self._encoding = encoding

    def name(self) -> str:
        return "KbDocReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        text = value.handle.read().decode(self._encoding, errors="replace")
        parsed = self.parse(text)

        meta = self._enrich_metadata(value.metadata, parsed)
        sub_doc = RawDocument(
            handle=BytesIO(parsed.body.encode(self._encoding)),
            source_id=value.source_id,
            metadata=meta,
        )

        for section in self._inner.convert(sub_doc):
            if parsed.tags:
                yield replace(section, tags=section.tags | parsed.tags)
            else:
                yield section

    @classmethod
    def parse(cls, text: str) -> ParsedKbDocHeader:
        """Разбить документ на header (metadata) и body."""
        match = _HEADER_SEPARATOR_RE.search(text)
        if match is None:
            return ParsedKbDocHeader(
                title=None,
                tags=frozenset(),
                source_url=None,
                anchor=None,
                custom={},
                body=text,
            )

        header_text = text[: match.start()]
        body = text[match.end():].lstrip("\n")

        title = cls._extract_title(header_text)
        tags, source_url, anchor, custom = cls._extract_kv(header_text)
        return ParsedKbDocHeader(
            title=title,
            tags=tags,
            source_url=source_url,
            anchor=anchor,
            custom=custom,
            body=body,
        )

    @staticmethod
    def _extract_title(header_text: str) -> str | None:
        m = _H1_RE.search(header_text)
        if m is None:
            return None
        title = m.group("title").strip()
        return title or None

    @staticmethod
    def _extract_kv(
        header_text: str,
    ) -> tuple[frozenset[str], str | None, str | None, dict[str, str]]:
        tags: frozenset[str] = frozenset()
        source_url: str | None = None
        anchor: str | None = None
        custom: dict[str, str] = {}

        for m in _INLINE_KV_RE.finditer(header_text):
            key = m.group("key").lower()
            value = m.group("value").strip()
            if not value:
                continue
            if key == _KEY_TAGS:
                tags = frozenset(
                    t.strip() for t in value.split(",") if t.strip()
                )
            elif key in (_KEY_SOURCE, _KEY_SOURCE_URL):
                source_url = value
            elif key == _KEY_ANCHOR:
                anchor = value
            else:
                custom[key] = value
        return tags, source_url, anchor, custom

    @staticmethod
    def _enrich_metadata(
        base: Metadata,
        parsed: ParsedKbDocHeader,
    ) -> Metadata:
        meta = base
        if parsed.title:
            meta = meta.set(ReaderKeys.PAGE_TITLE, parsed.title)
        if parsed.source_url:
            meta = meta.set(KbDocKeys.SOURCE_URL, parsed.source_url)
        if parsed.anchor:
            meta = meta.set(SectionKeys.ANCHOR, parsed.anchor)
            meta = meta.set(ChunkKeys.ANCHOR, parsed.anchor)
        if parsed.custom:
            extras = {
                f"{KbDocKeys.CUSTOM_PREFIX}{k}": v
                for k, v in parsed.custom.items()
            }
            meta = meta.merge(Metadata.from_wire(extras))
        return meta
