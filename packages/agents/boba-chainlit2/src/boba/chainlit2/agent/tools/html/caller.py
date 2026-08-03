"""Разбор HTML внутри песочницы: markdown, текст и секции страницы."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import ClassVar

from boba.chainlit2.agent.tools.html.protocol import (
    ConfluenceSectionsAnswer,
    ConfluenceSectionsRequest,
    HtmlToMarkdownAnswer,
    HtmlToMarkdownRequest,
    PlainTextAnswer,
    PlainTextRequest,
)
from boba.chainlit2.sandbox import SandboxCaller, SandboxEntryConfig

__all__ = ["HtmlCaller"]


class HtmlCaller:
    """Один вызов payload'а на страницу; HTML едет в запросе."""

    HEADING_STYLE: ClassVar[str] = "ATX"

    def __init__(
        self,
        tool: str,
        sandbox: SandboxEntryConfig,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._entry = sandbox.entry
        self._caller = SandboxCaller(tool, sandbox.effective(), path_vars)

    def to_markdown(self, html: str) -> str:
        request = HtmlToMarkdownRequest.of(html, self.HEADING_STYLE)
        answer = self._caller.call_json(self._entry, request, HtmlToMarkdownAnswer)
        return answer.markdown

    def plain_text(self, html: str) -> str:
        request = PlainTextRequest.of(html)
        answer = self._caller.call_json(self._entry, request, PlainTextAnswer)
        return answer.text

    def confluence_sections(
        self, html: str, title: str
    ) -> ConfluenceSectionsAnswer:
        request = ConfluenceSectionsRequest.of(html, title)
        return self._caller.call_json(self._entry, request, ConfluenceSectionsAnswer)
