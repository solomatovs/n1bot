"""Чтение документа из workspace и парсинг liteparse.

Файл лежит в образе пользователя, а LLM видит его как путь внутри
песочницы (/workspace/...), поэтому путь переводится в object_key
хранилища. Парсинг синхронный и тяжёлый — уходит в отдельный поток.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, ClassVar

from boba.chainlit2.agent.tools.doc.config import DocToolsConfig
from boba.chainlit2.agent.tools.sandbox import WORKSPACE_MOUNT
from boba.chainlit2.chat.data.storage import LocalStorageClient
from boba.chainlit2.infra.session import current_user_id
from boba.liteparse import LiteParseEngine, LiteParseError, ParseResult

__all__ = ["DocEngine"]


class DocEngine:
    """Достаёт байты документа по пути песочницы и разбирает его."""

    MOUNT: ClassVar[str] = WORKSPACE_MOUNT

    def __init__(self, cfg: DocToolsConfig) -> None:
        self._cfg = cfg
        self._storage = LocalStorageClient.from_config(cfg.storage)

    async def read_bytes(self, path: str) -> bytes:
        key = self.object_key(path)
        try:
            return await self._storage.read_file(key)
        except FileNotFoundError as e:
            msg = f"file not found: {path}"
            raise RuntimeError(msg) from e

    async def parse(self, path: str, target_pages: str | None = None) -> ParseResult:
        data = await self.read_bytes(path)
        return await asyncio.to_thread(self._parse_sync, data, path, target_pages)

    async def parse_native(self, path: str) -> Any:
        """Нативный результат нужен search_document для bbox-совпадений."""
        data = await self.read_bytes(path)
        return await asyncio.to_thread(self._parse_native_sync, data, path)

    @classmethod
    def object_key(cls, path: str) -> str:
        """Путь песочницы -> ключ хранилища: {user_id}/{thread_id}/upload/...."""
        user_id = current_user_id()
        if not user_id:
            msg = "no chainlit session: cannot resolve the document owner"
            raise RuntimeError(msg)
        rel = path.strip()
        if rel.startswith(cls.MOUNT):
            rel = rel[len(cls.MOUNT) :]
        rel = rel.lstrip("/")
        normalized = os.path.normpath(rel)
        if not normalized or normalized.startswith((".", "/")):
            msg = f"invalid document path: {path!r}"
            raise RuntimeError(msg)
        return f"{user_id}/{normalized}"

    @staticmethod
    def clip(text: str, limit: int) -> tuple[str, bool]:
        if len(text) <= limit:
            return text, False
        return text[:limit], True

    @staticmethod
    def window(text: str, start: int, length: int) -> tuple[str, int, int, bool]:
        total = len(text)
        chunk = text[start : start + length]
        end = start + len(chunk)
        return chunk, end, total, end < total

    def _parse_sync(
        self,
        data: bytes,
        path: str,
        target_pages: str | None,
    ) -> ParseResult:
        try:
            return LiteParseEngine.parse(
                self._cfg, data, path, target_pages=target_pages
            )
        except LiteParseError as e:
            msg = f"failed to parse document {path!r}: {e}"
            raise RuntimeError(msg) from e

    def _parse_native_sync(self, data: bytes, path: str) -> Any:
        try:
            return LiteParseEngine.parse_native(self._cfg, data, path)
        except LiteParseError as e:
            msg = f"failed to parse document {path!r}: {e}"
            raise RuntimeError(msg) from e
