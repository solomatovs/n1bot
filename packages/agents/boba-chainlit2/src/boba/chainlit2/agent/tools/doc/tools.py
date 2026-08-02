"""Инструменты doc: чтение и поиск по загруженным документам (liteparse).

Порт boba.tool.doc: тот же набор tool'ов, но файл берётся из workspace-образа
пользователя, а результат упаковывается в ToolResult chainlit2.
"""

from __future__ import annotations

from typing import Annotated, Any

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.chainlit2.agent.tools.doc.config import DocToolsConfig
from boba.chainlit2.agent.tools.doc.engine import DocEngine
from boba.chainlit2.rendering.render import pack_result
from boba.chainlit2.rendering.tool_result import TableResult, TextResult, ToolResult
from boba.liteparse import LiteParseEngine

__all__ = ["build_doc_tools"]

_PATH_DESCRIPTION = (
    "Путь к файлу в /workspace, например "
    "'/workspace/<thread_id>/upload/report.pdf'. Не URL: для веб-страниц "
    "есть web_fetch."
)


class DocSearch:
    """Совпадения через нативный search_items; сниппет — из текста страницы."""

    ELLIPSIS = "…"

    @classmethod
    def run(
        cls,
        native_result: Any,
        query: str,
        context: int,
        max_matches: int,
    ) -> list[dict[str, Any]]:
        needle = query.casefold()
        rows: list[dict[str, Any]] = []
        for page in native_result.pages:
            hits = LiteParseEngine.search_items(
                page.text_items, query, case_sensitive=False
            )
            if not hits:
                continue
            hay = page.text.casefold()
            cursor = 0
            for hit in hits:
                index = hay.find(needle, cursor)
                if index == -1:
                    snippet = hit.text
                else:
                    snippet = cls._snippet(
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
                    return rows
        return rows

    @classmethod
    def _snippet(cls, text: str, lo: int, hi: int, context: int) -> str:
        begin = max(0, lo - context)
        end = min(len(text), hi + context)
        prefix = ""
        if begin > 0:
            prefix = cls.ELLIPSIS
        suffix = ""
        if end < len(text):
            suffix = cls.ELLIPSIS
        return f"{prefix}{text[begin:end]}{suffix}"


class DocText:
    """Обрезка текста до лимита с явной пометкой для LLM."""

    @staticmethod
    def clip(text: str, limit: int) -> tuple[str, bool]:
        clipped, truncated = DocEngine.clip(text, limit)
        if truncated:
            clipped += f"\n\n[обрезано до {limit} символов]"
        return clipped, truncated


def build_doc_tools(cfg: DocToolsConfig) -> list[BaseTool]:
    engine = DocEngine(cfg)

    @tool(response_format="content_and_artifact")
    async def read_document(
        path: Annotated[str, Field(min_length=1, description=_PATH_DESCRIPTION)],
    ) -> tuple[str, ToolResult]:
        """Распарсить документ из workspace и вернуть весь извлечённый текст.

        Текст обрезается до max_text_chars; число страниц и факт обрезки — в
        metadata. Для отдельных страниц есть read_pages, для обзора —
        document_outline.
        """
        result = await engine.parse(path)
        text, truncated = DocText.clip(result.text, cfg.max_text_chars)
        return pack_result(
            TextResult(
                text=text,
                metadata={
                    "path": path,
                    "pages": str(result.num_pages),
                    "truncated": str(truncated),
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
        result = await engine.parse(path, target_pages=pages)
        text, truncated = DocText.clip(result.text, cfg.max_text_chars)
        parsed_pages: list[str] = []
        for page in result.pages:
            parsed_pages.append(str(page.page_num))
        return pack_result(
            TextResult(
                text=text,
                metadata={
                    "path": path,
                    "pages": ",".join(parsed_pages),
                    "truncated": str(truncated),
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
        result = await engine.parse(path)
        chunk, end_char, total, has_more = DocEngine.window(
            result.text, start_char, length
        )
        return pack_result(
            TextResult(
                text=chunk,
                metadata={
                    "path": path,
                    "start_char": str(start_char),
                    "end_char": str(end_char),
                    "total_chars": str(total),
                    "has_more": str(has_more),
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
        result = await engine.parse(path)
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
        return pack_result(
            TableResult(
                rows=rows,
                note=f"{path}: страниц {result.num_pages}",
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
        native = await engine.parse_native(path)
        rows = DocSearch.run(
            native,
            query,
            cfg.search_context_chars,
            cfg.search_max_matches,
        )
        note = f"{path}: совпадений {len(rows)}"
        if len(rows) >= cfg.search_max_matches:
            note += " (достигнут лимит search_max_matches)"
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
