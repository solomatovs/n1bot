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

from bs4.element import Tag

from boba.chainlit2.agent.tools.confluence.models import (
    ConfluenceKeys,
    ConfluencePayloadError,
    HttpKeys,
)
from boba.chainlit2.agent.tools.confluence.parsing import (
    ConfluenceHtml,
    ConfluenceJson,
    Heading,
)
from boba.html import HtmlKeys
from boba.indexing import (
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
    SectionKeys,
    SourceId,
)

__all__ = ["ConfluenceReader", "ConfluenceSearchHitsReader"]


class ConfluenceReader(Reader[str]):
    """Heading-aware Reader для Confluence-export HTML."""

    DOC_TYPE: ClassVar[str] = "confluence_html"
    READER_ID: ClassVar[ReaderId] = ReaderId("ext.confluence")
    BREADCRUMB_SEPARATOR: ClassVar[str] = " › "
    TITLE_LEVEL: ClassVar[int] = 0

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def read(self, value: RawDocument) -> Iterable[Section[str]]:
        payload = value.handle.read()
        if not payload.strip():
            return
        soup = ConfluenceHtml.parse_html(payload)
        body = soup.body or soup
        headings = [h for h in ConfluenceHtml.collect_headings(soup) if h.text.strip()]

        title = value.metadata.get(ReaderKeys.PAGE_TITLE) or ""

        if not headings:
            yield from self._fallback_section(value, body, title)
            return

        stack: list[tuple[int, str]] = []
        if title:
            stack.append((self.TITLE_LEVEL, title))

        for i, h in enumerate(headings):
            self._push_heading(stack, h.level, h.text)
            path = self._render_path(stack)
            next_tag = headings[i + 1].tag if i + 1 < len(headings) else None
            between = ConfluenceHtml.text_between(h.tag, next_tag)
            text = h.text + (("\n\n" + between) if between else "")
            yield Section(
                source_id=value.source_id,
                content=text.strip(),
                order=h.index,
                metadata=self._section_meta(value, h, path),
            )

    def _section_meta(self, value: RawDocument, h: Heading, path: str):
        meta = (
            value.metadata
            .set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
            .set(HtmlKeys.HEADING_LEVEL, h.level)
            .set(HtmlKeys.HEADING_TEXT, h.text)
            .set(SectionKeys.HEADING_PATH, path)
        )
        anchor = ConfluenceHtml.anchor_for(h)
        if anchor:
            meta = meta.set(SectionKeys.ANCHOR, anchor)
            src = meta.get(ConfluenceKeys.SOURCE_URL)
            if src and "#" not in src:
                meta = meta.set(ConfluenceKeys.SOURCE_URL, f"{src}#{anchor}")
        return meta

    def _fallback_section(
        self, value: RawDocument, body: Tag, title: str
    ) -> Iterable[Section[str]]:
        text = ConfluenceHtml.plain_text(body)
        if not text and not title:
            return
        composed = f"{title}\n\n{text}".strip() if title else text
        meta = value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
        if title:
            meta = meta.set(HtmlKeys.HEADING_TEXT, title)
            meta = meta.set(SectionKeys.HEADING_PATH, title)
        yield Section(
            source_id=value.source_id,
            content=composed,
            order=0,
            metadata=meta,
        )

    @staticmethod
    def _push_heading(stack: list[tuple[int, str]], level: int, text: str) -> None:
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text))

    @classmethod
    def _render_path(cls, stack: list[tuple[int, str]]) -> str:
        return cls.BREADCRUMB_SEPARATOR.join(text for _, text in stack)


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

    def __init__(self, *, base_url: str, snippet_chars: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._snippet_chars = snippet_chars

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
        text = ConfluenceHtml.plain_text(ConfluenceHtml.parse_html(html)).strip()
        if len(text) <= self._snippet_chars:
            return text
        return text[: self._snippet_chars - 1].rstrip() + "…"
