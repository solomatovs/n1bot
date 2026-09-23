"""Инструменты doc: функции уровня модуля, модуль — обычная программа.

Разбор документов (ридеры форматов, OCR) исполняется в теле — потому оно
живёт в песочнице: ридеры работают с недоверенными файлами workspace.

Ошибки:
DocumentError — документ не распознан, не открыт или не прочитан: формат,
    битый файл, нет файлов моделей OCR.
OcrUnavailableError — вызов просил OCR, а секция [tool.doc] держит
    ocr.provider = off (boba.doc.config).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final

from pydantic import Field

from boba.doc.config import OcrUnavailableError
from boba.doc.document import (
    Document,
    DocumentError,
    DocumentHint,
    Hit,
    PageInfo,
    PageWindow,
)
from boba.tool.doc.config import DocToolsConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import MarkdownResult, TableResult

_PATH_DESCRIPTION = (
    "Путь к файлу в /workspace, например "
    "'/workspace/<thread_id>/upload/report.pdf'. Не URL: для веб-страниц "
    "есть web_fetch_page."
)
_OCR_DESCRIPTION = (
    "OCR для сканов и изображений: true распознаёт текст по картинкам, "
    "false — только текстовый слой. Сканам/фото — true, обычным "
    "pdf/docx — false (OCR дорог: секунды на страницу)."
)


class DocErrorKind(StrEnum):
    """Ожидаемые отказы doc-инструментов."""

    DOCUMENT_UNREADABLE = "document_unreadable"
    OCR_UNAVAILABLE = "ocr_unavailable"


class DocToolSection(DocToolsConfig):
    """Конфиг doc-инструментов; секция [tool.doc]."""

    SECTION: ClassVar[str] = "tool.doc"


@dataclass(frozen=True)
class ReadResult:
    """Текст прочитанных страниц и их номера."""

    text: str
    numbers: Sequence[int]


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


class DocRun:
    """Чтение файла workspace роутером boba-doc под один вызов инструмента.

    Роутер и движок OCR собираются в конструкторе из секции и флага OCR
    вызова; библиотеки форматов тяжёлые и живут в песочнице, поэтому
    импортируются здесь, а не при загрузке модуля: манифест плагина
    импортирует модуль в процессе приложения.
    """

    def __init__(self, cfg: DocToolSection, *, ocr_enabled: bool) -> None:
        from boba.doc.ocr import OcrEngines  # noqa: PLC0415
        from boba.doc.router import DocumentRouter  # noqa: PLC0415

        self._cfg = cfg.for_call(ocr=ocr_enabled)
        self._router = DocumentRouter(self._cfg, OcrEngines().of(self._cfg.ocr))

    @contextmanager
    def open(self, path: str) -> Generator[Document, None, None]:
        try:
            source = open(path, "rb")  # noqa: SIM115 — закрывается ниже
        except OSError as exc:
            raise DocumentError(
                f"document {path}: cannot open the file: {exc}"
            ) from exc

        with source, self._router.open(source, DocumentHint(filename=path)) as document:
            yield document

    def read(self, path: str, pages: str) -> ReadResult:
        windows = PageWindow.parse_many(pages)
        texts: list[str] = []
        numbers: list[int] = []
        with self.open(path) as document:
            for window in windows:
                for page in document.pages(window):
                    texts.append(page.text)
                    numbers.append(page.number)

        return ReadResult(text="\n\n".join(texts), numbers=numbers)

    def outline(self, path: str) -> tuple[int, Sequence[PageInfo]]:
        with self.open(path) as document:
            return document.page_count(), list(document.outline())

    def search(self, path: str, query: str) -> tuple[list[Hit], bool]:
        """Совпадения до лимита; второй элемент — лимит достигнут."""
        hits: list[Hit] = []
        with self.open(path) as document:
            found = document.search(
                query,
                PageWindow.whole(),
                case_sensitive=False,
                context=self._cfg.search_context_chars,
            )
            for hit in found:
                if len(hits) >= self._cfg.search_max_matches:
                    return hits, True

                hits.append(hit)

        return hits, False


@tool
async def read_document(
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
    *,
    cfg: Annotated[DocToolSection, Injected],
) -> MarkdownResult:
    """Прочитать текст страниц документа из workspace; основной способ чтения."""
    # ридеры синхронные и тяжёлые: разбор уходит в поток
    run = DocRun(cfg, ocr_enabled=ocr_enabled)
    result = await asyncio.to_thread(run.read, path, pages)

    text, truncated = TextClip.clip(result.text, cfg.max_text_chars)

    numbers: list[str] = []
    for number in result.numbers:
        numbers.append(str(number))

    return MarkdownResult(
        text=TextClip.mark(text, truncated, cfg.max_text_chars),
        metadata={
            "path": path,
            "pages": ",".join(numbers),
            "truncated": str(truncated),
        },
    )


@tool
async def document_outline(
    path: Annotated[str, Field(min_length=1, description=_PATH_DESCRIPTION)],
    ocr_enabled: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
    *,
    cfg: Annotated[DocToolSection, Injected],
) -> TableResult:
    """Карта документа по страницам: дешёвый обзор перед read_document."""
    run = DocRun(cfg, ocr_enabled=ocr_enabled)
    count, infos = await asyncio.to_thread(run.outline, path)

    rows: list[dict[str, Any]] = []
    for info in infos:
        rows.append(asdict(info))

    return TableResult(
        rows=rows,
        note=f"{path}: pages {count}",
        metadata={"path": path},
    )


@tool
async def search_document(
    path: Annotated[str, Field(min_length=1, description=_PATH_DESCRIPTION)],
    query: Annotated[
        str, Field(min_length=1, description="Искомая фраза (регистронезависимо).")
    ],
    ocr_enabled: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
    *,
    cfg: Annotated[DocToolSection, Injected],
) -> TableResult:
    """Найти фразу в документе: страница, смещение, сниппет; у pdf — координаты."""
    run = DocRun(cfg, ocr_enabled=ocr_enabled)
    hits, limit_reached = await asyncio.to_thread(run.search, path, query)

    rows: list[dict[str, Any]] = []
    for hit in hits:
        rows.append(asdict(hit))

    note = f"{path}: matches {len(rows)}"
    if limit_reached:
        note += " (search_max_matches limit reached)"

    return TableResult(
        rows=rows,
        note=note,
        metadata={"path": path, "query": query},
    )


EXPECTED: Mapping[type[Exception], DocErrorKind] = {
    DocumentError: DocErrorKind.DOCUMENT_UNREADABLE,
    OcrUnavailableError: DocErrorKind.OCR_UNAVAILABLE,
}

TOOLS: Final = ToolMain.toolset(read_document, document_outline, search_document)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
