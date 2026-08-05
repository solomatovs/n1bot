"""Разбор HTML внутри песочницы: markdown, текст и секции страницы."""

from __future__ import annotations

from typing import ClassVar

from boba.tool.kb.html.protocol import (
    ConfluenceSection,
    ConfluenceSectionsAnswer,
    ConfluenceSectionsRequest,
    HtmlToMarkdownRequest,
    PlainTextRequest,
)
from boba.toolkit.launcher import (
    EmptyTrailer,
    LauncherFactory,
    RowCollector,
    TextCollector,
)

__all__ = ["HtmlCaller"]


class HtmlCaller:
    """Один вызов payload'а на страницу; HTML едет в запросе."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.kb.html.payload")

    HEADING_STYLE: ClassVar[str] = "ATX"
    MAX_RESULT_CHARS: ClassVar[int] = 10_000_000

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._caller = launchers(tool)

    def to_markdown(self, html: str) -> str:
        request = HtmlToMarkdownRequest.of(html, self.HEADING_STYLE)
        collector = self._text_collector()
        self._caller.call_stream(self.ENTRY, request, collector, EmptyTrailer)
        return collector.text()

    def plain_text(self, html: str) -> str:
        request = PlainTextRequest.of(html)
        collector = self._text_collector()
        self._caller.call_stream(self.ENTRY, request, collector, EmptyTrailer)
        return collector.text()

    def confluence_sections(self, html: str, title: str) -> ConfluenceSectionsAnswer:
        request = ConfluenceSectionsRequest.of(html, title)
        collector = RowCollector(max_chars=self.MAX_RESULT_CHARS, limit_rows=None)
        self._caller.call_stream(self.ENTRY, request, collector, EmptyTrailer)
        sections: list[ConfluenceSection] = []
        for raw in collector.rows():
            sections.append(ConfluenceSection.model_validate(raw))
        return ConfluenceSectionsAnswer(sections=tuple(sections))

    def _text_collector(self) -> TextCollector:
        return TextCollector(
            max_chars=self.MAX_RESULT_CHARS,
            limit_rows=None,
            header_lines=0,
        )
