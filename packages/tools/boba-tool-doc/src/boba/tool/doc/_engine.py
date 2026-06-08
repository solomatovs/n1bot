"""Внутренний слой пакета doc: чтение байт из workspace + сборка liteparse.

Централизует конструирование LiteParse из DocPluginConfig и единый
маппинг ошибок workspace/парсера в RuntimeError — чтобы tool'ы
(read_document/read_pages/document_outline/search_document) не
дублировали эту обвязку.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from liteparse import LiteParse, ParseError, ParseResult

# Приватный нативный модуль liteparse. Нужен только для parse_native
# (см. её docstring и boba.tool.doc.search._Search): публичный
# liteparse.search_items в 2.0.x сломан, а нативный search_items
# работает только с нативными PyTextItem, которые отдаёт нативный parse.
from liteparse._liteparse import LiteParse as _NativeLiteParse

from boba.tool.doc.config import DocPluginConfig
from boba.workspace.contract import (
    ProjectWorkspaceShell,
    WorkspaceError,
    WorkspaceNotFoundError,
)

__all__ = ["DocEngine"]


class DocEngine:
    """Чтение документа из workspace и парсинг через liteparse."""

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
    @contextmanager
    def _on_disk(data: bytes, path: str) -> Iterator[str]:
        """Записать байты во временный файл с расширением из path; отдать его путь.

        liteparse определяет office-форматы (docx/xlsx/pptx — это zip) по
        расширению файла, поэтому парсить нужно реальный файл с исходным
        суффиксом, а не голые байты (иначе docx опознаётся как .zip).
        Файл гарантированно удаляется на выходе.
        """
        suffix = os.path.splitext(path)[1]
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            yield tmp_path
        finally:
            os.unlink(tmp_path)

    @staticmethod
    def parse(
        cfg: DocPluginConfig,
        data: bytes,
        path: str,
        *,
        target_pages: str | None = None,
    ) -> ParseResult:
        """Распарсить документ публичным API liteparse; ошибки -> RuntimeError."""
        parser = LiteParse(
            ocr_enabled=cfg.ocr_enabled,
            ocr_language=cfg.ocr_language,
            max_pages=cfg.max_pages or None,
            target_pages=target_pages,
            quiet=True,
        )
        with DocEngine._on_disk(data, path) as tmp_path:
            try:
                return parser.parse(tmp_path)
            except ParseError as e:
                raise RuntimeError(f"Не удалось распарсить документ: {e}") from e

    @staticmethod
    def parse_native(cfg: DocPluginConfig, data: bytes, path: str) -> Any:
        """Распарсить документ нативным API liteparse; вернуть нативный результат.

        Отдаёт нативный PyParseResult с нативными PyTextItem — это
        единственный вход, который принимает нативный search_items
        (см. boba.tool.doc.search._Search). Используется приватный
        liteparse._liteparse, потому что публичный liteparse.search_items
        в 2.0.x сломан (передаёт dataclass-TextItem в нативную функцию,
        ждущую PyTextItem, -> TypeError), а публичный parse() нативные
        items не отдаёт. КОГДА АПСТРИМ ПОЧИНИТ публичный search_items —
        этот метод и весь private-импорт можно удалить, а _Search перевести
        на from liteparse import search_items поверх обычного parse().
        """
        parser = _NativeLiteParse(
            ocr_enabled=cfg.ocr_enabled,
            ocr_language=cfg.ocr_language,
            quiet=True,
            **({"max_pages": cfg.max_pages} if cfg.max_pages else {}),
        )
        with DocEngine._on_disk(data, path) as tmp_path:
            try:
                return parser.parse(tmp_path)
            except Exception as e:
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
