"""Исполнение liteparse в песочнице: конфиг, вызов payload'а, ридер индексации.

Ошибки: LauncherError — из LiteParseCaller при сбое payload'а;
IncompatibleContentError — из SandboxLiteParseReader (база PagedDocumentReader);
pydantic.ValidationError — при разборе конфига и ответа payload'а.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from pydantic import ConfigDict

from boba.text.document import LiteParseParams, PagedDocumentReader, ParsedPage
from boba.tool.doc.liteparse.protocol import (
    ParseBytesAnswer,
    ParseBytesRequest,
    ParseBytesTrailer,
    ParseParams,
)
from boba.toolkit.launcher import LauncherError, LauncherFactory, RowCollector

__all__ = ["LiteParseCaller", "SandboxLiteParseReader", "SandboxParserConfig"]


class SandboxParserConfig(LiteParseParams):
    """Настройки парсера плюс песочница, в которой он исполняется."""

    model_config = ConfigDict(extra="ignore")

    def parse_params(self) -> ParseParams:
        return ParseParams.of(self)


class LiteParseCaller:
    """Один вызов payload'а на документ; содержимое едет в запросе."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.doc.payload")

    MAX_RESULT_CHARS: ClassVar[int] = 50_000_000
    """Транспортный потолок объёма потока страниц; сам объём режет max_pages."""

    def __init__(
        self,
        tool: str,
        cfg: SandboxParserConfig,
        launchers: LauncherFactory,
    ) -> None:
        self._params = cfg.parse_params()
        self._caller = launchers(tool)

    def parse_bytes(self, data: bytes, filename: str) -> ParseBytesAnswer:
        request = ParseBytesRequest.of(data, filename, self._params)
        collector = RowCollector(max_chars=self.MAX_RESULT_CHARS, limit_rows=None)
        trailer = self._caller.call_stream(
            self.ENTRY, request, collector, ParseBytesTrailer
        )
        pages: list[ParsedPage] = []
        for raw in collector.rows():
            pages.append(ParsedPage.model_validate(raw))
        return ParseBytesAnswer(num_pages=trailer.num_pages, pages=tuple(pages))


class SandboxLiteParseReader(PagedDocumentReader):
    """Документ вложения -> Section[str] на страницу; парсит песочница."""

    PARSE_ERRORS: ClassVar[tuple[type[Exception], ...]] = (LauncherError,)

    def __init__(self, caller: LiteParseCaller) -> None:
        self._caller = caller

    def parse_pages(self, data: bytes, filename: str) -> Sequence[ParsedPage]:
        answer = self._caller.parse_bytes(data, filename)
        return answer.pages
