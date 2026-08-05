"""Вызовы confluence-payload'а: сеть и разбор идут внутри песочницы."""

from __future__ import annotations

from typing import ClassVar

from boba.tool.kb.confluence.protocol import (
    ConfluenceAttachmentRequest,
    ConfluenceGrepRequest,
    ConfluenceGrepRow,
    ConfluencePageRequest,
    ConfluencePageTrailer,
    ConfluenceSearchHit,
    ConfluenceSearchRequest,
    ConfluenceSpace,
    ConfluenceSpacesRequest,
)
from boba.toolkit.launcher import (
    EmptyTrailer,
    LauncherFactory,
    RowCollector,
    TextCollector,
)
from boba.transport.http import HttpProfile
from boba.web.caller import WebCaller
from boba.web.protocol import WebProfile

__all__ = ["ConfluenceCaller", "ConfluencePage"]


class ConfluencePage:
    """Материализованная страница: контент из потока плюс заголовок трейлера."""

    def __init__(self, text: str, title: str) -> None:
        self.text = text
        self.title = title


class ConfluenceCaller:
    """Один вызов payload'а на операцию; профиль соединения едет с ним."""

    ENTRY: ClassVar[tuple[str, ...]] = (
        "python3",
        "-m",
        "boba.tool.kb.confluence.payload",
    )

    MAX_RESULT_CHARS: ClassVar[int] = 10_000_000
    """Транспортный потолок объёма потока; выдачу режут limit'ы самих операций."""

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._caller = launchers(tool)

    @staticmethod
    def transport_of(profile: HttpProfile) -> WebProfile:
        return WebCaller.transport_of(profile)

    def page(self, request: ConfluencePageRequest) -> ConfluencePage:
        collector = self._text_collector()
        trailer = self._caller.call_stream(
            self.ENTRY, request, collector, ConfluencePageTrailer
        )
        return ConfluencePage(text=collector.text(), title=trailer.title)

    def grep(self, request: ConfluenceGrepRequest) -> list[ConfluenceGrepRow]:
        collector = self._row_collector(request.limit)
        self._caller.call_stream(self.ENTRY, request, collector, EmptyTrailer)
        rows: list[ConfluenceGrepRow] = []
        for raw in collector.rows():
            rows.append(ConfluenceGrepRow.model_validate(raw))
        return rows

    def search(self, request: ConfluenceSearchRequest) -> list[ConfluenceSearchHit]:
        collector = self._row_collector(request.limit)
        self._caller.call_stream(self.ENTRY, request, collector, EmptyTrailer)
        hits: list[ConfluenceSearchHit] = []
        for raw in collector.rows():
            hits.append(ConfluenceSearchHit.model_validate(raw))
        return hits

    def spaces(self, request: ConfluenceSpacesRequest) -> list[ConfluenceSpace]:
        collector = self._row_collector(request.limit)
        self._caller.call_stream(self.ENTRY, request, collector, EmptyTrailer)
        spaces: list[ConfluenceSpace] = []
        for raw in collector.rows():
            spaces.append(ConfluenceSpace.model_validate(raw))
        return spaces

    def attachment(self, request: ConfluenceAttachmentRequest) -> str:
        collector = self._text_collector()
        self._caller.call_stream(self.ENTRY, request, collector, EmptyTrailer)
        return collector.text()

    def _text_collector(self) -> TextCollector:
        return TextCollector(
            max_chars=self.MAX_RESULT_CHARS,
            limit_rows=None,
            header_lines=0,
        )

    def _row_collector(self, limit_rows: int) -> RowCollector:
        return RowCollector(max_chars=self.MAX_RESULT_CHARS, limit_rows=limit_rows)
