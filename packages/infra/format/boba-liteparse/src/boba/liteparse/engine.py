"""Всё, что требует нативного liteparse: движок, ретраи локалей, ридер.

Модуль импортируется только там, где liteparse установлен — в rootfs
песочницы. App-safe часть пакета живёт в `boba.liteparse` и
`boba.liteparse.sections`.

Ошибки: LiteParseError — парсинг не удался или нет каталога tessdata;
RuntimeError — сырой сбой нативного парсера вне ParseError (LiteParseError
наследует RuntimeError, так что `except RuntimeError` ловит оба);
IncompatibleContentError — из LiteParseReader через базу PagedDocumentReader.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, ClassVar, Protocol

from boba.text.document import (
    LiteParseError,
    LiteParseParams,
    PagedDocumentReader,
    ParsedPage,
)
from liteparse import LiteParse, ParseError, ParseResult
from liteparse._liteparse import LiteParse as _NativeLiteParse
from liteparse._liteparse import search_items as _native_search_items

__all__ = ["LiteParseEngine", "LiteParseReader", "LocaleRetry"]


class DocumentParser(Protocol):
    """Общий утиный контракт публичного и нативного парсеров liteparse."""

    def parse(self, path: str, /) -> Any: ...


class LocaleRetry:
    """Ретрай под запасными локалями: LibreOffice без UTF-8-локали молча
    (rc=0) не открывает файлы с не-ASCII именем."""

    MARKER: ClassVar[str] = (
        "LibreOffice conversion succeeded but output PDF not found"
    )
    RETRYABLE: ClassVar[tuple[type[Exception], ...]] = (ParseError, RuntimeError)
    LOCALES: ClassVar[tuple[str, ...]] = ("ru_RU.UTF-8", "en_US.UTF-8", "C")

    @classmethod
    def parse(cls, parser: DocumentParser, path: str) -> Any:
        try:
            return parser.parse(path)
        except cls.RETRYABLE as e:
            if cls.MARKER not in str(e):
                raise
            first = e

        for locale_name in cls.LOCALES:
            try:
                return cls.parse_with_locale(parser, path, locale_name)
            except cls.RETRYABLE as e:
                if cls.MARKER not in str(e):
                    raise

        raise first

    @classmethod
    def parse_with_locale(
        cls, parser: DocumentParser, path: str, locale_name: str
    ) -> Any:
        saved = os.environ.get("LC_ALL")
        os.environ["LC_ALL"] = locale_name
        try:
            return parser.parse(path)
        finally:
            if saved is None:
                os.environ.pop("LC_ALL", None)
            else:
                os.environ["LC_ALL"] = saved


class LiteParseEngine:
    """Парсинг документов по LiteParseParams; ошибки -> LiteParseError."""

    @staticmethod
    def check_ocr(params: LiteParseParams) -> None:
        """Проверяет каталог моделей: без него liteparse лезет в сеть."""
        if not params.ocr_enabled:
            return
        if os.path.isdir(params.tessdata_path):
            return

        msg = (
            f"ocr_enabled=true, но каталога моделей {params.tessdata_path!r} "
            "нет в rootfs песочницы: положи туда tessdata или выключи "
            "ocr_enabled"
        )
        raise LiteParseError(msg)

    @classmethod
    def parse(cls, params: LiteParseParams, path: str) -> ParseResult:
        """Распарсить документ целиком публичным API liteparse."""
        return cls._parse_public(params, path, target_pages=None)

    @classmethod
    def parse_pages(
        cls, params: LiteParseParams, path: str, pages: str
    ) -> ParseResult:
        """Распарсить только выбранные страницы, 1-based: '1-5,10'."""
        return cls._parse_public(params, path, target_pages=pages)

    @classmethod
    def parse_bytes(
        cls, params: LiteParseParams, data: bytes, filename: str
    ) -> ParseResult:
        """Распарсить байты: office-формат liteparse узнаёт по расширению."""
        with cls._on_disk(data, filename) as tmp_path:
            return cls.parse(params, tmp_path)

    @classmethod
    def parse_native(cls, params: LiteParseParams, path: str) -> Any:
        """Нативный парсер: только он отдаёт PyTextItem для search_items."""
        cls.check_ocr(params)

        options: dict[str, Any] = {
            "ocr_enabled": params.ocr_enabled,
            "ocr_language": params.ocr_language,
            "tessdata_path": params.tessdata_path,
            "num_workers": params.num_workers,
            "quiet": True,
        }
        limit = cls._page_limit(params)
        if limit is not None:
            options["max_pages"] = limit

        parser = _NativeLiteParse(**options)
        return cls._run(parser, path)

    @staticmethod
    def search_items(
        text_items: Any,
        query: str,
        *,
        case_sensitive: bool = False,
    ) -> Any:
        """Найти query среди нативных text_items; отдаёт hit'ы с bbox."""
        return _native_search_items(text_items, query, case_sensitive=case_sensitive)

    @classmethod
    def _parse_public(
        cls,
        params: LiteParseParams,
        path: str,
        *,
        target_pages: str | None,
    ) -> ParseResult:
        cls.check_ocr(params)

        parser = LiteParse(
            ocr_enabled=params.ocr_enabled,
            ocr_language=params.ocr_language,
            tessdata_path=params.tessdata_path,
            max_pages=cls._page_limit(params),
            target_pages=target_pages,
            num_workers=params.num_workers,
            quiet=True,
        )
        return cls._run(parser, path)

    @staticmethod
    def _page_limit(params: LiteParseParams) -> int | None:
        """Контрактный «0 = без лимита» -> None, которого ждёт API liteparse."""
        if params.max_pages == 0:
            return None
        return params.max_pages

    @staticmethod
    def _run(parser: DocumentParser, path: str) -> Any:
        try:
            return LocaleRetry.parse(parser, path)
        except ParseError as e:
            raise LiteParseError(str(e)) from e

    @staticmethod
    @contextmanager
    def _on_disk(data: bytes, filename: str) -> Generator[str]:
        """Байты во временный файл с суффиксом исходного имени; файл удаляется."""
        # office-форматы (docx/xlsx/pptx — это zip) liteparse узнаёт по расширению
        suffix = os.path.splitext(filename)[1]
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name

        try:
            yield tmp_path
        finally:
            os.unlink(tmp_path)


class LiteParseReader(PagedDocumentReader):
    """Reader[str] прямо на движке — для окружений, где liteparse под рукой."""

    PARSE_ERRORS: ClassVar[tuple[type[Exception], ...]] = (LiteParseError,)

    def __init__(self, params: LiteParseParams) -> None:
        self._params = params

    def parse_pages(self, data: bytes, filename: str) -> Sequence[ParsedPage]:
        result = LiteParseEngine.parse_bytes(self._params, data, filename)
        return tuple(self._pages(result))

    @staticmethod
    def _pages(result: ParseResult) -> Iterator[ParsedPage]:
        for page in result.pages:
            yield ParsedPage(page_num=page.page_num, text=page.text)
