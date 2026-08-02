"""Внутренний слой пакета doc: чтение байт из workspace + обвязка над движком.

Парсинг делегируется общему LiteParseEngine (boba.liteparse). Здесь —
doc-tool-специфика: чтение из workspace, маппинг ошибок workspace/парсера
в RuntimeError с человекочитаемым текстом и presentation-хелперы
(clip/window), чтобы tool'ы (read_document/read_pages/document_outline/
search_document) не дублировали обвязку.
"""

from __future__ import annotations

from typing import Any

from boba.liteparse import LiteParseEngine, LiteParseError, ParseResult
from boba.tool.doc.config import DocPluginConfig
from boba.workspace.contract import (
    ProjectWorkspaceShell,
    WorkspaceError,
    WorkspaceNotFoundError,
)

__all__ = ["DocEngine"]


class DocEngine:
    """Чтение документа из workspace и парсинг через LiteParseEngine."""

    @staticmethod
    def read_bytes(shell: ProjectWorkspaceShell, path: str) -> bytes:
        """Прочитать байты файла; ошибки workspace -> RuntimeError с понятным текстом."""
        try:
            with shell.read_binary(path) as fh:
                return fh.read()
        except WorkspaceNotFoundError as e:
            raise RuntimeError(f"Файл не найден: {path}") from e
        except WorkspaceError as e:
            raise RuntimeError(f"Ошибка чтения: {e}") from e

    @staticmethod
    def parse(
        cfg: DocPluginConfig,
        data: bytes,
        path: str,
        *,
        target_pages: str | None = None,
    ) -> ParseResult:
        """Распарсить документ через LiteParseEngine; ошибки -> RuntimeError."""
        try:
            return LiteParseEngine.parse(cfg, data, path, target_pages=target_pages)
        except LiteParseError as e:
            raise RuntimeError(f"Не удалось распарсить документ: {e}") from e

    @staticmethod
    def parse_native(cfg: DocPluginConfig, data: bytes, path: str) -> Any:
        """Распарсить документ нативным API (для search_document); -> RuntimeError.

        Нативный результат нужен search_document для bbox-merge через
        LiteParseEngine.search_items (публичный liteparse.search_items в
        2.0.x сломан — детали в boba.liteparse.engine).
        """
        try:
            return LiteParseEngine.parse_native(cfg, data, path)
        except LiteParseError as e:
            raise RuntimeError(f"Не удалось распарсить документ: {e}") from e

    @staticmethod
    def clip(text: str, limit: int) -> tuple[str, bool]:
        """Обрезать текст до limit символов; вернуть (текст, факт обрезки)."""
        if len(text) <= limit:
            return text, False
        return text[:limit], True

    @staticmethod
    def window(text: str, start: int, length: int) -> tuple[str, int, int, bool]:
        """Срез text[start:start+length]; -> (срез, end_char, total, has_more)."""
        total = len(text)
        chunk = text[start : start + length]
        end = start + len(chunk)
        return chunk, end, total, end < total
