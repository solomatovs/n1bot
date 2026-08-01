"""Парсинг Confluence: HTML (ac:*/ri:* макросы) и REST-JSON -> RawDocument.

- Heading               — DTO заголовка (level, text, anchor).
- ConfluenceHtml        — heading-aware extraction поверх BeautifulSoup+lxml:
  терпит кастомные namespace'ы Confluence-export'а (ac:/ri:), вычищает
  макросы, собирает heading'и и anchor'ы (scroll-bookmark / html-id / idx:N).
- ConfluenceJsonDecoder — REST-JSON -> HTML-handle + расширенная metadata
  (title/version/space/ancestors/attachments).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from io import BytesIO
from typing import Any, ClassVar

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from boba.chainlit2.agent.tools.confluence.models import (
    AttachmentInfo,
    ConfluenceKeys,
    ConfluencePayloadError,
    HttpKeys,
)
from boba.indexing import (
    Decoder,
    DecoderId,
    Metadata,
    RawDocument,
    ReaderKeys,
    TransportKeys,
)

__all__ = ["ConfluenceHtml", "ConfluenceJson", "ConfluenceJsonDecoder", "Heading"]


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
        return BeautifulSoup(data, "lxml")

    @staticmethod
    def collect_headings(
        soup: BeautifulSoup,
        *,
        max_depth: int | None = None,
    ) -> list[Heading]:
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
        return h.anchor if h.anchor else f"idx:{h.index}"

    @staticmethod
    def resolve_anchor(headings: list[Heading], anchor: str) -> Heading | None:
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
        soup = ConfluenceHtml.parse_html(html)
        for el in list(soup.find_all(ConfluenceHtml.is_confluence_macro)):
            el.decompose()
        body = soup.body
        if body is None:
            return str(soup)
        return body.decode_contents()

    @staticmethod
    def is_confluence_macro(tag: object) -> bool:
        name = getattr(tag, "name", None)
        return bool(name and name.startswith(("ac:", "ri:")))

    @staticmethod
    def _heading_anchor(tag: Tag) -> str | None:
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


class ConfluenceJson:
    """Извлечение полей из REST-JSON Confluence — единое место разбора схемы.

    Все обращения к ключам ответа (title/version/space/ancestors/_links/body/
    results/next) идут через эти @staticmethod'ы: page-decoder, search-reader и
    paginator парсят одну схему одинаково и терпимо к расхождениям версий
    Confluence (отсутствующие/нечисловые/не-dict блоки дают пустой результат).
    """

    @staticmethod
    def as_dict(v: Any) -> dict[str, Any]:
        return v if isinstance(v, dict) else {}

    @staticmethod
    def as_int(v: Any, *, default: int) -> int:
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def title(data: dict[str, Any]) -> str:
        return str(data.get("title") or "")

    @staticmethod
    def version_number(data: dict[str, Any]) -> int | None:
        n = ConfluenceJson.as_dict(data.get("version")).get("number")
        if n is None:
            return None
        try:
            return int(n)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def last_modified(data: dict[str, Any]) -> str:
        return str(ConfluenceJson.as_dict(data.get("version")).get("when") or "")

    @staticmethod
    def space_key(data: dict[str, Any]) -> str:
        return str(ConfluenceJson.as_dict(data.get("space")).get("key") or "")

    @staticmethod
    def ancestor_titles(data: dict[str, Any]) -> tuple[str, ...]:
        ancestors = data.get("ancestors")
        if not isinstance(ancestors, list):
            return ()
        return tuple(
            str(a.get("title", "")).strip()
            for a in ancestors
            if isinstance(a, dict) and str(a.get("title", "")).strip()
        )

    @staticmethod
    def source_url(data: dict[str, Any]) -> str:
        links = ConfluenceJson.as_dict(data.get("_links"))
        webui, base = links.get("webui"), links.get("base")
        if webui and base:
            return f"{str(base).rstrip('/')}{webui}"
        return ""

    @staticmethod
    def response_base(data: dict[str, Any]) -> str:
        base = ConfluenceJson.as_dict(data.get("_links")).get("base")
        return str(base).rstrip("/") if base else ""

    @staticmethod
    def webui(data: dict[str, Any]) -> str:
        return str(ConfluenceJson.as_dict(data.get("_links")).get("webui") or "")

    @staticmethod
    def body_html(data: dict[str, Any], body_format: str) -> str:
        body = ConfluenceJson.as_dict(data.get("body"))
        block = ConfluenceJson.as_dict(body.get(body_format))
        return str(block.get("value") or "")

    @staticmethod
    def results(data: dict[str, Any]) -> list[dict[str, Any]]:
        res = data.get("results")
        if isinstance(res, list):
            return res
        res = ConfluenceJson.as_dict(data.get("page")).get("results")
        return res if isinstance(res, list) else []

    @staticmethod
    def next_link(data: dict[str, Any]) -> str | None:
        nxt = ConfluenceJson.as_dict(data.get("_links")).get("next")
        return str(nxt) if nxt else None


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
        html = ConfluenceJson.body_html(data, self._body_format)

        meta = value.metadata.set(TransportKeys.CONTENT_TYPE, self._HTML_CONTENT_TYPE)
        if title := ConfluenceJson.title(data):
            meta = meta.set(ReaderKeys.PAGE_TITLE, title)
        if (version := ConfluenceJson.version_number(data)) is not None:
            meta = meta.set(ConfluenceKeys.VERSION, version)
        if (when := ConfluenceJson.last_modified(data)) and not meta.has(
            HttpKeys.LAST_MODIFIED,
        ):
            meta = meta.set(HttpKeys.LAST_MODIFIED, when)
        if space_key := ConfluenceJson.space_key(data):
            meta = meta.set(ConfluenceKeys.SPACE_KEY, space_key)
        if titles := ConfluenceJson.ancestor_titles(data):
            meta = meta.set(ConfluenceKeys.ANCESTORS_TITLES, titles)
        if source_url := ConfluenceJson.source_url(data):
            meta = meta.set(ConfluenceKeys.SOURCE_URL, source_url)
        meta = self._enrich_with_attachments(meta, data)

        return replace(
            value,
            handle=BytesIO(html.encode("utf-8")),
            metadata=meta,
        )

    @staticmethod
    def _enrich_with_attachments(meta: Metadata, data: dict[str, Any]) -> Metadata:
        block = ConfluenceJson.as_dict(
            ConfluenceJson.as_dict(data.get("children")).get("attachment"),
        )
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
        extensions = ConfluenceJson.as_dict(a.get("extensions"))
        version = ConfluenceJson.as_dict(a.get("version"))
        links = ConfluenceJson.as_dict(a.get("_links"))
        return AttachmentInfo(
            id=str(a.get("id", "")),
            title=str(a.get("title", "")),
            media_type=str(extensions.get("mediaType", "")),
            file_size=ConfluenceJson.as_int(extensions.get("fileSize"), default=0),
            download_path=str(links.get("download", "")),
            webui=str(links.get("webui", "")),
            version=ConfluenceJson.as_int(version.get("number"), default=1),
        )
