"""Операции над документами: liteparse внутри песочницы.

Документ берётся либо по пути внутри песочницы (doc-инструменты читают
workspace пользователя), либо байтами в самом запросе — их base64 шлют
инструменты, скачавшие вложение по сети. Контракт описан в
boba.tool.doc.liteparse.protocol и ...doc.protocol.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
from typing import Any, ClassVar

from liteparse import LiteParse
from liteparse._liteparse import LiteParse as NativeLiteParse
from liteparse._liteparse import search_items as native_search_items
from liteparse.types import ParseError

from boba.toolkit.payload.entry import PayloadEntry


class LocaleRetry:
    """Повторяет парсинг под запасными локалями.

    LibreOffice в локали без UTF-8 (в том числе C.UTF-8) молча — с rc=0 —
    не открывает файлы с не-ASCII именем, и liteparse сводит это к ошибке
    «output PDF not found». Локаль сама по себе язык имени не ограничивает:
    любая UTF-8-локаль открывает имена на любом алфавите, "C" пропускает
    любые байты и есть в любом glibc. LC_ALL перекрывает LANG и LC_* из
    профиля песочницы; LibreOffice наследует env процесса при каждом parse.
    """

    MARKER: ClassVar[str] = (
        "LibreOffice conversion succeeded but output PDF not found"
    )
    RETRYABLE: ClassVar[tuple[type[Exception], ...]] = (ParseError, RuntimeError)
    """LiteParse оборачивает ошибку в ParseError, нативный парсер — RuntimeError."""

    LOCALES: ClassVar[tuple[str, ...]] = ("ru_RU.UTF-8", "en_US.UTF-8", "C")

    @classmethod
    def parse(cls, parser: Any, path: str) -> Any:
        try:
            return parser.parse(path)
        except cls.RETRYABLE as e:
            if cls.MARKER not in str(e):
                raise
            first = e

        errors: list[Exception] = [first]
        for locale_name in cls.LOCALES:
            try:
                return cls.parse_with_locale(parser, path, locale_name)
            except cls.RETRYABLE as e:
                if cls.MARKER not in str(e):
                    raise
                errors.append(e)
        raise first

    @classmethod
    def parse_with_locale(cls, parser: Any, path: str, locale_name: str) -> Any:
        saved = os.environ.get("LC_ALL")
        os.environ["LC_ALL"] = locale_name
        try:
            return parser.parse(path)
        finally:
            if saved is None:
                os.environ.pop("LC_ALL", None)
            else:
                os.environ["LC_ALL"] = saved


class DocumentOps:
    """Операции liteparse; вызываются диспетчером payload'а по имени op."""

    ELLIPSIS: ClassVar[str] = "…"

    OPS: ClassVar[tuple[str, ...]] = (
        "read_document",
        "read_pages",
        "read_document_window",
        "document_outline",
        "search_document",
        "parse_bytes",
    )

    @classmethod
    def dispatch(cls, request: dict[str, Any]) -> dict[str, Any]:
        op = request["op"]
        if op == "read_document":
            return cls.read_document(request)
        if op == "read_pages":
            return cls.read_pages(request)
        if op == "read_document_window":
            return cls.read_document_window(request)
        if op == "document_outline":
            return cls.document_outline(request)
        if op == "search_document":
            return cls.search_document(request)
        if op == "parse_bytes":
            return cls.parse_bytes(request)
        msg = f"unknown document op: {op!r}"
        raise ValueError(msg)

    @classmethod
    def read_document(cls, request: dict[str, Any]) -> dict[str, Any]:
        result = cls.parse(request, target_pages=None)
        text, truncated = cls.clip(result.text, request["params"]["max_text_chars"])
        return {"text": text, "truncated": truncated, "num_pages": result.num_pages}

    @classmethod
    def read_pages(cls, request: dict[str, Any]) -> dict[str, Any]:
        result = cls.parse(request, target_pages=request["pages"])
        text, truncated = cls.clip(result.text, request["params"]["max_text_chars"])
        pages: list[int] = []
        for page in result.pages:
            pages.append(page.page_num)
        return {"text": text, "truncated": truncated, "pages": pages}

    @classmethod
    def read_document_window(cls, request: dict[str, Any]) -> dict[str, Any]:
        result = cls.parse(request, target_pages=None)
        start = request["start_char"]
        chunk = result.text[start : start + request["length"]]
        end = start + len(chunk)
        return {
            "text": chunk,
            "start_char": start,
            "end_char": end,
            "total_chars": len(result.text),
            "has_more": end < len(result.text),
        }

    @classmethod
    def document_outline(cls, request: dict[str, Any]) -> dict[str, Any]:
        result = cls.parse(request, target_pages=None)
        rows: list[dict[str, Any]] = []
        for page in result.pages:
            rows.append(
                {
                    "page": page.page_num,
                    "width": round(page.width, 1),
                    "height": round(page.height, 1),
                    "chars": len(page.text),
                    "items": len(page.text_items),
                }
            )
        return {"num_pages": result.num_pages, "rows": rows}

    @classmethod
    def search_document(cls, request: dict[str, Any]) -> dict[str, Any]:
        native = cls.parse_native(request)
        query = request["query"]
        context = request["context_chars"]
        max_matches = request["max_matches"]
        needle = query.casefold()
        rows: list[dict[str, Any]] = []
        for page in native.pages:
            hits = native_search_items(page.text_items, query, case_sensitive=False)
            if not hits:
                continue
            hay = page.text.casefold()
            cursor = 0
            for hit in hits:
                index = hay.find(needle, cursor)
                if index == -1:
                    snippet = hit.text
                else:
                    snippet = cls.snippet(
                        page.text, index, index + len(query), context
                    )
                    cursor = index + len(query)
                rows.append(
                    {
                        "page": page.page_num,
                        "x": round(hit.x, 1),
                        "y": round(hit.y, 1),
                        "width": round(hit.width, 1),
                        "height": round(hit.height, 1),
                        "snippet": snippet,
                    }
                )
                if len(rows) >= max_matches:
                    return {"rows": rows, "limit_reached": True}
        return {"rows": rows, "limit_reached": False}

    @classmethod
    def parse_bytes(cls, request: dict[str, Any]) -> dict[str, Any]:
        """Документ приехал в запросе: пишем его на tmpfs и парсим как файл.

        Формат liteparse определяет по расширению, поэтому имя файла с
        исходным суффиксом обязательно.
        """
        data = base64.b64decode(request["content_b64"])
        return cls.parse_content(data, request["filename"], request["params"])

    @classmethod
    def parse_content(
        cls, data: bytes, filename: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Разобрать документ, уже лежащий в памяти песочницы."""
        suffix = os.path.splitext(filename)[1]
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            local: dict[str, Any] = {"path": tmp_path, "params": params}
            result = cls.parse(local, target_pages=None)
        finally:
            os.unlink(tmp_path)
        pages: list[dict[str, Any]] = []
        for page in result.pages:
            pages.append({"page_num": page.page_num, "text": page.text})
        return {"num_pages": result.num_pages, "pages": pages}

    @staticmethod
    def check_ocr(params: dict[str, Any]) -> None:
        """OCR без каталога моделей liteparse качает их из интернета.

        В песочнице сети нет, поэтому запрос упёрся бы в таймаут HTTP вместо
        понятной ошибки. Проверяем заранее.
        """
        if not params["ocr_enabled"]:
            return
        path = params["tessdata_path"]
        if os.path.isdir(path):
            return
        msg = (
            f"ocr_enabled=true, но каталога моделей {path!r} нет в rootfs "
            "песочницы: положи туда tessdata или выключи ocr_enabled"
        )
        raise RuntimeError(msg)

    @staticmethod
    def parse(request: dict[str, Any], target_pages: str | None) -> Any:
        params = request["params"]
        DocumentOps.check_ocr(params)
        max_pages = params["max_pages"]
        if max_pages == 0:
            max_pages = None
        parser = LiteParse(
            ocr_enabled=params["ocr_enabled"],
            ocr_language=params["ocr_language"],
            tessdata_path=params["tessdata_path"],
            max_pages=max_pages,
            target_pages=target_pages,
            quiet=True,
        )
        return LocaleRetry.parse(parser, request["path"])

    @staticmethod
    def parse_native(request: dict[str, Any]) -> Any:
        """Нативный парсер: только он отдаёт PyTextItem для search_items."""
        params = request["params"]
        DocumentOps.check_ocr(params)
        options: dict[str, Any] = {
            "ocr_enabled": params["ocr_enabled"],
            "ocr_language": params["ocr_language"],
            "tessdata_path": params["tessdata_path"],
            "quiet": True,
        }
        if params["max_pages"]:
            options["max_pages"] = params["max_pages"]
        parser = NativeLiteParse(**options)
        return LocaleRetry.parse(parser, request["path"])

    @staticmethod
    def clip(text: str, limit: int) -> tuple[str, bool]:
        if len(text) <= limit:
            return text, False
        return text[:limit], True

    @classmethod
    def snippet(cls, text: str, lo: int, hi: int, context: int) -> str:
        begin = max(0, lo - context)
        end = min(len(text), hi + context)
        prefix = ""
        if begin > 0:
            prefix = cls.ELLIPSIS
        suffix = ""
        if end < len(text):
            suffix = cls.ELLIPSIS
        return f"{prefix}{text[begin:end]}{suffix}"


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(DocumentOps.dispatch))
