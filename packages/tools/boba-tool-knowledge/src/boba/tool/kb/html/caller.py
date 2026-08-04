"""Разбор HTML внутри песочницы: markdown, текст и секции страницы."""

from __future__ import annotations

from typing import ClassVar

from boba.tool.kb.html.protocol import (
    ConfluenceSectionsAnswer,
    ConfluenceSectionsRequest,
    HtmlToMarkdownAnswer,
    HtmlToMarkdownRequest,
    PlainTextAnswer,
    PlainTextRequest,
)
from boba.toolkit.launcher import LauncherFactory

__all__ = ["HtmlCaller"]


class HtmlCaller:
    """Один вызов payload'а на страницу; HTML едет в запросе."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.kb.html.payload")

    HEADING_STYLE: ClassVar[str] = "ATX"

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._caller = launchers(tool)

    def to_markdown(self, html: str) -> str:
        request = HtmlToMarkdownRequest.of(html, self.HEADING_STYLE)
        answer = self._caller.call_json(self.ENTRY, request, HtmlToMarkdownAnswer)
        return answer.markdown

    def plain_text(self, html: str) -> str:
        request = PlainTextRequest.of(html)
        answer = self._caller.call_json(self.ENTRY, request, PlainTextAnswer)
        return answer.text

    def confluence_sections(
        self, html: str, title: str
    ) -> ConfluenceSectionsAnswer:
        request = ConfluenceSectionsRequest.of(html, title)
        return self._caller.call_json(self.ENTRY, request, ConfluenceSectionsAnswer)
