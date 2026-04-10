"""Стадия 3: Чтение расширенного контекста из файлов по найденным позициям.

Чтение фрагментов — через markdown_reader (общая логика с индексацией).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterator, List

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import ContextReady, DocPipelineEvent
from application.doc_pipeline.doc_reader import registry
from domain.doc_search import Fragment, SearchHit
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
            yield ContextReady(context="", fragments=[])
            yield StageCompleted(stage=self.name, detail="нет результатов поиска")
            return

        grouped = _group_by_file(ctx.hits)
        fragments: List[Fragment] = []

        for filename, hits in grouped.items():
            file_path = ctx.source_file_path(filename)
            if not file_path.exists():
                continue

            for hit in hits:
                fragments.append(registry.read_fragment(file_path, hit, ctx.context_expand_lines))

        ctx.expanded_context = "\n\n---\n\n".join(
            _format_fragment(f) for f in fragments
        )
        yield ContextReady(context=ctx.expanded_context, fragments=fragments)
        yield StageCompleted(
            stage=self.name,
            detail=f"{len(fragments)} фрагментов из {len(grouped)} файлов",
        )


def _format_fragment(fragment: Fragment) -> str:
    """Форматировать фрагмент для контекста LLM."""
    loc = fragment.hit.location
    return (
        f"### Источник: {fragment.read_label}\n"
        f"(секция: {loc.section_title}, "
        f"чанк: {loc.label}, "
        f"релевантность: {fragment.hit.score:.2f})\n\n"
        f"{fragment.text}"
    )


def _group_by_file(hits: List[SearchHit]) -> dict[str, List[SearchHit]]:
    """Группировать хиты по имени файла, сохраняя порядок по score."""
    grouped: dict[str, List[SearchHit]] = defaultdict(list)
    for hit in hits:
        grouped[hit.location.source_file].append(hit)
    return dict(grouped)
