"""Вызов doc-payload'а в песочнице: сборка запроса и разбор ответа."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
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
from boba.toolkit.sandbox import SandboxCaller

__all__ = ["DocEngine"]

M = TypeVar("M", bound=BaseModel)


class DocEngine:
    """Четыре операции doc-инструментов, каждая — один запуск payload'а."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.doc.payload")

    def __init__(
        self,
        cfg: DocToolsConfig,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._cfg = cfg
        self._caller = SandboxCaller("doc", cfg.sandbox.effective(), path_vars)

    async def read_document(
        self, path: str, pages: str, ocr_enabled: bool, num_workers: int
    ) -> DocPagesAnswer:
        request = DocPagesRequest(
            op=DocPagesRequest.OP,
            path=path,
            pages=pages,
            params=self._params(ocr_enabled, num_workers),
        )
        return await self._call(request, DocPagesAnswer)

    async def read_window(
        self,
        path: str,
        start_char: int,
        length: int,
        ocr_enabled: bool,
        num_workers: int,
    ) -> DocWindowAnswer:
        request = DocWindowRequest(
            op=DocWindowRequest.OP,
            path=path,
            start_char=start_char,
            length=length,
            params=self._params(ocr_enabled, num_workers),
        )
        return await self._call(request, DocWindowAnswer)

    async def outline(
        self, path: str, ocr_enabled: bool, num_workers: int
    ) -> DocOutlineAnswer:
        request = DocPathRequest(
            op=DocPathRequest.OUTLINE,
            path=path,
            params=self._params(ocr_enabled, num_workers),
        )
        return await self._call(request, DocOutlineAnswer)

    async def search(
        self, path: str, query: str, ocr_enabled: bool, num_workers: int
    ) -> DocSearchAnswer:
        request = DocSearchRequest(
            op=DocSearchRequest.OP,
            path=path,
            query=query,
            context_chars=self._cfg.search_context_chars,
            max_matches=self._cfg.search_max_matches,
            params=self._params(ocr_enabled, num_workers),
        )
        return await self._call(request, DocSearchAnswer)

    def _params(self, ocr_enabled: bool, num_workers: int) -> DocParams:
        base = self._cfg.parse_params().model_dump()
        base["ocr_enabled"] = ocr_enabled
        base["num_workers"] = num_workers
        return DocParams(**base, max_text_chars=self._cfg.max_text_chars)

    async def _call(self, request: BaseModel, schema: type[M]) -> M:
        """Парсинг долгий и синхронный — уходит в отдельный поток."""
        return await asyncio.to_thread(
            self._caller.call_json, self.ENTRY, request, schema
        )
