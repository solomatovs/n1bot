"""Операции над документами: единый движок boba.liteparse в песочнице.

Запросы и ответы проходят через те же pydantic-модели, которыми пользуется
caller-сторона (boba.tool.doc.protocol и doc.liteparse.protocol): контракт
границы — физически один код с обеих сторон.

Ошибки: ValueError — неизвестный op; pydantic.ValidationError — запрос не
по контракту; LiteParseError/RuntimeError — сбой парсинга. Все они роняют
payload, и PayloadEntry отдаёт их наружу через stderr песочницы.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from typing import Any, ClassVar

from pydantic import BaseModel

from boba.liteparse.engine import LiteParseEngine
from boba.tool.doc.liteparse.protocol import (
    ParseBytesAnswer,
    ParseBytesRequest,
    ParsedPage,
)
from boba.tool.doc.protocol import (
    DocOutlineAnswer,
    DocOutlineRow,
    DocPagesAnswer,
    DocPagesRequest,
    DocPathRequest,
    DocSearchAnswer,
    DocSearchRequest,
    DocSearchRow,
    DocTextAnswer,
    DocWindowAnswer,
    DocWindowRequest,
)
from boba.toolkit.payload.entry import PayloadEntry


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
        # read_document production-кодом не зовётся (DocEngine шлёт read_pages),
        # но остаётся в контракте: его гоняет test_payload_in_sandbox
        DocPathRequest.READ,
        DocPagesRequest.OP,
        DocWindowRequest.OP,
        DocPathRequest.OUTLINE,
        DocSearchRequest.OP,
        ParseBytesRequest.OP,
    )

    @classmethod
    def dispatch(cls, request: dict[str, Any]) -> dict[str, Any]:
        handlers: dict[str, Callable[[dict[str, Any]], BaseModel]] = {
            DocPathRequest.READ: cls.read_document,
            DocPagesRequest.OP: cls.read_pages,
            DocWindowRequest.OP: cls.read_document_window,
            DocPathRequest.OUTLINE: cls.document_outline,
            DocSearchRequest.OP: cls.search_document,
            ParseBytesRequest.OP: cls.parse_bytes,
        }

        op = request["op"]
        if op not in handlers:
            msg = f"unknown document op: {op!r}"
            raise ValueError(msg)

        answer = handlers[op](request)
        return answer.model_dump()

    @classmethod
    def read_document(cls, request: dict[str, Any]) -> DocTextAnswer:
        req = DocPathRequest.model_validate(request)
        result = LiteParseEngine.parse(req.params, req.path)

        text, truncated = TextClip.clip(result.text, req.params.max_text_chars)
        return DocTextAnswer(
            text=text,
            truncated=truncated,
            num_pages=result.num_pages,
        )

    @classmethod
    def read_pages(cls, request: dict[str, Any]) -> DocPagesAnswer:
        req = DocPagesRequest.model_validate(request)
        result = LiteParseEngine.parse_pages(req.params, req.path, req.pages)

        text, truncated = TextClip.clip(result.text, req.params.max_text_chars)
        pages = tuple(cls._page_numbers(result))
        return DocPagesAnswer(text=text, truncated=truncated, pages=pages)

    @classmethod
    def read_document_window(cls, request: dict[str, Any]) -> DocWindowAnswer:
        req = DocWindowRequest.model_validate(request)
        result = LiteParseEngine.parse(req.params, req.path)

        full = result.text
        chunk = full[req.start_char : req.start_char + req.length]
        end = req.start_char + len(chunk)
        return DocWindowAnswer(
            text=chunk,
            start_char=req.start_char,
            end_char=end,
            total_chars=len(full),
            has_more=end < len(full),
        )

    @classmethod
    def document_outline(cls, request: dict[str, Any]) -> DocOutlineAnswer:
        req = DocPathRequest.model_validate(request)
        result = LiteParseEngine.parse(req.params, req.path)

        rows = tuple(cls._outline_rows(result))
        return DocOutlineAnswer(num_pages=result.num_pages, rows=rows)

    @classmethod
    def search_document(cls, request: dict[str, Any]) -> DocSearchAnswer:
        req = DocSearchRequest.model_validate(request)
        native = LiteParseEngine.parse_native(req.params, req.path)

        rows: list[DocSearchRow] = []
        for page in native.pages:
            rows.extend(cls.search_page(page, req))
            if len(rows) >= req.max_matches:
                del rows[req.max_matches :]
                return DocSearchAnswer(rows=tuple(rows), limit_reached=True)

        return DocSearchAnswer(rows=tuple(rows), limit_reached=False)

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
    def parse_bytes(cls, request: dict[str, Any]) -> ParseBytesAnswer:
        """Разобрать документ, приехавший содержимым в запросе (base64)."""
        req = ParseBytesRequest.model_validate(request)
        result = LiteParseEngine.parse_bytes(req.params, req.content(), req.filename)

        pages = tuple(cls._parsed_pages(result))
        return ParseBytesAnswer(num_pages=result.num_pages, pages=pages)

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
    sys.exit(PayloadEntry.main(DocumentOps.dispatch))
