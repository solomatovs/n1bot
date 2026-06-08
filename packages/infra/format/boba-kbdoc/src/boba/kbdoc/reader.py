"""KbDocReader — строгий Reader для формата KB-документа.

Формат (header — плоские key: value строки до разделителя ---):

    source: https://wiki.example.com/pages/viewpage.action?pageId=950276
    title: Правила именования-v6-20260318_191938
    page_id: 950276
    space: PAAS
    tags: dev, process        # опционально
    anchor: optional-id       # опционально
    version: 7                # опционально (confluence version.number)
    ---

    body content — markdown-текст оператора, индексируется целиком
    как одна Section.

Обязательные header-поля: source, title, page_id, space. Их
отсутствие (или отсутствие самого ---) — ошибка KbDocFormatError:
документ подготовлен не по формату и в KB не попадёт. Распознанные ключи
маппятся в типизированную metadata, нераспознанные — в reader.kbdoc.{key}.

**Body — единая ParagraphSection**. Это намеренный выбор: KB-документы
оператора — атомарные карточки знаний, каждый файл = ровно один логический
документ. Если body превышает chunk_size, splitter (OverlapCharSplitter)
сам уйдёт в paragraph-split в StructuralChunker.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

from boba.indexing import (
    IndexingError,
    Metadata,
    ParagraphSection,
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
    SectionKeys,
    SourceId,
)
from boba.indexing.chunks import ChunkKeys
from boba.kbdoc.keys import KbDocKeys

__all__ = ["KbDocFormatError", "KbDocReader", "ParsedKbDocHeader"]


_HEADER_SEPARATOR_RE: re.Pattern[str] = re.compile(
    r"^[ \t]*---[ \t]*$",
    re.MULTILINE,
)
"""Разделитель header/body — строка из трёх дефисов (опц. с whitespace)."""

_KV_RE: re.Pattern[str] = re.compile(
    r"^[ \t]*(?P<key>[\w][\w.\-]*)[ \t]*:[ \t]*(?P<value>.+?)[ \t]*$",
)
"""Плоская key: value строка header'а. Значение режется по первому :,
так что URL (https://...) в value не ломает разбор."""

_KEY_TAGS: str = "tags"
_KEY_SOURCE: str = "source"
_KEY_TITLE: str = "title"
_KEY_PAGE_ID: str = "page_id"
_KEY_SPACE: str = "space"
_KEY_ANCHOR: str = "anchor"
_KEY_VERSION: str = "version"

_REQUIRED_KEYS: tuple[str, ...] = (
    _KEY_SOURCE,
    _KEY_TITLE,
    _KEY_PAGE_ID,
    _KEY_SPACE,
)
"""Header-поля, без которых документ считается невалидным."""


class KbDocFormatError(IndexingError):
    """KB-документ подготовлен не по формату (нет --- или required-полей)."""

    def __init__(self, source_id: SourceId, missing: Iterable[str]) -> None:
        self.source_id = source_id
        self.missing = tuple(missing)
        super().__init__(
            f"kbdoc {str(source_id)!r} не по формату: "
            f"нет обязательных header-полей {list(self.missing)} "
            f"(требуются {list(_REQUIRED_KEYS)}; "
            f"header — плоские `key: value` строки до `---`)"
        )


@dataclass(frozen=True)
class ParsedKbDocHeader:
    """Структурированный результат парсинга header'а KB-документа."""

    title: str | None
    source_url: str | None
    page_id: str | None
    space: str | None
    tags: frozenset[str]
    anchor: str | None
    version: str | None
    custom: dict[str, str]
    body: str

    def missing_required(self) -> tuple[str, ...]:
        """Список обязательных header-полей, которых нет (в порядке контракта)."""
        present = {
            _KEY_SOURCE: self.source_url,
            _KEY_TITLE: self.title,
            _KEY_PAGE_ID: self.page_id,
            _KEY_SPACE: self.space,
        }
        return tuple(k for k in _REQUIRED_KEYS if not present[k])


class KbDocReader(Reader[str]):
    """Reader[str] для KB-document формата — один файл = одна Section.

    Строго требует header-поля source/title/page_id/space; иначе
    бросает KbDocFormatError (-> SourceFailed в индексаторе). Body отдаёт
    одной ParagraphSection без структурной разбивки — операторская
    KB-конвенция: каждый документ атомарен. Размерный split делает splitter
    в StructuralChunker.
    """

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.kbdoc")
    DOC_TYPE: ClassVar[str] = "kbdoc"
    DEFAULT_ENCODING: ClassVar[str] = "utf-8"

    def __init__(self, *, encoding: str = DEFAULT_ENCODING) -> None:
        self._encoding = encoding

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def read(self, value: RawDocument) -> Iterable[Section[str]]:
        text = value.handle.read().decode(self._encoding, errors="replace")
        parsed = self.parse(text)

        missing = parsed.missing_required()
        if missing:
            raise KbDocFormatError(value.source_id, missing)

        if not parsed.body:
            raise KbDocFormatError(value.source_id, ("body",))

        meta = self._enrich_metadata(value.metadata, parsed).set(
            ReaderKeys.DOC_TYPE,
            self.DOC_TYPE,
        )

        yield ParagraphSection(
            source_id=value.source_id,
            content=parsed.body,
            order=0,
            metadata=meta,
            tags=parsed.tags,
        )

    @classmethod
    def parse(cls, text: str) -> ParsedKbDocHeader:
        """Разбить документ на header (плоские key: value) и body.

        Без --- весь текст — body, а required-поля пустые -> невалидно.
        """
        match = _HEADER_SEPARATOR_RE.search(text)
        if match is None:
            return ParsedKbDocHeader(
                title=None,
                source_url=None,
                page_id=None,
                space=None,
                tags=frozenset(),
                anchor=None,
                version=None,
                custom={},
                body=text,
            )

        header_text = text[: match.start()]
        body = text[match.end():].lstrip("\n")

        fields = cls._extract_kv(header_text)
        return ParsedKbDocHeader(
            title=fields.get(_KEY_TITLE),
            source_url=fields.get(_KEY_SOURCE),
            page_id=fields.get(_KEY_PAGE_ID),
            space=fields.get(_KEY_SPACE),
            tags=cls._parse_tags(fields.get(_KEY_TAGS)),
            anchor=fields.get(_KEY_ANCHOR),
            version=fields.get(_KEY_VERSION),
            custom={
                k: v
                for k, v in fields.items()
                if k not in cls._KNOWN_KEYS
            },
            body=body,
        )

    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            _KEY_TAGS, _KEY_SOURCE, _KEY_TITLE, _KEY_PAGE_ID,
            _KEY_SPACE, _KEY_ANCHOR, _KEY_VERSION,
        }
    )

    @staticmethod
    def _extract_kv(header_text: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for line in header_text.splitlines():
            m = _KV_RE.match(line)
            if m is None:
                continue
            fields[m.group("key").lower()] = m.group("value").strip()
        return fields

    @staticmethod
    def _parse_tags(raw: str | None) -> frozenset[str]:
        if not raw:
            return frozenset()
        return frozenset(t.strip() for t in raw.split(",") if t.strip())

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
        if parsed.page_id:
            meta = meta.set(KbDocKeys.PAGE_ID, parsed.page_id)
        if parsed.space:
            meta = meta.set(KbDocKeys.SPACE, parsed.space)
        if parsed.version:
            with contextlib.suppress(ValueError):
                meta = meta.set(KbDocKeys.VERSION, int(parsed.version))
        if parsed.anchor:
            meta = meta.set(SectionKeys.ANCHOR, parsed.anchor)
            meta = meta.set(ChunkKeys.ANCHOR, parsed.anchor)
            # deep-link на индексации: #anchor в человекочитаемый SOURCE_URL
            src = meta.get(KbDocKeys.SOURCE_URL)
            if src and "#" not in src:
                meta = meta.set(KbDocKeys.SOURCE_URL, f"{src}#{parsed.anchor}")
        if parsed.custom:
            extras = {
                f"{KbDocKeys.CUSTOM_PREFIX}{k}": v
                for k, v in parsed.custom.items()
            }
            meta = meta.merge(Metadata.from_wire(extras))
        return meta
