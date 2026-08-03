"""Инструменты doc: чтение и поиск по загруженным документам (liteparse).

Документ парсится payload'ом внутри песочницы; здесь остаётся только
описание инструментов для LLM и упаковка ответа в ToolResult.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated, Any

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.tool.doc.config import DocToolsConfig
from boba.tool.doc.engine import DocEngine
from boba.toolkit.pack import pack_result
from boba.toolkit.result import TableResult, TextResult, ToolResult

__all__ = ["build_doc_tools"]

_PATH_DESCRIPTION = (
    "Путь к файлу в /workspace, например "
    "'/workspace/<thread_id>/upload/report.pdf'. Не URL: для веб-страниц "
    "есть web_fetch."
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
    path_vars: Callable[[], Mapping[str, str]],
) -> list[BaseTool]:
    engine = DocEngine(cfg, path_vars)

    @tool(response_format="content_and_artifact")
    async def read_document(
        path: Annotated[str, Field(min_length=1, description=_PATH_DESCRIPTION)],
    ) -> tuple[str, ToolResult]:
        """Распарсить документ из workspace и вернуть весь извлечённый текст.

        Текст обрезается до max_text_chars; число страниц и факт обрезки — в
        metadata. Для отдельных страниц есть read_pages, для обзора —
        document_outline.
        """
        answer = await engine.read_document(path)
        text = DocText.mark(answer.text, answer.truncated, cfg.max_text_chars)
        return pack_result(
            TextResult(
                text=text,
                metadata={
                    "path": path,
                    "pages": str(answer.num_pages),
                    "truncated": str(answer.truncated),
                },
            )
        )

    @tool(response_format="content_and_artifact")
    async def read_pages(
        path: Annotated[str, Field(min_length=1, description=_PATH_DESCRIPTION)],
        pages: Annotated[
            str,
            Field(
                min_length=1,
                description=(
                    "Страницы, 1-based: диапазоны и перечисление через "
                    "запятую, например '1-5,10,15-20'."
                ),
            ),
        ],
    ) -> tuple[str, ToolResult]:
        """Вернуть текст только указанных страниц документа.

        Дешевле read_document для больших PDF: парсятся лишь нужные страницы.
        """
        answer = await engine.read_pages(path, pages)
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
    async def read_document_window(
        path: Annotated[str, Field(min_length=1, description=_PATH_DESCRIPTION)],
        start_char: Annotated[
            int, Field(ge=0, description="Смещение начала окна в символах, 0-based.")
        ],
        length: Annotated[
            int, Field(ge=1, description="Сколько символов вернуть от start_char.")
        ],
    ) -> tuple[str, ToolResult]:
        """Вернуть срез текста документа [start_char, start_char+length).

        Для последовательного чтения большого документа порциями: увеличивай
        start_char на длину прочитанного, пока metadata.has_more == 'True'.
        """
        if length > cfg.max_text_chars:
            msg = (
                f"length ({length}) exceeds max_text_chars "
                f"({cfg.max_text_chars}): read in smaller windows"
            )
            raise RuntimeError(msg)
        answer = await engine.read_window(path, start_char, length)
        return pack_result(
            TextResult(
                text=answer.text,
                metadata={
                    "path": path,
                    "start_char": str(answer.start_char),
                    "end_char": str(answer.end_char),
                    "total_chars": str(answer.total_chars),
                    "has_more": str(answer.has_more),
                },
            )
        )

    @tool(response_format="content_and_artifact")
    async def document_outline(
        path: Annotated[str, Field(min_length=1, description=_PATH_DESCRIPTION)],
    ) -> tuple[str, ToolResult]:
        """Карта документа: по строке на страницу (размер, символы, фрагменты).

        Дешёвый обзор перед чтением: по нему выбирают страницы для read_pages.
        """
        answer = await engine.outline(path)
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
    ) -> tuple[str, ToolResult]:
        """Найти фразу в документе: страница, координаты совпадения и сниппет."""
        answer = await engine.search(path, query)
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
        read_pages,
        read_document_window,
        document_outline,
        search_document,
    ]
