"""Вызовы узлов Confluence: сеть и разбор идут внутри песочницы."""

from __future__ import annotations

from typing import ClassVar

from pydantic import JsonValue

from boba.tool.kb.confluence.protocol import (
    ConfluenceGrepRow,
    ConfluenceNode,
    ConfluencePageTrailer,
    ConfluenceSearchHit,
    ConfluenceSpace,
)
from boba.toolkit.launcher import (
    LauncherFactory,
    RowCollector,
    StageRun,
    TextCollector,
)

__all__ = ["ConfluenceCaller", "ConfluencePage"]


class ConfluencePage:
    """Материализованная страница: контент из потока плюс заголовок квитанции."""

    def __init__(self, text: str, title: str) -> None:
        self.text = text
        self.title = title


class ConfluenceCaller:
    """Один запуск узла на операцию; профиль соединения добавляет реестр."""

    MAX_RESULT_CHARS: ClassVar[int] = 10_000_000
    """Транспортный потолок объёма потока; выдачу режут limit'ы самих операций."""

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._run = StageRun(launchers(tool))

    def page(self, *, page_id: str, as_markdown: bool) -> ConfluencePage:
        collector = self._text_collector()
        args: dict[str, JsonValue] = {
            "page_id": page_id,
            "as_markdown": as_markdown,
        }

        trailer = self._run.trailer(
            ConfluenceNode.PAGE.value,
            args,
            ConfluencePageTrailer,
            sink=collector,
        )

        return ConfluencePage(text=collector.text(), title=trailer.title)

    def grep(  # noqa: PLR0913 — флаги grep'а независимы
        self,
        *,
        page_id: str,
        pattern: str,
        as_markdown: bool,
        case_insensitive: bool,
        context: int,
        limit: int,
        fixed_string: bool,
    ) -> list[ConfluenceGrepRow]:
        collector = RowCollector(max_chars=self.MAX_RESULT_CHARS, limit_rows=limit)
        args: dict[str, JsonValue] = {
            "page_id": page_id,
            "pattern": pattern,
            "as_markdown": as_markdown,
            "case_insensitive": case_insensitive,
            "context": context,
            "limit": limit,
            "fixed_string": fixed_string,
        }

        self._run.call(ConfluenceNode.GREP.value, args, sink=collector)

        rows: list[ConfluenceGrepRow] = []
        for raw in collector.rows():
            rows.append(ConfluenceGrepRow.model_validate(raw))

        return rows

    def search(
        self,
        *,
        cql: str,
        limit: int,
        snippet_chars: int,
    ) -> list[ConfluenceSearchHit]:
        collector = RowCollector(max_chars=self.MAX_RESULT_CHARS, limit_rows=limit)
        args: dict[str, JsonValue] = {
            "cql": cql,
            "limit": limit,
            "snippet_chars": snippet_chars,
        }

        self._run.call(ConfluenceNode.SEARCH.value, args, sink=collector)

        hits: list[ConfluenceSearchHit] = []
        for raw in collector.rows():
            hits.append(ConfluenceSearchHit.model_validate(raw))

        return hits

    def spaces(self, *, space_type: str) -> list[ConfluenceSpace]:
        """Сколько спейсов запросить у REST, решает конфиг узла, а не вызов."""
        collector = RowCollector(max_chars=self.MAX_RESULT_CHARS, limit_rows=None)
        args: dict[str, JsonValue] = {"space_type": space_type}

        self._run.call(ConfluenceNode.SPACES.value, args, sink=collector)

        spaces: list[ConfluenceSpace] = []
        for raw in collector.rows():
            spaces.append(ConfluenceSpace.model_validate(raw))

        return spaces

    def attachment(
        self,
        *,
        page_id: str,
        filename: str,
        ocr_enabled: bool,
        num_workers: int,
        ocr_language: str,
    ) -> str:
        collector = self._text_collector()
        args: dict[str, JsonValue] = {
            "page_id": page_id,
            "filename": filename,
            "ocr_enabled": ocr_enabled,
            "num_workers": num_workers,
            "ocr_language": ocr_language,
        }

        self._run.call(ConfluenceNode.ATTACHMENT.value, args, sink=collector)

        return collector.text()

    def _text_collector(self) -> TextCollector:
        return TextCollector(
            max_chars=self.MAX_RESULT_CHARS,
            limit_rows=None,
            header_lines=0,
        )
