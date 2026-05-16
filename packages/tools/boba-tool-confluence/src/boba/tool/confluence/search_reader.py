"""
ConfluenceSearchHitsReader: REST search-JSON → Section[str] на каждый hit.

Каждый hit становится отдельной `Section[str]`:
    - `content` — excerpt-плейнтекст из `body.view.value` (обрезан до `snippet_chars`)
    - `source_id` — stable viewpage URL найденной страницы
    - `metadata`  — `PAGE_ID`, `PAGE_TITLE`, `SPACE_KEY`, `LAST_MODIFIED`

Reader заточен под shape `/rest/api/content/search` — для shape отдельной
страницы (`/rest/api/content/{id}`) используется `ConfluenceJsonDecoder` +
`ConfluenceReader`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, ClassVar

from boba.indexing import (
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
    SourceId,
)
from boba.tool.confluence.errors import ConfluencePayloadError
from boba.tool.confluence.keys import ConfluenceKeys
from boba.tool.confluence.parse import parse_html, plain_text
from boba.tool.confluence.request_sources._common import viewpage_url
from boba.transport.http import HttpKeys

__all__ = ["ConfluenceSearchHitsReader"]


class ConfluenceSearchHitsReader(Reader[str]):
    """Search-JSON → `Section[str]` на каждый hit."""

    DOC_TYPE: ClassVar[str] = "confluence_search_hit"
    READER_ID: ClassVar[ReaderId] = ReaderId("ext.confluence_search_hits")

    def __init__(self, *, base_url: str, snippet_chars: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._snippet_chars = snippet_chars

    def name(self) -> str:
        return f"ConfluenceSearchHitsReader(snippet={self._snippet_chars})"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        payload = value.handle.read()
        if not payload:
            return
        try:
            data: dict[str, Any] = json.loads(payload)
        except json.JSONDecodeError as e:
            raise ConfluencePayloadError(
                f"ConfluenceSearchHitsReader: невалидный JSON от Confluence search: {e}"
            ) from e
        for order, hit in enumerate(data.get("results") or []):
            yield self._make_section(value, hit, order)

    def _make_section(
        self,
        value: RawDocument,
        hit: dict[str, Any],
        order: int,
    ) -> Section[str]:
        page_id = str(hit["id"])
        title = str(hit.get("title") or "")
        space_key = ""
        space = hit.get("space")
        if isinstance(space, dict):
            space_key = str(space.get("key") or "")
        last_modified = ""
        version = hit.get("version")
        if isinstance(version, dict):
            last_modified = str(version.get("when") or "")
        excerpt = self._make_excerpt(hit)
        url = viewpage_url(self._base_url, page_id)

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

        return Section(
            source_id=SourceId(url),
            content=excerpt,
            order=order,
            metadata=meta,
        )

    def _make_excerpt(self, hit: dict[str, Any]) -> str:
        body = hit.get("body") or {}
        view = body.get("view") if isinstance(body, dict) else None
        html = ""
        if isinstance(view, dict):
            html = str(view.get("value") or "")
        if not html:
            return ""
        text = plain_text(parse_html(html)).strip()
        if len(text) <= self._snippet_chars:
            return text
        return text[: self._snippet_chars - 1].rstrip() + "…"
