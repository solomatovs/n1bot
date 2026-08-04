"""Вызов doc-payload'а в песочнице: сборка запроса и разбор ответа.

Ошибки: SandboxPayloadError — сбой запуска или исполнения payload'а;
pydantic.ValidationError — ответ payload'а не по контракту.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar, TypeVar

from pydantic import BaseModel

from boba.tool.doc.config import DocToolsConfig
from boba.tool.doc.protocol import (
    DocOutlineAnswer,
    DocPagesAnswer,
    DocPagesRequest,
    DocParams,
    DocPathRequest,
    DocSearchAnswer,
    DocSearchRequest,
    DocWindowAnswer,
    DocWindowRequest,
)
from boba.toolkit.launcher import LauncherFactory

__all__ = ["DocEngine"]

M = TypeVar("M", bound=BaseModel)


class DocEngine:
    """Четыре операции doc-инструментов, каждая — один запуск payload'а."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.doc.payload")

    def __init__(
        self,
        cfg: DocToolsConfig,
        launchers: LauncherFactory,
    ) -> None:
        self._cfg = cfg
        self._caller = launchers("doc")

    async def read_document(
        self,
        path: str,
        pages: str,
        ocr_enabled: bool,
        num_workers: int,
        ocr_language: str,
    ) -> DocPagesAnswer:
        request = DocPagesRequest(
            op=DocPagesRequest.OP,
            path=path,
            pages=pages,
            params=self._params(ocr_enabled, num_workers, ocr_language),
        )
        return await self._call(request, DocPagesAnswer)

    async def read_window(
        self,
        path: str,
        start_char: int,
        length: int,
        ocr_enabled: bool,
        num_workers: int,
        ocr_language: str,
    ) -> DocWindowAnswer:
        request = DocWindowRequest(
            op=DocWindowRequest.OP,
            path=path,
            start_char=start_char,
            length=length,
            params=self._params(ocr_enabled, num_workers, ocr_language),
        )
        return await self._call(request, DocWindowAnswer)

    async def outline(
        self,
        path: str,
        ocr_enabled: bool,
        num_workers: int,
        ocr_language: str,
    ) -> DocOutlineAnswer:
        request = DocPathRequest(
            op=DocPathRequest.OUTLINE,
            path=path,
            params=self._params(ocr_enabled, num_workers, ocr_language),
        )
        return await self._call(request, DocOutlineAnswer)

    async def search(
        self,
        path: str,
        query: str,
        ocr_enabled: bool,
        num_workers: int,
        ocr_language: str,
    ) -> DocSearchAnswer:
        request = DocSearchRequest(
            op=DocSearchRequest.OP,
            path=path,
            query=query,
            context_chars=self._cfg.search_context_chars,
            max_matches=self._cfg.search_max_matches,
            params=self._params(ocr_enabled, num_workers, ocr_language),
        )
        return await self._call(request, DocSearchAnswer)

    def _params(
        self, ocr_enabled: bool, num_workers: int, ocr_language: str
    ) -> DocParams:
        """Настройки конфига плюс переопределения из вызова LLM — явно по полям."""
        return DocParams(
            ocr_enabled=ocr_enabled,
            ocr_language=ocr_language,
            max_pages=self._cfg.max_pages,
            tessdata_path=self._cfg.tessdata_path,
            num_workers=num_workers,
            max_text_chars=self._cfg.max_text_chars,
        )

    async def _call(self, request: BaseModel, schema: type[M]) -> M:
        """Парсинг долгий и синхронный — уходит в отдельный поток."""
        return await asyncio.to_thread(
            self._caller.call_json, self.ENTRY, request, schema
        )
