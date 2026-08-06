"""StructuralChunker — heading-aware chunker с replication header.

Format-нейтральный (T=str) чанкер, который потребляет FormatPlan от
Section.to_format_plan():

- держит стек активных headings per source_id и пишет полный путь в
  SectionKeys.HEADING_PATH;
- prepend-ит prefix + plan.repeat_header к каждому чанку секции;
- append-ит plan.repeat_footer (закрытие code-fence);
- мерджит Section.metadata с Section.to_chunk_metadata();
- делегирует overflow-split инжектируемому Splitter[str] (типично —
  OverlapCharSplitter); под extra_overhead чанкер пересоздаёт splitter
  через splitter_factory, чтобы итоговый chunk влез в budget.

raw_content каждого чанка собирается из FormatBlock.raw_content-ов,
которые покрываются split-piece'ом (по location в склеенном body).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable
from typing import Final

from boba.indexing import (
    Chunk,
    Chunker,
    ChunkerId,
    ChunkIdGenerator,
    FormatPlan,
    KeyEncoder,
    Section,
    SectionKeys,
    SourceId,
    SplitPiece,
    Splitter,
)

__all__ = ["SplitterFactory", "StructuralChunker"]


SplitterFactory = Callable[[int], Splitter[str]]
"""extra_overhead -> Splitter[str]. Создаёт splitter, у которого эффективный
budget уменьшен на размер prefix + repeat_header + repeat_footer."""


class StructuralChunker(Chunker[str]):
    """
    Собирает чанкер: режет документ по заголовкам,
    длинные секции добивает разбиением с перекрытием по символам
    до заданного размера (overlap)

    content_hasher считает SHA-256 по содержимому чанка
        хэш позволяет IndexSink решить, нужно ли повторно индексировать чанкт

    chunk_id_generator считает SHA-256 по хэш по source_id с префиксом
        chunk_id уникален в рамках всей таблицы kb
    """

    DEFAULT_BREADCRUMB_SEPARATOR: Final[str] = " › "

    def __init__(
        self,
        chunker_id: ChunkerId,
        splitter_factory: SplitterFactory,
        chunk_id_generator: ChunkIdGenerator[str],
        content_hasher: KeyEncoder[str],
        breadcrumb_separator: str = DEFAULT_BREADCRUMB_SEPARATOR,
    ) -> None:
        self._chunker_id = chunker_id
        self._splitter_factory = splitter_factory
        self._id_strategy = chunk_id_generator
        self._content_hasher = content_hasher
        self._breadcrumb_separator = breadcrumb_separator

    def chunker_id(self) -> ChunkerId:
        return self._chunker_id

    async def chunk(
        self,
        sections: AsyncIterable[Section[str]],
    ) -> AsyncIterator[Chunk[str]]:
        breadcrumbs: list[tuple[int, str]] = []
        per_source_index: dict[str, int] = {}
        prev_source: SourceId | None = None

        async for section in sections:
            if prev_source is None or prev_source != section.source_id:
                breadcrumbs.clear()
                prev_source = section.source_id

            plan = section.to_format_plan()
            self._update_breadcrumbs(breadcrumbs, plan)

            if not plan.blocks:
                continue

            is_heading = plan.breadcrumb_level is not None
            prefix = (
                ""
                if is_heading or not breadcrumbs
                else self._render_breadcrumbs(breadcrumbs) + "\n\n"
            )

            base_meta = section.metadata.merge(section.to_chunk_metadata())
            if breadcrumbs:
                base_meta = base_meta.set(
                    SectionKeys.HEADING_PATH,
                    self._render_breadcrumbs(breadcrumbs),
                )

            for fc, rc in self._compose_chunks(plan, prefix):
                key = section.source_id
                idx = per_source_index.get(key, 0)
                per_source_index[key] = idx + 1
                yield Chunk[str](
                    chunk_id=self._id_strategy.compute(section, idx),
                    source_id=section.source_id,
                    format_content=fc,
                    raw_content=rc,
                    chunk_index=idx,
                    content_hash=self._content_hasher.encode(fc),
                    metadata=base_meta,
                    tags=section.tags,
                )

    @staticmethod
    def _update_breadcrumbs(
        stack: list[tuple[int, str]],
        plan: FormatPlan,
    ) -> None:
        if plan.breadcrumb_level is None or plan.breadcrumb_text is None:
            return
        while stack and stack[-1][0] >= plan.breadcrumb_level:
            stack.pop()
        stack.append((plan.breadcrumb_level, plan.breadcrumb_text))

    def _render_breadcrumbs(self, stack: list[tuple[int, str]]) -> str:
        return self._breadcrumb_separator.join(text for _, text in stack)

    def _compose_chunks(
        self,
        plan: FormatPlan,
        prefix: str,
    ) -> Iterable[tuple[str, str]]:
        body, raw_starts, raws = self._build_body(plan)
        overhead = len(prefix) + len(plan.repeat_header) + len(plan.repeat_footer)
        splitter = self._splitter_factory(overhead)
        pieces = list(splitter.split(body))
        if not pieces:
            # Splitter ничего не вернул на непустом body — обернём целиком,
            # чтобы не потерять секцию.
            yield (
                prefix + plan.repeat_header + body + plan.repeat_footer,
                self._join_raws(raws),
            )
            return
        for piece in pieces:
            fc = prefix + plan.repeat_header + piece.content + plan.repeat_footer
            rc = self._raw_for_piece(piece, raw_starts, raws)
            yield fc, rc

    @staticmethod
    def _build_body(plan: FormatPlan) -> tuple[str, list[int], list[str]]:
        """Склеить body через plan.block_glue; вернуть body, индекс начал
        блоков в body и параллельный список raw_content.
        """
        body_parts: list[str] = []
        raw_starts: list[int] = []
        raws: list[str] = []
        cursor = 0
        glue = plan.block_glue
        for i, block in enumerate(plan.blocks):
            if i > 0:
                body_parts.append(glue)
                cursor += len(glue)
            raw_starts.append(cursor)
            raws.append(block.raw_content)
            body_parts.append(block.format_content)
            cursor += len(block.format_content)
        return "".join(body_parts), raw_starts, raws

    @staticmethod
    def _raw_for_piece(
        piece: SplitPiece[str],
        raw_starts: list[int],
        raws: list[str],
    ) -> str:
        """Найти block-индексы, покрытые split-piece'ом, и склеить их raw."""
        if not raws:
            return ""
        lo = piece.location.start
        hi = piece.location.end
        # Первый блок, чей start <= lo. bisect_right возвращает индекс
        # ПОСЛЕ всех равных — поэтому −1.
        first = max(0, bisect_right(raw_starts, lo) - 1)
        last = max(first, bisect_right(raw_starts, max(hi - 1, lo)) - 1)
        if first == last:
            return raws[first]
        return "\n\n".join(raws[first : last + 1])

    @staticmethod
    def _join_raws(raws: list[str]) -> str:
        return "\n\n".join(raws)
