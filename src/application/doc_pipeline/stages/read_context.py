"""Стадия 3: Чтение расширенного контекста из файлов по найденным позициям."""
from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Iterator, List

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import ContextReady, DocPipelineEvent
from domain.doc_search import SearchHit
from domain.pipeline import StageCompleted, StageStarted

log = logging.getLogger(__name__)


class ReadContextStage:
    """Читает файлы вокруг найденных чанков, расширяя контекст."""

    @property
    def name(self) -> str:
        return "read_context"

    def run(self, ctx: DocPipelineContext) -> Iterator[DocPipelineEvent]:
        yield StageStarted(stage=self.name)

        if not ctx.hits:
            ctx.expanded_context = ""
            yield ContextReady(context="", sources=[])
            yield StageCompleted(stage=self.name, detail="нет результатов поиска")
            return

        grouped = _group_by_file(ctx.hits)

        blocks: List[str] = []
        sources: List[str] = []

        for filename, hits in grouped.items():
            file_path = ctx.context_file_path(filename)
            if not file_path.exists():
                continue

            ranges = [
                (
                    max(1, hit.location.start_line - ctx.context_expand_lines),
                    hit.location.end_line + ctx.context_expand_lines,
                )
                for hit in hits
            ]

            extracted = _extract_line_ranges(file_path, ranges)

            for hit, text in zip(hits, extracted):
                label = hit.location.label
                sources.append(label)
                blocks.append(
                    f"### Источник: {label}\n"
                    f"(секция: {hit.location.section_title}, релевантность: {hit.score:.2f})\n\n"
                    f"{text}"
                )

        ctx.expanded_context = "\n\n---\n\n".join(blocks)
        yield ContextReady(context=ctx.expanded_context, sources=sources)
        yield StageCompleted(
            stage=self.name,
            detail=f"{len(blocks)} фрагментов из {len(grouped)} файлов",
        )


def _group_by_file(hits: List[SearchHit]) -> dict[str, List[SearchHit]]:
    """Группировать хиты по имени файла, сохраняя порядок по score."""
    grouped: dict[str, List[SearchHit]] = defaultdict(list)
    for hit in hits:
        grouped[hit.location.source_file].append(hit)
    return dict(grouped)


def _extract_line_ranges(
    file_path: Path, ranges: list[tuple[int, int]],
) -> list[str]:
    """Извлечь несколько диапазонов строк за один проход по файлу.

    Читает файл строка за строкой, собирая только нужные диапазоны.
    Не загружает весь файл в память.
    """
    max_end = max(end for _, end in ranges)
    collectors: list[list[str]] = [[] for _ in ranges]

    with open(file_path, encoding="utf-8", errors="replace") as fh:
        for line_num, line in enumerate(fh, start=1):
            if line_num > max_end:
                break
            for i, (start, end) in enumerate(ranges):
                if start <= line_num <= end:
                    collectors[i].append(line.rstrip("\n"))

    return ["\n".join(lines) for lines in collectors]
