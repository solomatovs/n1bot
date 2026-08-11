"""Разбор HTML внутри песочницы: markdown, текст и секции страницы."""

from __future__ import annotations

from typing import ClassVar

from boba.tool.kb.html.protocol import (
    ConfluenceSection,
    ConfluenceSectionsAnswer,
    HtmlNode,
)

from boba.toolkit.launcher import (
    LauncherFactory,
    RowCollector,
    StageRun,
    TextCollector,
)

__all__ = ["HtmlCaller"]


class HtmlCaller:
    """Один запуск узла на страницу; разметка едет каналом входа."""

    MAX_RESULT_CHARS: ClassVar[int] = 10_000_000

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._run = StageRun(launchers(tool))

    def to_markdown(self, html: str) -> str:
        collector = self._text_collector()

        self._run.call(HtmlNode.MARKDOWN.value, {}, sink=collector, stdin=html)

        return collector.text()

    def plain_text(self, html: str) -> str:
        collector = self._text_collector()

        self._run.call(HtmlNode.PLAIN_TEXT.value, {}, sink=collector, stdin=html)

        return collector.text()

    def confluence_sections(self, html: str, title: str) -> ConfluenceSectionsAnswer:
        collector = RowCollector(max_chars=self.MAX_RESULT_CHARS, limit_rows=None)

        self._run.call(
            HtmlNode.SECTIONS.value,
            {"title": title},
            sink=collector,
            stdin=html,
        )

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
