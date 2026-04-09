"""Стадия 1: Индексация файлов из папки.

Читает .md файлы, чанкирует по секциям (строка за строкой),
сохраняет батчами через VectorStoreService.
Пропускает, если коллекция уже существует.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator, List

from langchain_core.documents import Document

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import (
    DocPipelineEvent,
    FileIndexed,
    IndexingDone,
    IndexingSkipped,
)
from domain.pipeline import StageCompleted, StageStarted

log = logging.getLogger(__name__)

_INDEXABLE_EXTENSIONS = {".md", ".txt"}
_HEADING_PATTERN = re.compile(r"^(#+)\s+(.+)$")
_STORE_BATCH_SIZE = 32


class IndexStage:
    """Индексирует файлы из папки через VectorStoreService."""

    @property
    def name(self) -> str:
        return "index"

    def run(self, ctx: DocPipelineContext) -> Iterator[DocPipelineEvent]:
        yield StageStarted(stage=self.name)

        vs = ctx.vectorstore_service

        doc_count = vs.collection_doc_count(ctx.collection_name)
        if doc_count > 0:
            yield IndexingSkipped(
                collection=ctx.collection_name, doc_count=doc_count,
            )
            yield StageCompleted(stage=self.name, detail=f"индекс существует ({doc_count} чанков)")
            return

        vs.create_collection(ctx.collection_name, ctx.embedding_model)

        file_count = _count_files(ctx.context_path)
        if file_count == 0:
            yield IndexingDone(total_files=0, total_chunks=0)
            yield StageCompleted(stage=self.name, detail="нет файлов для индексации")
            return

        total_files = 0
        total_chunks = 0
        batch: List[Document] = []

        for file_path in _iter_files(ctx.context_path):
            total_files += 1
            file_chunks = 0

            for chunk in _iter_chunks(file_path):
                batch.append(chunk)
                file_chunks += 1

                if len(batch) >= _STORE_BATCH_SIZE:
                    vs.store_batch(ctx.collection_name, batch)
                    batch.clear()

            total_chunks += file_chunks
            yield FileIndexed(
                filename=file_path.name,
                chunks=file_chunks,
                index=total_files,
                total=file_count,
            )

        if batch:
            vs.store_batch(ctx.collection_name, batch)
            batch.clear()

        yield IndexingDone(total_files=total_files, total_chunks=total_chunks)
        yield StageCompleted(stage=self.name, detail=f"{total_files} файлов, {total_chunks} чанков")


# ---------------------------------------------------------------------------
# Файловые итераторы
# ---------------------------------------------------------------------------

def _count_files(folder: Path) -> int:
    """Подсчитать количество индексируемых файлов (быстрый проход)."""
    if not folder.is_dir():
        return 0
    return sum(1 for f in folder.iterdir() if _is_indexable(f))


def _iter_files(folder: Path) -> Iterator[Path]:
    """Лениво итерировать индексируемые файлы в отсортированном порядке."""
    if not folder.is_dir():
        return
    for f in sorted(folder.iterdir(), key=lambda p: p.name):
        if _is_indexable(f):
            yield f


def _is_indexable(f: Path) -> bool:
    return f.is_file() and f.suffix.lower() in _INDEXABLE_EXTENSIONS and not f.name.startswith(".")


# ---------------------------------------------------------------------------
# Потоковый чанкинг — строка за строкой
# ---------------------------------------------------------------------------

def _iter_chunks(file_path: Path) -> Iterator[Document]:
    """Разбить файл на чанки, читая строка за строкой.

    Yield-ит Document по мере обнаружения границ секций.
    Не загружает весь файл в память.
    """
    section_title = ""
    section_lines: List[str] = []
    section_start = 1
    line_number = 0

    with open(file_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line_number += 1
            line = line.rstrip("\n")
            heading_match = _HEADING_PATTERN.match(line.strip())

            if heading_match:
                if section_lines:
                    chunk = _build_chunk(
                        file_path.name, section_title, section_lines,
                        section_start, line_number - 1,
                    )
                    if chunk is not None:
                        yield chunk
                    section_lines = []
                section_title = heading_match.group(2)
                section_start = line_number
                section_lines.append(line)
            elif not line.strip() and section_lines:
                section_lines.append(line)
            else:
                if not section_lines:
                    section_start = line_number
                section_lines.append(line)

    if section_lines:
        chunk = _build_chunk(
            file_path.name, section_title, section_lines,
            section_start, line_number,
        )
        if chunk is not None:
            yield chunk


def _build_chunk(
    filename: str,
    section_title: str,
    lines: List[str],
    start_line: int,
    end_line: int,
) -> Document | None:
    """Собрать Document из накопленных строк секции. None если пустой контент."""
    content = "\n".join(lines).strip()
    if not content:
        return None
    return Document(
        page_content=content,
        metadata={
            "source_file": filename,
            "start_line": start_line,
            "end_line": end_line,
            "section_title": section_title,
        },
    )
