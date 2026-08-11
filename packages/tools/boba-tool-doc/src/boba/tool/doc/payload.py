"""Операции над документами: единый движок boba.liteparse в песочнице.

Запросы и ответы проходят через те же pydantic-модели, которыми пользуется
caller-сторона (boba.tool.doc.protocol и doc.liteparse.protocol): контракт
границы — физически один код с обеих сторон. Текст уезжает в канал данных
сырыми байтами, строки выдачи — NDJSON-строками.

Ошибки: LiteParseError — документ не разобрать (формат, битый файл, нет моделей
OCR); объявлена ожидаемой и уезжает пользователю конвертом с готовым текстом.
Остальное, включая несовпадение модели запроса с диспетчером, роняет payload
трейсбеком: это дефект кода.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping
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
    DocOp,
    DocOutlineRow,
    DocOutlineTrailer,
    DocPagesRequest,
    DocPagesTrailer,
    DocPathRequest,
    DocSearchRequest,
    DocSearchRow,
    DocSearchTrailer,
)
from boba.toolkit.channels import ByteText, StreamCodec
from boba.toolkit.payload import PayloadChannels, PayloadEntry, PayloadStream


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
    """Операции liteparse; вызываются диспетчером payload'а по модели запроса."""

    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {
        LiteParseError: "document_unreadable",
    }

    REQUESTS: ClassVar[Mapping[str, type[BaseModel]]] = {
        DocOp.READ: DocPagesRequest,
        DocOp.OUTLINE: DocPathRequest,
        DocOp.SEARCH: DocSearchRequest,
        ParseBytesRequest.OP: ParseBytesRequest,
    }

    @classmethod
    async def dispatch(
        cls, request: BaseModel, channels: PayloadChannels
    ) -> BaseModel:
        """Текст уходит байтами в канал данных, строки выдачи — NDJSON."""
        stream = channels.payload()

        if isinstance(request, DocPagesRequest):
            return cls.read_document(request, stream)

        if isinstance(request, DocSearchRequest):
            return cls.search_document(request, stream)

        if isinstance(request, ParseBytesRequest):
            return cls.parse_bytes(request, stream)

        if isinstance(request, DocPathRequest):
            return cls.document_outline(request, stream)

        msg = f"unexpected request model: {type(request).__name__}"
        raise TypeError(msg)

    @classmethod
    def read_document(
        cls, request: DocPagesRequest, stream: PayloadStream
    ) -> DocPagesTrailer:
        params = request.parse_params()
        result = LiteParseEngine.parse_pages(params, request.path, request.pages)

        text, truncated = TextClip.clip(result.text, request.max_text_chars)
        stream.write(text.encode(ByteText.ENCODING))

        pages = tuple(cls._page_numbers(result))

        return DocPagesTrailer(truncated=truncated, pages=pages)

    @classmethod
    def document_outline(
        cls, request: DocPathRequest, stream: PayloadStream
    ) -> DocOutlineTrailer:
        params = request.parse_params()
        result = LiteParseEngine.parse(params, request.path)

        for row in cls._outline_rows(result):
            stream.write(StreamCodec.encode_row(row.model_dump()))

        return DocOutlineTrailer(num_pages=result.num_pages)

    @classmethod
    def search_document(
        cls, request: DocSearchRequest, stream: PayloadStream
    ) -> DocSearchTrailer:
        params = request.parse_params()
        native = LiteParseEngine.parse_native(params, request.path)

        emitted = 0
        for page in native.pages:
            for row in cls.search_page(page, request):
                if emitted >= request.max_matches:
                    return DocSearchTrailer(limit_reached=True)
                stream.write(StreamCodec.encode_row(row.model_dump()))
                emitted += 1

        return DocSearchTrailer(limit_reached=False)

    @classmethod
    def search_page(cls, page: Any, request: DocSearchRequest) -> list[DocSearchRow]:
        hits = LiteParseEngine.search_items(
            page.text_items, request.query, case_sensitive=False
        )
        if not hits:
            return []

        matcher = PageMatchRows(page, request.query, request.context_chars)

        return list(matcher.rows(hits))

    @classmethod
    def parse_bytes(
        cls, request: ParseBytesRequest, stream: PayloadStream
    ) -> ParseBytesTrailer:
        """Разобрать документ, приехавший содержимым в запросе (base64)."""
        params = request.parse_params()
        result = LiteParseEngine.parse_bytes(
            params, request.content(), request.filename
        )

        for page in cls._parsed_pages(result):
            stream.write(StreamCodec.encode_row(page.model_dump()))

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
