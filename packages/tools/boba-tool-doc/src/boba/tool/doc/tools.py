"""Инструменты doc: чтение и поиск по загруженным документам (liteparse)."""

from __future__ import annotations

from typing import Annotated, Any

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.tool.doc.config import DocToolsConfig
from boba.tool.doc.engine import DocEngine
from boba.toolkit.launcher import LauncherFactory
from boba.toolkit.result import TableResult, TextResult, ToolResult, pack_result

__all__ = ["build_doc_tools"]

_PATH_DESCRIPTION = (
    "Путь к файлу в /workspace, например "
    "'/workspace/<thread_id>/upload/report.pdf'. Не URL: для веб-страниц "
    "есть web_fetch."
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


class DocText:
    """Пометка об обрезке: LLM должна видеть, что текст неполный."""

    @staticmethod
    def mark(text: str, truncated: bool, limit: int) -> str:
        if not truncated:
            return text
        return f"{text}\n\n[обрезано до {limit} символов]"

    @staticmethod
    def search_note(path: str, matches: int, limit_reached: bool) -> str:
        note = f"{path}: совпадений {matches}"
        if limit_reached:
            note += " (достигнут лимит search_max_matches)"
        return note


def build_doc_tools(
    cfg: DocToolsConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    engine = DocEngine(cfg, launchers)

    @tool(response_format="content_and_artifact")
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
        num_workers: Annotated[
            int, Field(ge=1, le=4, description=_WORKERS_DESCRIPTION)
        ] = 1,
        ocr_language: Annotated[
            str, Field(min_length=1, description=_LANGUAGE_DESCRIPTION)
        ] = "rus+eng",
    ) -> tuple[str, ToolResult]:
        """Прочитать текст страниц документа из workspace; основной способ чтения."""
        answer = await engine.read_document(
            path, pages, ocr_enabled, num_workers, ocr_language
        )
        text = DocText.mark(answer.text, answer.truncated, cfg.max_text_chars)
        parsed_pages: list[str] = []
        for page in answer.pages:
            parsed_pages.append(str(page))
        return pack_result(
            TextResult(
                text=text,
                metadata={
                    "path": path,
                    "pages": ",".join(parsed_pages),
                    "truncated": str(answer.truncated),
                },
            )
        )

    @tool(response_format="content_and_artifact")
    async def document_outline(
        path: Annotated[str, Field(min_length=1, description=_PATH_DESCRIPTION)],
        ocr_enabled: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
        num_workers: Annotated[
            int, Field(ge=1, le=4, description=_WORKERS_DESCRIPTION)
        ] = 1,
        ocr_language: Annotated[
            str, Field(min_length=1, description=_LANGUAGE_DESCRIPTION)
        ] = "rus+eng",
    ) -> tuple[str, ToolResult]:
        """Карта документа по страницам: дешёвый обзор перед read_document."""
        answer = await engine.outline(path, ocr_enabled, num_workers, ocr_language)
        rows: list[dict[str, Any]] = []
        for row in answer.rows:
            rows.append(row.model_dump())
        return pack_result(
            TableResult(
                rows=rows,
                note=f"{path}: страниц {answer.num_pages}",
                metadata={"path": path},
            )
        )

    @tool(response_format="content_and_artifact")
    async def search_document(
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
    ) -> tuple[str, ToolResult]:
        """Найти фразу в документе: страница, координаты совпадения и сниппет."""
        answer = await engine.search(
            path, query, ocr_enabled, num_workers, ocr_language
        )
        rows: list[dict[str, Any]] = []
        for row in answer.rows:
            rows.append(row.model_dump())
        note = DocText.search_note(path, len(rows), answer.limit_reached)
        return pack_result(
            TableResult(
                rows=rows,
                note=note,
                metadata={"path": path, "query": query},
            )
        )

    return [
        read_document,
        document_outline,
        search_document,
    ]
