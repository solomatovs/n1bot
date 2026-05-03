"""HtmlReader: HTML → одна Section (full-text)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from bs4 import BeautifulSoup

from boba.indexing import (
    IndexingContext,
    Reader,
    ReaderId,
    Section,
    SourceItem,
)

__all__ = ["HtmlReader"]


_HTML_HINTS = frozenset({"html", "htm", "xhtml"})
_WS = re.compile(r"\s+")
_BLANK_LINES = re.compile(r"\n\s*\n+")


class HtmlReader(Reader):
    """HTML → plain text Section. Drops <script>/<style> и Confluence-макросы."""

    def name(self) -> str:
        return "HtmlReader"

    def reader_id(self) -> ReaderId:
        return ReaderId("ext.html")

    def accepts(self, item: SourceItem) -> bool:
        return item.content_hint in _HTML_HINTS

    def convert(
        self, ctx: IndexingContext, value: SourceItem
    ) -> Iterable[Section]:
        del ctx
        raw = value.payload.decode("utf-8", errors="replace")
        soup = BeautifulSoup(raw, "lxml")

        for tag in soup(["script", "style"]):
            tag.decompose()
        for tag in soup.find_all(re.compile(r"^(ac:|ri:)")):
            tag.decompose()

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""

        text = soup.get_text(" ")
        text = _WS.sub(" ", text)
        text = _BLANK_LINES.sub("\n\n", text).strip()
        if not text:
            return

        metadata: dict[str, str] = {**value.metadata, "format": "html"}
        if title:
            metadata["title"] = title

        yield Section(
            source_id=value.source_id,
            text=text,
            content_hash=value.content_hash,
            metadata=metadata,
        )
