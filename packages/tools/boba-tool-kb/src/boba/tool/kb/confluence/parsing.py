"""Парсинг Confluence: HTML (ac:*/ri:* макросы) и REST-JSON -> RawDocument.

- Heading               — DTO заголовка (level, text, anchor).
- ConfluenceHtml        — heading-aware extraction поверх BeautifulSoup+lxml:
  терпит кастомные namespace'ы Confluence-export'а (ac:/ri:), вычищает
  макросы, собирает heading'и и anchor'ы (scroll-bookmark / html-id / idx:N).
- ConfluenceJsonDecoder — REST-JSON -> HTML-handle + расширенная metadata
  (title/version/space/ancestors/attachments).
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, replace
from io import BytesIO
from typing import Any, ClassVar

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from boba.indexing import (
    Decoder,
    DecoderId,
    Metadata,
    RawDocument,
    ReaderKeys,
    TransportKeys,
)
from boba.tool.kb.confluence.models import (
    AttachmentInfo,
    ConfluenceKeys,
    ConfluencePayloadError,
    HttpKeys,
)

__all__ = ["ConfluenceHtml", "ConfluenceJsonDecoder", "Heading"]


@dataclass(frozen=True)
class Heading:
    """Заголовок Confluence-страницы. index 1-based; anchor —
    scroll-bookmark или html id, None если ни того, ни другого.
    """

    index: int
    level: int
    text: str
    anchor: str | None
    tag: Tag


class ConfluenceHtml:
    """Confluence-aware HTML-парсер: heading'и, anchor'ы, текст без макросов.

    Self-contained: BeautifulSoup поверх lxml, без зависимости на boba.html
    (там structural parser, не подходящий для кастомных namespace'ов
    ac:/ri: Confluence-export'а).
    """

    _HEADING_TAGS: ClassVar[tuple[str, ...]] = ("h1", "h2", "h3", "h4", "h5", "h6")
    _HEADING_TAG_NAMES: ClassVar[frozenset[str]] = frozenset(_HEADING_TAGS)

    _GOBACK: ClassVar[str] = "_GoBack"
    """Служебный браузерный anchor (Confluence ставит его в начало страницы
    при экспорте; ни на что не ссылается). Игнорируем при extraction'е."""

    @staticmethod
    def parse_html(data: bytes | str) -> BeautifulSoup:
        """BeautifulSoup поверх lxml — терпит произвольные namespace'ы (ac:/ri:)."""
        return BeautifulSoup(data, "lxml")

    @staticmethod
    def collect_headings(
        soup: BeautifulSoup,
        *,
        max_depth: int | None = None,
    ) -> list[Heading]:
        """Все <h1>..<h6> в document order. Текст и anchor — Confluence-aware."""
        cap = max_depth if max_depth is not None else 6
        tags = soup.find_all(list(ConfluenceHtml._HEADING_TAGS[:cap]))
        headings: list[Heading] = []
        for i, tag in enumerate(tags, start=1):
            level = int(tag.name[1])
            headings.append(
                Heading(
                    index=i,
                    level=level,
                    text=ConfluenceHtml._heading_text(tag),
                    anchor=ConfluenceHtml._heading_anchor(tag),
                    tag=tag,
                ),
            )
        return headings

    @staticmethod
    def anchor_for(h: Heading) -> str:
        """
        Canonical-anchor: html-id/scroll-bookmark
        или idx:N если ни того, ни другого
        """
        return h.anchor if h.anchor else f"idx:{h.index}"

    @staticmethod
    def resolve_anchor(headings: list[Heading], anchor: str) -> Heading | None:
        """Найти heading по anchor: html-id/scroll-bookmark или idx:N
        (с/без ведущего #).
        """
        key = anchor.lstrip("#").strip()
        if key.startswith("idx:"):
            try:
                idx = int(key[4:])
            except ValueError:
                return None
            return next((h for h in headings if h.index == idx), None)
        return next((h for h in headings if h.anchor == key), None)

    @staticmethod
    def text_between(start_tag: Tag, end_tag: Tag | None) -> str:
        """Конкатенация всех NavigableString между start_tag (исключая) и
        end_tag, с пропуском содержимого ac:*/ri:* макросов и текста самих
        heading-тегов (он живёт в Heading.text).
        """
        parts: list[str] = []
        for el in start_tag.next_elements:
            if el is end_tag:
                break
            if not isinstance(el, NavigableString):
                continue
            if ConfluenceHtml._is_inside_heading(el):
                continue
            if ConfluenceHtml._is_inside_macro(el):
                continue
            parts.append(str(el))
        return " ".join(" ".join(parts).split())

    @staticmethod
    def plain_text(node: Tag) -> str:
        """Plain-text всего поддерева, с пропуском ac:*/ri:* содержимого."""
        parts: list[str] = []
        for el in node.descendants:
            if not isinstance(el, NavigableString):
                continue
            if ConfluenceHtml._is_inside_macro(el):
                continue
            parts.append(str(el))
        return " ".join(" ".join(parts).split())

    @staticmethod
    def strip_confluence_macros(html: str) -> str:
        """Вырезать ac:*/ri:* поддеревья из HTML-строки (целиком, с children)."""
        soup = ConfluenceHtml.parse_html(html)
        for el in list(soup.find_all(ConfluenceHtml.is_confluence_macro)):
            el.decompose()
        body = soup.body
        if body is None:
            return str(soup)
        return body.decode_contents()

    @staticmethod
    def is_confluence_macro(tag: object) -> bool:
        """True если тег принадлежит Confluence-расширениям (ac:* / ri:*)."""
        name = getattr(tag, "name", None)
        return bool(name and name.startswith(("ac:", "ri:")))

    @staticmethod
    def _heading_anchor(tag: Tag) -> str | None:
        """Anchor heading'а: scroll-bookmark из ac:structured-macro или html-id."""
        for sm in tag.find_all(
            lambda t: t.name == "ac:structured-macro" and t.get("ac:name") == "anchor",
        ):
            param = sm.find("ac:parameter")
            if param is None:
                continue
            name = param.get_text(strip=True)
            if name and name != ConfluenceHtml._GOBACK:
                return name
        return ConfluenceHtml._extract_html_id(tag)

    @staticmethod
    def _heading_text(tag: Tag) -> str:
        """Текст heading'а — все NavigableString рекурсивно, без ac:*/ri:*."""
        parts: list[str] = []
        for el in tag.descendants:
            if not isinstance(el, NavigableString):
                continue
            if any(
                isinstance(p, Tag) and ConfluenceHtml.is_confluence_macro(p)
                for p in el.parents
            ):
                continue
            parts.append(str(el))
        return " ".join(" ".join(parts).split())

    @staticmethod
    def _extract_html_id(tag: Tag) -> str | None:
        """Достать html id heading-тега; пустой / list -> None."""
        raw = tag.get("id")
        if isinstance(raw, list):
            return raw[0] if raw else None
        return str(raw) if raw else None

    @staticmethod
    def _is_inside_heading(el: NavigableString) -> bool:
        return any(
            isinstance(p, Tag) and p.name in ConfluenceHtml._HEADING_TAG_NAMES
            for p in el.parents
        )

    @staticmethod
    def _is_inside_macro(el: NavigableString) -> bool:
        return any(
            isinstance(p, Tag) and ConfluenceHtml.is_confluence_macro(p)
            for p in el.parents
        )


class ConfluenceJsonDecoder(Decoder):
    """Confluence REST JSON -> HTML-handle + расширенная metadata.

    Вынимает HTML из body.<body_format>.value, обогащает metadata: title
    (ReaderKeys.PAGE_TITLE), version (ConfluenceKeys.VERSION), space,
    ancestors, attachments, last_modified (HttpKeys.LAST_MODIFIED, если ещё
    не заполнен HttpTransport'ом).
    """

    DECODER_ID: ClassVar[DecoderId] = DecoderId("ext.confluence_json")

    _HTML_CONTENT_TYPE: ClassVar[str] = "text/html"
    """После JSON->HTML распаковки handle содержит HTML; CONTENT_TYPE приводим к
    этому факту, чтобы DispatchReader мог честно роутить страницы через
    HTMLReader (а не через несуществующий JSONReader)."""

    def __init__(self, *, body_format: str = "export_view") -> None:
        self._body_format = body_format

    def decoder_id(self) -> DecoderId:
        return self.DECODER_ID

    def decode(self, value: RawDocument) -> RawDocument:
        payload = value.handle.read()
        if not payload:
            return value
        try:
            data: dict[str, Any] = json.loads(payload)
        except json.JSONDecodeError as e:
            raise ConfluencePayloadError(
                f"ConfluenceJsonDecoder: невалидный JSON от Confluence: {e}"
            ) from e
        body = data.get("body") or {}
        body_block = body.get(self._body_format) or {}
        html = str(body_block.get("value", "")) if isinstance(body_block, dict) else ""

        meta = value.metadata.set(TransportKeys.CONTENT_TYPE, self._HTML_CONTENT_TYPE)
        if title := data.get("title"):
            meta = meta.set(ReaderKeys.PAGE_TITLE, str(title))
        version = data.get("version") or {}
        if isinstance(version, dict):
            if (n := version.get("number")) is not None:
                with contextlib.suppress(TypeError, ValueError):
                    meta = meta.set(ConfluenceKeys.VERSION, int(n))
            if (when := version.get("when")) and not meta.has(HttpKeys.LAST_MODIFIED):
                meta = meta.set(HttpKeys.LAST_MODIFIED, str(when))
        space = data.get("space") or {}
        if isinstance(space, dict) and (space_key := space.get("key")):
            meta = meta.set(ConfluenceKeys.SPACE_KEY, str(space_key))
        ancestors = data.get("ancestors")
        if isinstance(ancestors, list):
            titles = tuple(
                str(a.get("title", "")).strip()
                for a in ancestors
                if isinstance(a, dict) and str(a.get("title", "")).strip()
            )
            if titles:
                meta = meta.set(ConfluenceKeys.ANCESTORS_TITLES, titles)
        meta = self._enrich_with_attachments(meta, data)

        return replace(
            value,
            handle=BytesIO(html.encode("utf-8")),
            metadata=meta,
        )

    @staticmethod
    def _enrich_with_attachments(meta: Metadata, data: dict[str, Any]) -> Metadata:
        """Put children.attachment.results[] into ConfluenceKeys.ATTACHMENTS.

        Пустой/отсутствующий список -> meta без изменений (тот же pattern,
        что у ANCESTORS_TITLES). Структурно непригодный JSON тоже даёт no-op:
        Decoder не должен взрываться на расхождении схемы — Confluence в
        разных версиях кладёт expand-блоки по-разному.
        """
        children = data.get("children")
        if not isinstance(children, dict):
            return meta
        block = children.get("attachment")
        if not isinstance(block, dict):
            return meta
        results = block.get("results")
        if not isinstance(results, list):
            return meta
        items = tuple(
            ConfluenceJsonDecoder._attachment_from_json(a)
            for a in results
            if isinstance(a, dict)
        )
        if not items:
            return meta
        return meta.set(ConfluenceKeys.ATTACHMENTS, items)

    @staticmethod
    def _attachment_from_json(a: dict[str, Any]) -> AttachmentInfo:
        """Один Confluence-attachment JSON-объект -> AttachmentInfo.

        Missing/нечисловые extensions.fileSize и version.number фолбэчатся
        в 0 и 1 — пользователю download'а размер не критичен, version по
        умолчанию 1 соответствует Confluence-семантике первой загрузки.
        """
        extensions = ConfluenceJsonDecoder._dict_or_empty(a.get("extensions"))
        version = ConfluenceJsonDecoder._dict_or_empty(a.get("version"))
        links = ConfluenceJsonDecoder._dict_or_empty(a.get("_links"))
        return AttachmentInfo(
            id=str(a.get("id", "")),
            title=str(a.get("title", "")),
            media_type=str(extensions.get("mediaType", "")),
            file_size=ConfluenceJsonDecoder._int_or(
                extensions.get("fileSize"), default=0,
            ),
            download_path=str(links.get("download", "")),
            version=ConfluenceJsonDecoder._int_or(version.get("number"), default=1),
        )

    @staticmethod
    def _dict_or_empty(v: Any) -> dict[str, Any]:
        return v if isinstance(v, dict) else {}

    @staticmethod
    def _int_or(v: Any, *, default: int) -> int:
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default
