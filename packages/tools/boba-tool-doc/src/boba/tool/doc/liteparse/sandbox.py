"""Исполнение liteparse в песочнице: конфиг, узел parse_bytes, ридер индексации.

Ошибки: LauncherError и WorkflowError — сбой запуска или итог не по контракту;
PayloadFailureError — документ не разобрать; IncompatibleContentError — из
SandboxLiteParseReader (база PagedDocumentReader); pydantic.ValidationError —
при разборе конфига и строк выдачи.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from pydantic import ConfigDict

from boba.text.document import LiteParseParams, PagedDocumentReader, ParsedPage
from boba.tool.doc.liteparse.protocol import (
    ParseBytesAnswer,
    ParseBytesArgs,
    ParseBytesRequest,
    ParseBytesTrailer,
    ParseParams,
    ParseRequest,
)
from boba.toolkit.channels import StreamFormat
from boba.toolkit.launcher import (
    LauncherError,
    LauncherFactory,
    RowCollector,
    StageRun,
)
from boba.toolkit.workflow import (
    StageArgsEnricher,
    StageContract,
    StageNode,
    WorkflowError,
)

__all__ = [
    "LiteParseCaller",
    "SandboxLiteParseReader",
    "SandboxParserConfig",
]


class SandboxParserConfig(LiteParseParams):
    """Настройки парсера плюс песочница, в которой он исполняется."""

    model_config = ConfigDict(extra="ignore")

    def parse_params(self) -> ParseParams:
        """Настройки движка liteparse из полей конфига."""
        return ParseParams(
            ocr_enabled=self.ocr_enabled,
            ocr_language=self.ocr_language,
            max_pages=self.max_pages,
            tessdata_path=self.tessdata_path,
            num_workers=self.num_workers,
        )


class LiteParseCaller:
    """Один вызов узла parse_bytes на документ; содержимое едет в аргументах."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.doc.payload")

    MAX_RESULT_CHARS: ClassVar[int] = 50_000_000
    """Транспортный потолок объёма потока страниц; сам объём режет max_pages."""

    def __init__(
        self,
        tool: str,
        cfg: SandboxParserConfig,
        launchers: LauncherFactory,
    ) -> None:
        self._node = self.node_of(tool)
        self._run = StageRun(launchers(tool))

    @classmethod
    def node_of(cls, tool: str) -> str:
        """Имя узла парсера у инструмента: у каждого свой профиль и настройки."""
        return f"{tool}_{ParseBytesRequest.OP}"

    @classmethod
    def stages(
        cls, tool: str, cfg: SandboxParserConfig
    ) -> Mapping[str, StageNode]:
        """Узел парсера инструмента; профиль подставляет приложение."""
        settings = ParseRequest.settings_of(ParseBytesRequest.OP, cfg)
        contract = StageContract(
            accepts=frozenset(),
            out=StreamFormat.NDJSON,
            result=ParseBytesTrailer,
        )

        node = StageNode(
            contract=contract,
            entry=cls.ENTRY,
            request=ParseBytesRequest,
            enrich=StageArgsEnricher(settings),
        )

        return {cls.node_of(tool): node}

    def parse_bytes(self, data: bytes, filename: str) -> ParseBytesAnswer:
        args = ParseBytesArgs.of(data, filename)

        collector = RowCollector(max_chars=self.MAX_RESULT_CHARS, limit_rows=None)

        trailer = self._run.trailer(
            self._node,
            args.model_dump(mode="json"),
            ParseBytesTrailer,
            sink=collector,
        )

        pages: list[ParsedPage] = []
        for raw in collector.rows():
            pages.append(ParsedPage.model_validate(raw))

        return ParseBytesAnswer(num_pages=trailer.num_pages, pages=tuple(pages))


class SandboxLiteParseReader(PagedDocumentReader):
    """Документ вложения -> Section[str] на страницу; парсит песочница."""

    PARSE_ERRORS: ClassVar[tuple[type[Exception], ...]] = (
        LauncherError,
        WorkflowError,
    )

    def __init__(self, caller: LiteParseCaller) -> None:
        self._caller = caller

    def parse_pages(self, data: bytes, filename: str) -> Sequence[ParsedPage]:
        answer = self._caller.parse_bytes(data, filename)
        return answer.pages
