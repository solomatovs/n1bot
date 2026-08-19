"""Инструменты doc: функции уровня модуля, модуль — обычная программа.

Разбор документов (liteparse, OCR) исполняется в теле — потому оно живёт в
песочнице: парсер работает с недоверенными файлами workspace.

Ошибки:
LiteParseError — документ не разобрать (формат, битый файл, нет моделей OCR).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field

from boba.text.document import LiteParseError
from boba.tool.doc.config import DocToolsConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import TableResult, TextResult, ToolResult, pack_result

_PATH_DESCRIPTION = (
    "Путь к файлу в /workspace, например "
    "'/workspace/<thread_id>/upload/report.pdf'. Не URL: для веб-страниц "
    "есть web_fetch_page."
)
_OCR_DESCRIPTION = (
    "OCR для сканов и изображений: true распознаёт текст по картинкам, "
    "false — только текстовый слой. Сканам/фото — true, обычным "
    "pdf/docx — false (OCR дорог: минуты и гигабайты памяти)."
)
_WORKERS_DESCRIPTION = "Параллелизм OCR, 1..4; ~50-100 MiB на воркер"
_LANGUAGE_DESCRIPTION = (
    "Язык OCR в формате Tesseract: 'rus+eng' для русских документов, "
    "'eng' для английских."
)


class DocErrorKind(StrEnum):
    """Ожидаемые отказы doc-инструментов."""

    DOCUMENT_UNREADABLE = "document_unreadable"


class DocToolSection(DocToolsConfig):
    """Конфиг doc-инструментов; секция [tool.doc]."""

    SECTION: ClassVar[str] = "tool.doc"


class DocOutlineRow(BaseModel):
    """Строка карты документа: страница и её метрики."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: int
    width: float
    height: float
    chars: int
    items: int


class DocSearchRow(BaseModel):
    """Совпадение поиска: страница, координаты и сниппет вокруг."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: int
    x: float
    y: float
    width: float
    height: float
    snippet: str


class TextClip:
    """Обрезка текста по лимиту с признаком усечения и пометкой для LLM."""

    @staticmethod
    def clip(text: str, limit: int) -> tuple[str, bool]:
        if len(text) <= limit:
            return text, False
        return text[:limit], True

    @staticmethod
    def mark(text: str, truncated: bool, limit: int) -> str:
        if not truncated:
            return text
        return f"{text}\n\n[truncated to {limit} characters]"


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


@tool
async def read_document(  # noqa: PLR0913 — фасад LLM, параметры независимы
    path: Annotated[str, Field(min_length=1, description=_PATH_DESCRIPTION)],
    pages: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Страницы, 1-based: диапазоны и перечисление через "
                "запятую, например '1-5,10,15-20'. Число страниц в "
                "документе узнаётся из document_outline."
            ),
        ),
    ],
    ocr_enabled: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
    num_workers: Annotated[
        int, Field(ge=1, le=4, description=_WORKERS_DESCRIPTION)
    ] = 1,
    ocr_language: Annotated[
        str, Field(min_length=1, description=_LANGUAGE_DESCRIPTION)
    ] = "rus+eng",
    *,
    cfg: Annotated[DocToolSection, Injected],
) -> tuple[str, ToolResult]:
    """Прочитать текст страниц документа из workspace; основной способ чтения."""
    # liteparse тяжёлый и нативный: импорт в теле, разбор в потоке (GIL)
    from boba.liteparse.engine import LiteParseEngine  # noqa: PLC0415

    run_cfg = cfg.with_parser(
        ocr_enabled=ocr_enabled, num_workers=num_workers, ocr_language=ocr_language
    )

    result = await asyncio.to_thread(LiteParseEngine.parse_pages, run_cfg, path, pages)

    text, truncated = TextClip.clip(result.text, run_cfg.max_text_chars)

    parsed_pages: list[str] = []
    for page in result.pages:
        parsed_pages.append(str(page.page_num))

    artifact = TextResult(
        text=TextClip.mark(text, truncated, run_cfg.max_text_chars),
        metadata={
            "path": path,
            "pages": ",".join(parsed_pages),
            "truncated": str(truncated),
        },
    )
    return pack_result(artifact)


@tool
async def document_outline(
    path: Annotated[str, Field(min_length=1, description=_PATH_DESCRIPTION)],
    ocr_enabled: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
    num_workers: Annotated[
        int, Field(ge=1, le=4, description=_WORKERS_DESCRIPTION)
    ] = 1,
    ocr_language: Annotated[
        str, Field(min_length=1, description=_LANGUAGE_DESCRIPTION)
    ] = "rus+eng",
    *,
    cfg: Annotated[DocToolSection, Injected],
) -> tuple[str, ToolResult]:
    """Карта документа по страницам: дешёвый обзор перед read_document."""
    from boba.liteparse.engine import LiteParseEngine  # noqa: PLC0415

    run_cfg = cfg.with_parser(
        ocr_enabled=ocr_enabled, num_workers=num_workers, ocr_language=ocr_language
    )

    result = await asyncio.to_thread(LiteParseEngine.parse, run_cfg, path)

    rows: list[dict[str, Any]] = []
    for page in result.pages:
        row = DocOutlineRow(
            page=page.page_num,
            width=round(page.width, 1),
            height=round(page.height, 1),
            chars=len(page.text),
            items=len(page.text_items),
        )
        rows.append(row.model_dump())

    table = TableResult(
        rows=rows,
        note=f"{path}: pages {result.num_pages}",
        metadata={"path": path},
    )
    return pack_result(table)


@tool
async def search_document(  # noqa: PLR0913 — фасад LLM, параметры независимы
    path: Annotated[str, Field(min_length=1, description=_PATH_DESCRIPTION)],
    query: Annotated[
        str, Field(min_length=1, description="Искомая фраза (регистронезависимо).")
    ],
    ocr_enabled: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
    num_workers: Annotated[
        int, Field(ge=1, le=4, description=_WORKERS_DESCRIPTION)
    ] = 1,
    ocr_language: Annotated[
        str, Field(min_length=1, description=_LANGUAGE_DESCRIPTION)
    ] = "rus+eng",
    *,
    cfg: Annotated[DocToolSection, Injected],
) -> tuple[str, ToolResult]:
    """Найти фразу в документе: страница, координаты совпадения и сниппет."""
    from boba.liteparse.engine import LiteParseEngine  # noqa: PLC0415

    run_cfg = cfg.with_parser(
        ocr_enabled=ocr_enabled, num_workers=num_workers, ocr_language=ocr_language
    )

    native = await asyncio.to_thread(LiteParseEngine.parse_native, run_cfg, path)

    rows: list[dict[str, Any]] = []
    limit_reached = False
    for page in native.pages:
        hits = LiteParseEngine.search_items(
            page.text_items, query, case_sensitive=False
        )
        if not hits:
            continue

        matcher = PageMatchRows(page, query, run_cfg.search_context_chars)
        for row in matcher.rows(hits):
            if len(rows) >= run_cfg.search_max_matches:
                limit_reached = True
                break

            rows.append(row.model_dump())

        if limit_reached:
            break

    note = f"{path}: matches {len(rows)}"
    if limit_reached:
        note += " (search_max_matches limit reached)"

    table = TableResult(
        rows=rows,
        note=note,
        metadata={"path": path, "query": query},
    )
    return pack_result(table)


EXPECTED: Mapping[type[Exception], DocErrorKind] = {
    LiteParseError: DocErrorKind.DOCUMENT_UNREADABLE,
}

TOOLS: Final = ToolMain.toolset(read_document, document_outline, search_document)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
