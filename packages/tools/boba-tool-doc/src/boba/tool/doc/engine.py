"""Вызов doc-узлов: сборка вырожденного графа, сбор продукта и квитанции.

Ошибки:
PayloadFailureError — документ не читается (ожидаемый отказ стадии).
LauncherError и WorkflowError — запуск сорвался либо итог не по контракту.
CollectorCapacityError/CollectorRowLimitError — продукт стадии превысил потолок
    вызывающего; pydantic.ValidationError — строка выдачи не по контракту.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from enum import StrEnum
from typing import ClassVar

from pydantic import JsonValue

from boba.tool.doc.config import DocToolsConfig
from boba.tool.doc.protocol import (
    DocOp,
    DocOutlineAnswer,
    DocOutlineRow,
    DocOutlineTrailer,
    DocPagesAnswer,
    DocPagesRequest,
    DocPagesTrailer,
    DocPathRequest,
    DocReadSettings,
    DocSearchAnswer,
    DocSearchRequest,
    DocSearchRow,
    DocSearchSettings,
    DocSearchTrailer,
    DocSettings,
)
from boba.toolkit.channels import ChannelSink, StreamFormat
from boba.toolkit.launcher import (
    LauncherFactory,
    RowCollector,
    StageRun,
    TextCollector,
)
from boba.toolkit.workflow import (
    StageArgsEnricher,
    StageContract,
    StageNode,
    WorkflowOutcome,
)

__all__ = ["DocEngine"]


class DocArgs(StrEnum):
    """Имена аргументов doc-узлов: их присылает LLM, их же кладёт фасад."""

    PATH = "path"
    PAGES = "pages"
    QUERY = "query"
    OCR_ENABLED = "ocr_enabled"
    NUM_WORKERS = "num_workers"
    OCR_LANGUAGE = "ocr_language"


class DocEngine:
    """Три операции doc-инструментов, каждая — вырожденный граф из одного узла."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.doc.payload")

    def __init__(
        self,
        cfg: DocToolsConfig,
        launchers: LauncherFactory,
    ) -> None:
        self._cfg = cfg
        self._run = StageRun(launchers("doc"))

    @classmethod
    def stages(cls, cfg: DocToolsConfig) -> Mapping[str, StageNode]:
        """Узлы пакета для реестра стадий; профиль подставляет приложение."""
        return {
            DocOp.READ: cls._read_node(cfg),
            DocOp.OUTLINE: cls._outline_node(cfg),
            DocOp.SEARCH: cls._search_node(cfg),
        }

    async def read_document(
        self,
        path: str,
        pages: str,
        ocr_enabled: bool,
        num_workers: int,
        ocr_language: str,
    ) -> DocPagesAnswer:
        args = self._common_args(path, ocr_enabled, num_workers, ocr_language)
        args[DocArgs.PAGES] = pages

        collector = TextCollector(
            max_chars=self._cfg.max_text_chars,
            limit_rows=None,
            header_lines=0,
        )

        outcome = await self._call(DocOp.READ, args, collector)
        trailer = outcome.trailer(DocOp.READ, DocPagesTrailer)

        return DocPagesAnswer(
            text=collector.text(),
            truncated=trailer.truncated,
            pages=trailer.pages,
        )

    async def outline(
        self,
        path: str,
        ocr_enabled: bool,
        num_workers: int,
        ocr_language: str,
    ) -> DocOutlineAnswer:
        args = self._common_args(path, ocr_enabled, num_workers, ocr_language)

        collector = self._row_collector(None)

        outcome = await self._call(DocOp.OUTLINE, args, collector)
        trailer = outcome.trailer(DocOp.OUTLINE, DocOutlineTrailer)

        rows: list[DocOutlineRow] = []
        for raw in collector.rows():
            rows.append(DocOutlineRow.model_validate(raw))

        return DocOutlineAnswer(num_pages=trailer.num_pages, rows=tuple(rows))

    async def search(
        self,
        path: str,
        query: str,
        ocr_enabled: bool,
        num_workers: int,
        ocr_language: str,
    ) -> DocSearchAnswer:
        args = self._common_args(path, ocr_enabled, num_workers, ocr_language)
        args[DocArgs.QUERY] = query

        collector = self._row_collector(self._cfg.search_max_matches)

        outcome = await self._call(DocOp.SEARCH, args, collector)
        trailer = outcome.trailer(DocOp.SEARCH, DocSearchTrailer)

        rows: list[DocSearchRow] = []
        for raw in collector.rows():
            rows.append(DocSearchRow.model_validate(raw))

        return DocSearchAnswer(rows=tuple(rows), limit_reached=trailer.limit_reached)

    @staticmethod
    def _common_args(
        path: str,
        ocr_enabled: bool,
        num_workers: int,
        ocr_language: str,
    ) -> dict[str, JsonValue]:
        """Аргументы, общие для всех doc-узлов; остальное кладёт обогатитель."""
        return {
            DocArgs.PATH: path,
            DocArgs.OCR_ENABLED: ocr_enabled,
            DocArgs.NUM_WORKERS: num_workers,
            DocArgs.OCR_LANGUAGE: ocr_language,
        }

    async def _call(
        self,
        node: DocOp,
        args: Mapping[str, JsonValue],
        sink: ChannelSink,
    ) -> WorkflowOutcome:
        """Парсинг долгий и синхронный — уходит в отдельный поток."""
        return await asyncio.to_thread(
            self._run.call, node.value, dict(args), sink=sink
        )

    def _row_collector(self, limit_rows: int | None) -> RowCollector:
        return RowCollector(max_chars=self._cfg.max_result_chars, limit_rows=limit_rows)

    @classmethod
    def _read_node(cls, cfg: DocToolsConfig) -> StageNode:
        settings = DocReadSettings(
            op=DocOp.READ,
            max_pages=cfg.max_pages,
            tessdata_path=cfg.tessdata_path,
            max_text_chars=cfg.max_text_chars,
        )
        contract = StageContract(
            out=StreamFormat.TEXT,
            result=DocPagesTrailer,
        )

        return StageNode(
            contract=contract,
            entry=cls.ENTRY,
            request=DocPagesRequest,
            enrich=StageArgsEnricher(settings),
        )

    @classmethod
    def _outline_node(cls, cfg: DocToolsConfig) -> StageNode:
        settings = DocSettings(
            op=DocOp.OUTLINE,
            max_pages=cfg.max_pages,
            tessdata_path=cfg.tessdata_path,
        )
        contract = StageContract(
            out=StreamFormat.NDJSON,
            result=DocOutlineTrailer,
        )

        return StageNode(
            contract=contract,
            entry=cls.ENTRY,
            request=DocPathRequest,
            enrich=StageArgsEnricher(settings),
        )

    @classmethod
    def _search_node(cls, cfg: DocToolsConfig) -> StageNode:
        settings = DocSearchSettings(
            op=DocOp.SEARCH,
            max_pages=cfg.max_pages,
            tessdata_path=cfg.tessdata_path,
            context_chars=cfg.search_context_chars,
            max_matches=cfg.search_max_matches,
        )
        contract = StageContract(
            out=StreamFormat.NDJSON,
            result=DocSearchTrailer,
        )

        return StageNode(
            contract=contract,
            entry=cls.ENTRY,
            request=DocSearchRequest,
            enrich=StageArgsEnricher(settings),
        )
