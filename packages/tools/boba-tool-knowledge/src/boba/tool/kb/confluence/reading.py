"""Reader'ы Confluence: export-HTML страницы и REST search-hits -> Section[str].

- ConfluenceReader           — heading-aware split одной export-страницы:
  каждая heading-секция (h1..h6) -> отдельная Section с anchor'ом из
  scroll-bookmark или fallback idx:N; содержимое ac:*/ri:* макросов
  исключается.
- ConfluenceSearchHitsReader — /rest/api/content/search-JSON -> одна
  Section на каждый hit (excerpt + реальный _links.webui URL +
  page/space/version meta).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, ClassVar

from boba.html.keys import HtmlKeys
from boba.indexing import (
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
    SectionKeys,
    SourceId,
)
from boba.tool.kb.confluence.models import (
    ConfluenceKeys,
    ConfluencePayloadError,
    HttpKeys,
)
from boba.tool.kb.confluence.parsing import ConfluenceJson
from boba.tool.kb.html import ConfluenceSection, HtmlCaller

__all__ = ["ConfluenceReader", "ConfluenceSearchHitsReader"]


class ConfluenceReader(Reader[str]):
    """Heading-aware Reader для Confluence-export HTML.

    Саму разметку разбирает payload в песочнице: сюда возвращаются готовые
    куски текста с их местом в дереве заголовков, а метаданные индексации
    (doc_type, breadcrumb, anchor в URL) проставляет уже приложение.
    """

    DOC_TYPE: ClassVar[str] = "confluence_html"
    READER_ID: ClassVar[ReaderId] = ReaderId("ext.confluence")

    def __init__(self, html_caller: HtmlCaller) -> None:
        self._html = html_caller

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def read(self, value: RawDocument) -> Iterable[Section[str]]:
        payload = value.handle.read()
        if not payload.strip():
            return
        html = payload.decode("utf-8", errors="replace")
        title = value.metadata.get(ReaderKeys.PAGE_TITLE) or ""
        answer = self._html.confluence_sections(html, title)
        for section in answer.sections:
            yield Section(
                source_id=value.source_id,
                content=section.content,
                order=section.order,
                metadata=self._section_meta(value, section),
            )

    def _section_meta(self, value: RawDocument, section: ConfluenceSection):
        meta = value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
        if section.heading_level:
            meta = meta.set(HtmlKeys.HEADING_LEVEL, section.heading_level)
        if section.heading_text:
            meta = meta.set(HtmlKeys.HEADING_TEXT, section.heading_text)
        if section.heading_path:
            meta = meta.set(SectionKeys.HEADING_PATH, section.heading_path)
        if not section.anchor:
            return meta
        meta = meta.set(SectionKeys.ANCHOR, section.anchor)
        src = meta.get(ConfluenceKeys.SOURCE_URL)
        if src and "#" not in src:
            meta = meta.set(ConfluenceKeys.SOURCE_URL, f"{src}#{section.anchor}")
        return meta


class ConfluenceSearchHitsReader(Reader[str]):
    """Search-JSON (/rest/api/content/search) -> Section[str] на каждый hit.

    Каждый hit: content — excerpt-плейнтекст из body.view.value (обрезан
    до snippet_chars); source_id — реальный _links.webui URL хита (не
    вычисляем хардкодом); metadata —
    PAGE_ID/PAGE_TITLE/SPACE_KEY/LAST_MODIFIED. Shape отдельной
    страницы (/rest/api/content/{id}) обрабатывают ConfluenceJsonDecoder
    + ConfluenceReader.
    """

    DOC_TYPE: ClassVar[str] = "confluence_search_hit"
    READER_ID: ClassVar[ReaderId] = ReaderId("ext.confluence_search_hits")

    def __init__(
        self, *, base_url: str, snippet_chars: int, html_caller: HtmlCaller
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._snippet_chars = snippet_chars
        self._html = html_caller

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def read(self, value: RawDocument) -> Iterable[Section[str]]:
        payload = value.handle.read()
        if not payload:
            return
        try:
            data: dict[str, Any] = json.loads(payload)
        except json.JSONDecodeError as e:
            raise ConfluencePayloadError(
                f"ConfluenceSearchHitsReader: invalid JSON from Confluence search: {e}"
            ) from e
        base = ConfluenceJson.response_base(data) or self._base_url
        for order, hit in enumerate(ConfluenceJson.results(data)):
            yield self._make_section(value, hit, order, base)

    def _make_section(
        self,
        value: RawDocument,
        hit: dict[str, Any],
        order: int,
        base: str,
    ) -> Section[str]:
        page_id = str(hit["id"])
        title = ConfluenceJson.title(hit)
        space_key = ConfluenceJson.space_key(hit)
        last_modified = ConfluenceJson.last_modified(hit)
        version = ConfluenceJson.version_number(hit)
        excerpt = self._make_excerpt(hit)
        webui = ConfluenceJson.webui(hit)
        url = f"{base}{webui}" if webui else base

        meta = (
            value.metadata
            .set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
            .set(ConfluenceKeys.PAGE_ID, page_id)
        )

        if title:
            meta = meta.set(ReaderKeys.PAGE_TITLE, title)

        if space_key:
            meta = meta.set(ConfluenceKeys.SPACE_KEY, space_key)

        if last_modified:
            meta = meta.set(HttpKeys.LAST_MODIFIED, last_modified)

        if version is not None:
            meta = meta.set(ConfluenceKeys.VERSION, version)

        return Section(
            source_id=SourceId(url),
            content=excerpt,
            order=order,
            metadata=meta,
        )

    def _make_excerpt(self, hit: dict[str, Any]) -> str:
        html = ConfluenceJson.body_html(hit, "view")
        if not html:
            return ""
        text = self._html.plain_text(html).strip()
        if len(text) <= self._snippet_chars:
            return text
        return text[: self._snippet_chars - 1].rstrip() + "…"
