"""Единый движок поверх liteparse: parse / parse_native / search_items.

Центральная (и единственная) точка, где импортируется liteparse —
публичный API и приватный нативный модуль. Потребители (doc-tool,
будущий indexer-reader) ходят только сюда и не знают про устройство
liteparse, временные файлы и приватные символы.

Почему нужен parse_native + приватный liteparse._liteparse:
публичный liteparse.search_items в 2.0.x сломан — он передаёт
dataclass-TextItem (вывод публичного parse()) в нативную функцию,
ждущую PyTextItem, и падает с TypeError. Поэтому для поиска по bbox
парсим нативным parse_native (отдаёт нативные PyTextItem) и зовём
нативный search_items. КОГДА АПСТРИМ ПОЧИНИТ публичный search_items —
parse_native и весь private-импорт можно удалить, а search_items
перевести на публичный поверх обычного parse().
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from boba.liteparse.errors import LiteParseError
from boba.liteparse.params import LiteParseParams
from liteparse import LiteParse, ParseError, ParseResult

# Приватный нативный модуль liteparse — нужен только для parse_native +
# search_items (см. docstring модуля). Держим импорт строго здесь.
from liteparse._liteparse import LiteParse as _NativeLiteParse
from liteparse._liteparse import search_items as _native_search_items

__all__ = ["LiteParseEngine"]


class LiteParseEngine:
    """Парсинг байт документа через liteparse; ошибки -> LiteParseError."""

    @staticmethod
    @contextmanager
    def _on_disk(data: bytes, filename: str) -> Iterator[str]:
        """Записать байты во временный файл с расширением из filename; отдать путь.

        liteparse определяет office-форматы (docx/xlsx/pptx — это zip) по
        расширению файла, поэтому парсить нужно реальный файл с исходным
        суффиксом, а не голые байты (иначе docx опознаётся как .zip).
        Файл гарантированно удаляется на выходе.
        """
        suffix = os.path.splitext(filename)[1]
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            yield tmp_path
        finally:
            os.unlink(tmp_path)

    @staticmethod
    def parse(
        params: LiteParseParams,
        data: bytes,
        filename: str,
        *,
        target_pages: str | None = None,
    ) -> ParseResult:
        """Распарсить документ публичным API liteparse; ошибки -> LiteParseError."""
        parser = LiteParse(
            ocr_enabled=params.ocr_enabled,
            ocr_language=params.ocr_language,
            max_pages=params.max_pages or None,
            target_pages=target_pages,
            quiet=True,
        )
        with LiteParseEngine._on_disk(data, filename) as tmp_path:
            try:
                return parser.parse(tmp_path)
            except ParseError as e:
                raise LiteParseError(str(e)) from e

    @staticmethod
    def parse_native(params: LiteParseParams, data: bytes, filename: str) -> Any:
        """Распарсить документ нативным API liteparse; вернуть нативный результат.

        Отдаёт нативный PyParseResult с нативными PyTextItem — единственный
        вход, который принимает нативный search_items. См. docstring модуля.
        """
        parser = _NativeLiteParse(
            ocr_enabled=params.ocr_enabled,
            ocr_language=params.ocr_language,
            quiet=True,
            **({"max_pages": params.max_pages} if params.max_pages else {}),
        )
        with LiteParseEngine._on_disk(data, filename) as tmp_path:
            try:
                return parser.parse(tmp_path)
            except Exception as e:
                raise LiteParseError(str(e)) from e

    @staticmethod
    def search_items(
        text_items: Any,
        query: str,
        *,
        case_sensitive: bool = False,
    ) -> Any:
        """Найти query среди нативных text_items (bbox-merge через нативный модуль).

        text_items — нативные PyTextItem страницы из parse_native; возвращает
        нативные hit'ы с координатами. Контекст-сниппет модуль не даёт —
        собирается потребителем из page.text.
        """
        return _native_search_items(text_items, query, case_sensitive=case_sensitive)
