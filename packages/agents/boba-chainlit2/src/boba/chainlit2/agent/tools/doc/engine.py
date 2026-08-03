"""Вызов doc-payload'а в песочнице: сборка запроса и разбор ответа.

Сам документ приложение не открывает: файл читает payload по пути внутри
песочницы, где workspace пользователя уже смонтирован профилем.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import TypeVar

from pydantic import BaseModel

from boba.chainlit2.agent.tools.doc.config import DocToolsConfig
from boba.chainlit2.agent.tools.doc.protocol import (
    DocOutlineAnswer,
    DocPagesAnswer,
    DocPagesRequest,
    DocParams,
    DocPathRequest,
    DocSearchAnswer,
    DocSearchRequest,
    DocTextAnswer,
    DocWindowAnswer,
    DocWindowRequest,
)
from boba.chainlit2.sandbox import SandboxCaller

__all__ = ["DocEngine"]

M = TypeVar("M", bound=BaseModel)


class DocEngine:
    """Пять операций doc-инструментов, каждая — один запуск payload'а."""

    def __init__(
        self,
        cfg: DocToolsConfig,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._cfg = cfg
        self._entry = cfg.sandbox.entry
        self._caller = SandboxCaller("doc", cfg.sandbox.effective(), path_vars)

    async def read_document(self, path: str) -> DocTextAnswer:
        request = DocPathRequest(
            op=DocPathRequest.READ, path=path, params=self._params()
        )
        return await self._call(request, DocTextAnswer)

    async def read_pages(self, path: str, pages: str) -> DocPagesAnswer:
        request = DocPagesRequest(
            op=DocPagesRequest.OP, path=path, pages=pages, params=self._params()
        )
        return await self._call(request, DocPagesAnswer)

    async def read_window(
        self, path: str, start_char: int, length: int
    ) -> DocWindowAnswer:
        request = DocWindowRequest(
            op=DocWindowRequest.OP,
            path=path,
            start_char=start_char,
            length=length,
            params=self._params(),
        )
        return await self._call(request, DocWindowAnswer)

    async def outline(self, path: str) -> DocOutlineAnswer:
        request = DocPathRequest(
            op=DocPathRequest.OUTLINE, path=path, params=self._params()
        )
        return await self._call(request, DocOutlineAnswer)

    async def search(self, path: str, query: str) -> DocSearchAnswer:
        request = DocSearchRequest(
            op=DocSearchRequest.OP,
            path=path,
            query=query,
            context_chars=self._cfg.search_context_chars,
            max_matches=self._cfg.search_max_matches,
            params=self._params(),
        )
        return await self._call(request, DocSearchAnswer)

    def _params(self) -> DocParams:
        base = self._cfg.parse_params()
        return DocParams(
            **base.model_dump(), max_text_chars=self._cfg.max_text_chars
        )

    async def _call(self, request: BaseModel, schema: type[M]) -> M:
        """Парсинг долгий и синхронный — уходит в отдельный поток."""
        return await asyncio.to_thread(
            self._caller.call_json, self._entry, request, schema
        )
