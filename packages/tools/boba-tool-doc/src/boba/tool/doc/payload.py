"""Операции над документами: единый движок boba.liteparse в песочнице.

Запросы и ответы проходят через те же pydantic-модели, которыми пользуется
caller-сторона (boba.tool.doc.protocol и doc.liteparse.protocol): контракт
границы — физически один код с обеих сторон.

Ошибки: LiteParseError — документ не разобрать (формат, битый файл, нет моделей
OCR) и pydantic.ValidationError — запрос не по контракту; обе объявлены
ожидаемыми и уезжают пользователю кадром с готовым текстом. Остальное, включая
ValueError на неизвестном op, роняет payload трейсбеком: это дефект кода.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Mapping
from typing import Any, ClassVar

from pydantic import BaseModel

from boba.liteparse.engine import LiteParseEngine
from boba.text.document import LiteParseError
from boba.tool.doc.liteparse.protocol import (
    ParseBytesRequest,
    ParseBytesTrailer,
    ParsedPage,
)
from boba.tool.doc.protocol import (
    DocOutlineRow,
    DocOutlineTrailer,
    DocPagesRequest,
    DocPagesTrailer,
    DocPathRequest,
    DocSearchRequest,
    DocSearchRow,
    DocSearchTrailer,
)
from boba.toolkit.launcher import RowStream
from boba.toolkit.payload import ChunkEmitter, PayloadEntry


class TextClip:
    """Обрезка текста по лимиту с признаком усечения."""

    @staticmethod
    def clip(text: str, limit: int) -> tuple[str, bool]:
        if len(text) <= limit:
            return text, False
        return text[:limit], True


class Snippet:
    """Вырез [lo, hi) с контекстом вокруг и многоточиями по краям."""

    ELLIPSIS: ClassVar[str] = "…"

    @classmethod
    def around(cls, text: str, lo: int, hi: int, context: int) -> str:
        begin = max(0, lo - context)
        end = min(len(text), hi + context)

        prefix = ""
        if begin > 0:
            prefix = cls.ELLIPSIS

        suffix = ""
        if end < len(text):
            suffix = cls.ELLIPSIS

        return f"{prefix}{text[begin:end]}{suffix}"


class PageMatchRows:
    """Строки совпадений одной страницы: hit'ы liteparse плюс сниппеты."""

    def __init__(self, page: Any, query: str, context_chars: int) -> None:
        self._page = page
        self._query = query
        self._context = context_chars
        self._haystack = page.text.casefold()
        self._needle = query.casefold()
        # курсор по casefold-тексту: i-й hit получает i-е вхождение запроса
        self._cursor = 0

    def rows(self, hits: Any) -> Iterator[DocSearchRow]:
        for hit in hits:
            yield self._row(hit)

    def _row(self, hit: Any) -> DocSearchRow:
        return DocSearchRow(
            page=self._page.page_num,
            x=round(hit.x, 1),
            y=round(hit.y, 1),
            width=round(hit.width, 1),
            height=round(hit.height, 1),
            snippet=self._snippet(hit),
        )

    def _snippet(self, hit: Any) -> str:
        index = self._haystack.find(self._needle, self._cursor)
        if index == -1:
            return hit.text

        self._cursor = index + len(self._query)
        return Snippet.around(
            self._page.text, index, index + len(self._query), self._context
        )


class DocumentOps:
    """Операции liteparse; вызываются диспетчером payload'а по имени op."""

    OPS: ClassVar[tuple[str, ...]] = (
        DocPagesRequest.OP,
        DocPathRequest.OUTLINE,
        DocSearchRequest.OP,
        ParseBytesRequest.OP,
    )

    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {
        LiteParseError: "document_unreadable",
    }

    @classmethod
    async def dispatch(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> dict[str, Any]:
        """Текст уходит кадрами-кусками, строки выдачи — кадрами-записями."""
        handlers: dict[str, Callable[[dict[str, Any], ChunkEmitter], BaseModel]] = {
            DocPagesRequest.OP: cls.read_document,
            DocPathRequest.OUTLINE: cls.document_outline,
            DocSearchRequest.OP: cls.search_document,
            ParseBytesRequest.OP: cls.parse_bytes,
        }

        op = request["op"]
        if op not in handlers:
            msg = f"unknown document op: {op!r}"
            raise ValueError(msg)

        trailer = handlers[op](request, emit)
        return trailer.model_dump()

    @classmethod
    def read_document(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> DocPagesTrailer:
        req = DocPagesRequest.model_validate(request)
        result = LiteParseEngine.parse_pages(req.params, req.path, req.pages)

        text, truncated = TextClip.clip(result.text, req.params.max_text_chars)
        PayloadEntry.emit_text(emit, text)
        pages = tuple(cls._page_numbers(result))
        return DocPagesTrailer(truncated=truncated, pages=pages)

    @classmethod
    def document_outline(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> DocOutlineTrailer:
        req = DocPathRequest.model_validate(request)
        result = LiteParseEngine.parse(req.params, req.path)

        for row in cls._outline_rows(result):
            emit(RowStream.encode(row.model_dump()))
        return DocOutlineTrailer(num_pages=result.num_pages)

    @classmethod
    def search_document(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> DocSearchTrailer:
        req = DocSearchRequest.model_validate(request)
        native = LiteParseEngine.parse_native(req.params, req.path)

        emitted = 0
        for page in native.pages:
            for row in cls.search_page(page, req):
                if emitted >= req.max_matches:
                    return DocSearchTrailer(limit_reached=True)
                emit(RowStream.encode(row.model_dump()))
                emitted += 1

        return DocSearchTrailer(limit_reached=False)

    @classmethod
    def search_page(cls, page: Any, req: DocSearchRequest) -> list[DocSearchRow]:
        hits = LiteParseEngine.search_items(
            page.text_items, req.query, case_sensitive=False
        )
        if not hits:
            return []

        matcher = PageMatchRows(page, req.query, req.context_chars)
        return list(matcher.rows(hits))

    @classmethod
    def parse_bytes(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> ParseBytesTrailer:
        """Разобрать документ, приехавший содержимым в запросе (base64)."""
        req = ParseBytesRequest.model_validate(request)
        result = LiteParseEngine.parse_bytes(req.params, req.content(), req.filename)

        for page in cls._parsed_pages(result):
            emit(RowStream.encode(page.model_dump()))
        return ParseBytesTrailer(num_pages=result.num_pages)

    @staticmethod
    def _page_numbers(result: Any) -> Iterator[int]:
        for page in result.pages:
            yield page.page_num

    @staticmethod
    def _outline_rows(result: Any) -> Iterator[DocOutlineRow]:
        for page in result.pages:
            yield DocOutlineRow(
                page=page.page_num,
                width=round(page.width, 1),
                height=round(page.height, 1),
                chars=len(page.text),
                items=len(page.text_items),
            )

    @staticmethod
    def _parsed_pages(result: Any) -> Iterator[ParsedPage]:
        for page in result.pages:
            yield ParsedPage(page_num=page.page_num, text=page.text)


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(DocumentOps))
