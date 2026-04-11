"""Конкретные инструменты агентного режима.

Каждый инструмент — генератор: yield'ит события по мере выполнения,
финальный yield — ToolResult с текстом для LLM.

Добавление нового: создать класс, добавить в create_tool_registry().
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterator

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.doc_reader import registry as doc_reader_registry
from application.doc_pipeline.events import (
    ContextReady,
    FileIndexed,
    IndexingDone,
    IndexingSkipped,
    SearchDone,
)
from application.index_pipeline import (
    IndexContext,
    run_indexing,
)
from application.index_pipeline import (
    FileCompleted as IdxFileCompleted,
    IndexingDone as IdxDone,
    IndexingSkipped as IdxSkipped,
)
from domain.doc_search import ChunkLocation, Fragment, SearchHit
from domain.errors import CorruptedIndexError
from domain.tools import Tool, ToolRegistry, ToolResult

log = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 4000
_REQUIRED_META_FIELDS = ("source_file", "start_line", "end_line", "start_offset", "end_offset")


# ---------------------------------------------------------------------------
# index_documents
# ---------------------------------------------------------------------------

class IndexDocumentsTool(Tool):
    """Индексация документов для последующего поиска."""

    def __init__(self, ctx: DocPipelineContext) -> None:
        self._ctx = ctx

    @property
    def name(self) -> str:
        return "index_documents"

    @property
    def description(self) -> str:
        return (
            "Индексация документов для последующего поиска. "
            "Вызови ПЕРЕД search_documents, если документы ещё не проиндексированы "
            "или если search_documents ничего не находит. "
            "Индексация проверяет изменения — если документы не менялись, пропускает."
        )

    @property
    def parameters(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs: Any) -> Iterator[ToolResult | Any]:
        ctx = self._ctx
        idx_ctx = IndexContext(
            source_path=ctx.source_path,
            manifest_path=ctx.manifest_path,
            collection_name=ctx.collection_name,
            embedding_model=ctx.embedding_model,
            vectorstore_service=ctx.vectorstore_service,
        )

        total_files = 0
        total_chunks = 0

        for event in run_indexing(idx_ctx):
            match event:
                case IdxSkipped(collection=c, doc_count=n):
                    yield IndexingSkipped(collection=c, doc_count=n)
                    yield ToolResult(content=f"Индекс актуален ({n} чанков), переиндексация не нужна.")
                    return

                case IdxFileCompleted(filename=f, chunks=c, index=i, total=t):
                    total_files = i
                    total_chunks += c
                    yield FileIndexed(filename=f, chunks=c, index=i, total=t)

                case IdxDone(total_files=f, total_chunks=c):
                    total_files = f
                    total_chunks = c
                    yield IndexingDone(total_files=f, total_chunks=c)

        yield ToolResult(content=f"Индексация завершена: {total_files} файлов, {total_chunks} чанков.")


# ---------------------------------------------------------------------------
# search_documents
# ---------------------------------------------------------------------------

class SearchDocumentsTool(Tool):
    """Векторный поиск по проиндексированным документам."""

    def __init__(self, ctx: DocPipelineContext) -> None:
        self._ctx = ctx

    @property
    def name(self) -> str:
        return "search_documents"

    @property
    def description(self) -> str:
        return (
            "Поиск релевантных фрагментов в документах по запросу. "
            "Используй для нахождения информации по теме вопроса. "
            "Можно вызывать несколько раз с разными запросами."
        )

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Количество результатов (по умолчанию 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    def execute(self, *, query: str, top_k: int = 5, **kwargs: Any) -> Iterator[ToolResult | Any]:
        ctx = self._ctx
        results = ctx.vectorstore_service.search_with_scores(
            ctx.collection_name, query, top_k,
        )

        hits: list[SearchHit] = []
        for scored in results:
            hits.append(SearchHit(
                content=scored.document.page_content,
                location=_parse_location(scored.document.metadata),
                score=scored.score,
            ))

        ctx.hits = hits
        yield SearchDone(hits=hits)

        if not hits:
            yield ToolResult(content="Ничего не найдено по запросу.")
            return

        parts = (
            f"[{i}] {h.location.source_file}:{h.location.start_line}-{h.location.end_line} "
            f"(секция: {h.location.section_title}, score: {h.score:.2f})\n{h.content}"
            for i, h in enumerate(hits, 1)
        )
        text = "\n\n---\n\n".join(parts)
        yield ToolResult(content=text[:_MAX_RESULT_CHARS])


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

class ReadFileTool(Tool):
    """Чтение содержимого документа (целиком или диапазон строк)."""

    def __init__(self, ctx: DocPipelineContext) -> None:
        self._ctx = ctx

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Прочитать содержимое документа. "
            "Можно указать диапазон строк для чтения части файла. "
            "Используй после search_documents для получения полного контекста."
        )

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Имя файла (из результатов search_documents или list_files)",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Начальная строка (опционально, нумерация с 1)",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Конечная строка (опционально)",
                },
            },
            "required": ["filename"],
        }

    def execute(
        self,
        *,
        filename: str,
        start_line: int | None = None,
        end_line: int | None = None,
        **kwargs: Any,
    ) -> Iterator[ToolResult | Any]:
        file_path = self._ctx.source_file_path(filename)

        if not file_path.exists():
            yield ToolResult(content=f"Файл не найден: {filename}")
            return

        if start_line is not None and end_line is not None:
            text = _read_line_range(file_path, start_line, end_line)
            label = f"{filename}:{start_line}-{end_line}"
        else:
            text = file_path.read_text(encoding="utf-8", errors="replace")
            label = filename

        text = text[:_MAX_RESULT_CHARS]

        fragment = Fragment(
            text=text,
            hit=SearchHit(
                content="",
                location=ChunkLocation(
                    source_file=filename,
                    start_line=start_line or 1,
                    end_line=end_line or text.count("\n") + 1,
                    start_offset=0,
                    end_offset=len(text.encode("utf-8")),
                ),
            ),
            read_start_line=start_line or 1,
            read_end_line=end_line or text.count("\n") + 1,
            read_start_offset=0,
            read_end_offset=len(text.encode("utf-8")),
        )
        yield ContextReady(context=text, fragments=[fragment])
        yield ToolResult(content=f"### {label}\n\n{text}")


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------

class ListFilesTool(Tool):
    """Список доступных документов в папке."""

    def __init__(self, ctx: DocPipelineContext) -> None:
        self._ctx = ctx

    @property
    def name(self) -> str:
        return "list_files"

    @property
    def description(self) -> str:
        return (
            "Показать список доступных документов в папке. "
            "Используй, когда нужно узнать какие файлы есть."
        )

    @property
    def parameters(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs: Any) -> Iterator[ToolResult | Any]:
        lines = []
        for f in doc_reader_registry.iter_files(self._ctx.source_path):
            lines.append(f"- {f.name}")

        if not lines:
            yield ToolResult(content="В папке нет поддерживаемых документов.")
            return

        yield ToolResult(
            content=f"Доступные документы ({len(lines)}):\n" + "\n".join(lines),
        )


# ---------------------------------------------------------------------------
# Фабрика реестра
# ---------------------------------------------------------------------------

def create_tool_registry(ctx: DocPipelineContext) -> ToolRegistry:
    """Создать реестр со всеми доступными инструментами.

    Добавление нового инструмента — одна строка здесь.
    """
    return ToolRegistry([
        IndexDocumentsTool(ctx),
        SearchDocumentsTool(ctx),
        ReadFileTool(ctx),
        ListFilesTool(ctx),
    ])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_location(meta: dict) -> ChunkLocation:
    """Валидировать метаданные и собрать ChunkLocation."""
    missing = [f for f in _REQUIRED_META_FIELDS if f not in meta]
    if missing:
        raise CorruptedIndexError(
            missing_fields=missing,
            source_file=meta.get("source_file", ""),
        )
    return ChunkLocation(
        source_file=meta["source_file"],
        start_line=meta["start_line"],
        end_line=meta["end_line"],
        start_offset=meta["start_offset"],
        end_offset=meta["end_offset"],
        section_title=meta.get("section_title", ""),
    )


def _read_line_range(file_path, start_line: int, end_line: int) -> str:
    """Прочитать диапазон строк из файла (1-based)."""
    lines: list[str] = []
    with open(file_path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            if i > end_line:
                break
            if i >= start_line:
                lines.append(line.rstrip("\n"))
    return "\n".join(lines)
