"""StructuralChunker — generic per-section-type chunker.

Принимает поток типизированных `Section[str]` (HeadingSection, ParagraphSection,
CodeFenceSection, TableSection, ListSection, BlockquoteSection,
HorizontalRuleSection или пользовательский подкласс Section)
и применяет per-type стратегии:

- `HeadingSection`        → prefix-merge: heading НЕ эмитится отдельным чанком;
  HEADING_LEVEL/HEADING_TEXT идут в metadata следующей не-heading секции,
  а текст heading'а склеивается в `format_content` (для LLM-контекста).
  Standalone trailing heading (без followup) → atomic chunk.
- `ParagraphSection`      → atomic если ≤ chunk_size; иначе char-split с
  overlap через `OverlapCharSplitter`.
- `CodeFenceSection`      → atomic если влезает; иначе line-based split с
  `CODE_FENCE_LINE_RANGE` в metadata. Slice-инвариант сохранён.
- `TableSection`          → atomic если влезает; иначе row-by-row split.
  `raw_content` — slice rows из исходника; `format_content` — header +
  rows (header реплицируется в каждый чанк для LLM).
- `ListSection`           → atomic если влезает; иначе item-by-item split.
- `BlockquoteSection`     → как `ParagraphSection`.
- `HorizontalRuleSection` → skip (структурный разделитель, в чанк не идёт).
- любая другая `Section`  → fallback на paragraph-стратегию.

**Контракт `Chunk` полей**:

- `raw_content` — оригинальный slice **одной Section.content** (без синтеза
  и без heading-merge): `original[location.start:location.end] == raw_content`.
- `format_content` — версия для LLM/embedding: для большинства чанков равна
  `raw_content`, для heading-prefix-merge'нутых — `heading + raw`, для
  table-row'ов — `header + rows`.
- `location` — offset чанка в исходном raw документе (всегда совпадает с
  диапазоном внутри родительской `Section.location`).
- `section_location` — `Section.location` целиком.
- `section_type` — `Section.SECTION_TYPE` (canonical alias).
- `metadata` — `section.metadata + section.to_chunk_metadata() +
  prefix_heading.to_chunk_metadata() + chunker-специфичные ключи`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from boba.indexing.chunk_id import ChunkIdStrategy
from boba.indexing.chunker import Chunker, ChunkerId
from boba.indexing.chunks import Chunk
from boba.indexing.context import PipelineContext
from boba.indexing.location import ChunkLocation
from boba.indexing.metadata import Metadata, MetadataKey
from boba.indexing.sections import (
    CodeFenceSection,
    HeadingSection,
    HorizontalRuleSection,
    ListSection,
    Section,
    TableSection,
)
from boba.indexing.splitter import OverlapCharSplitter

__all__ = [
    "StructuralChunker",
    "StructuralKeys",
]


class StructuralKeys:
    """Metadata-ключи, проставляемые `StructuralChunker`."""

    OVERFLOW_REASON: ClassVar[MetadataKey[str]] = MetadataKey(
        name="chunker.structural.overflow_reason",
        decode=str,
        encode=str,
    )
    """`code_fence_too_large` | `table_too_large` | `list_too_large` —
    проставляется когда секция не влезает в chunk_size и эмитится atomic
    (без structured-split, обычно из-за отсутствия per-unit locations)."""

    TABLE_ROW_RANGE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="chunker.structural.table_row_range",
        decode=str,
        encode=str,
    )
    """`"0..3"` — какие data-строки таблицы попали в этот чанк."""

    LIST_ITEM_RANGE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="chunker.structural.list_item_range",
        decode=str,
        encode=str,
    )
    """`"0..4"` — какие items списка попали в этот чанк."""

    CODE_FENCE_LINE_RANGE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="chunker.structural.code_fence_line_range",
        decode=str,
        encode=str,
    )
    """`"0..15"` — какие строки тела code-fence попали в этот чанк."""


# Внутреннее представление одного chunk'а до сборки финального `Chunk[str]`.
# (raw_start, raw_end, raw_content, format_content, extra_metadata)
_ChunkSpec = tuple[int, int, str, str, Metadata]


class StructuralChunker(Chunker[str]):
    """Generic per-section-type chunker."""

    def __init__(
        self,
        chunker_id: ChunkerId,
        id_strategy: ChunkIdStrategy[str],
        chunk_size: int,
        chunk_overlap: int,
    ) -> None:
        self._chunker_id = chunker_id
        self._id_strategy = id_strategy
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._overflow_splitter = OverlapCharSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    def name(self) -> str:
        return f"StructuralChunker({self._chunker_id.to_wire()})"

    def chunker_id(self) -> ChunkerId:
        return self._chunker_id

    def reset(self) -> None:
        pass

    def stream(
        self,
        ctx: PipelineContext,
        stream: Iterable[Section[str]],
    ) -> Iterable[Chunk[str]]:
        del ctx
        per_source_index: dict[str, int] = {}
        for prefix_headings, main_section in self._group_with_heading_prefix(stream):
            for spec in self._chunks_for(main_section, prefix_headings):
                key = main_section.source_id.to_wire()
                idx = per_source_index.get(key, 0)
                per_source_index[key] = idx + 1
                yield self._build_chunk(main_section, prefix_headings, spec, idx)

    @staticmethod
    def _group_with_heading_prefix(
        sections: Iterable[Section[str]],
    ) -> Iterable[tuple[list[HeadingSection], Section[str]]]:
        """Yields `(prefix_headings, main_section)` пар.

        - `HeadingSection` накапливаются как префикс к следующей не-heading секции.
        - `HorizontalRuleSection` пропускается.
        - Если поток заканчивается trailing heading'ами — каждый эмитится как
          самостоятельная группа `([heading], heading)`.
        """
        pending: list[HeadingSection] = []
        for section in sections:
            if isinstance(section, HorizontalRuleSection):
                continue
            if isinstance(section, HeadingSection):
                pending.append(section)
                continue
            yield pending, section
            pending = []
        for trailing in pending:
            yield [trailing], trailing

    def _chunks_for(
        self,
        section: Section[str],
        prefix_headings: list[HeadingSection],
    ) -> Iterable[_ChunkSpec]:
        """Per-type dispatch + fallback."""
        if isinstance(section, CodeFenceSection):
            yield from self._strategy_code_fence(section, prefix_headings)
        elif isinstance(section, TableSection):
            yield from self._strategy_table(section, prefix_headings)
        elif isinstance(section, ListSection):
            yield from self._strategy_list(section, prefix_headings)
        else:
            # ParagraphSection, BlockquoteSection, HeadingSection
            # (standalone trailing) и пользовательские подклассы.
            yield from self._strategy_paragraph_like(section, prefix_headings)

    def _strategy_paragraph_like(
        self,
        section: Section[str],
        prefix_headings: list[HeadingSection],
    ) -> Iterable[_ChunkSpec]:
        """Atomic если slice ≤ chunk_size; иначе char-split с overlap.

        `format_content` первого чанка содержит heading-prefix (если есть)
        — это даёт LLM контекст «к какому разделу относится фрагмент».
        Последующие split-чанки prefix не повторяют (он избыточен; metadata
        с HEADING_LEVEL/TEXT у них уже есть от section.to_chunk_metadata).
        """
        raw_content = section.content
        prefix_text = self._prefix_text(prefix_headings)
        full_format = f"{prefix_text}\n\n{raw_content}" if prefix_text else raw_content
        loc = section.location
        if len(full_format) <= self._chunk_size:
            yield loc.start, loc.end, raw_content, full_format, Metadata.empty()
            return
        # Overflow → char-split. format-prefix кладём только в первый sub-чанк.
        first = True
        for piece in self._overflow_splitter.split(raw_content):
            sub_raw_start = loc.start + piece.location.start
            sub_raw_end = loc.start + piece.location.end
            sub_format = (
                f"{prefix_text}\n\n{piece.content}"
                if first and prefix_text
                else piece.content
            )
            empty = Metadata.empty()
            yield sub_raw_start, sub_raw_end, piece.content, sub_format, empty
            first = False

    def _strategy_code_fence(
        self,
        section: CodeFenceSection,
        prefix_headings: list[HeadingSection],
    ) -> Iterable[_ChunkSpec]:
        """Atomic если влезает; иначе line-based split."""
        raw_content = section.content
        prefix_text = self._prefix_text(prefix_headings)
        full_format = f"{prefix_text}\n\n{raw_content}" if prefix_text else raw_content
        loc = section.location
        if len(full_format) <= self._chunk_size:
            yield loc.start, loc.end, raw_content, full_format, Metadata.empty()
            return
        if not section.code_line_locations:
            meta = Metadata.empty().set(
                StructuralKeys.OVERFLOW_REASON, "code_fence_too_large"
            )
            yield loc.start, loc.end, raw_content, full_format, meta
            return
        ranges = self._pack_locations(
            unit_locations=section.code_line_locations,
            first_chunk_prefix_size=0,
            chunk_start_for_first=section.code_line_locations[0].start,
        )
        for first_idx, last_idx, c_start, c_end in ranges:
            sub_raw = self._slice(section, c_start, c_end)
            meta = Metadata.empty().set(
                StructuralKeys.CODE_FENCE_LINE_RANGE, f"{first_idx}..{last_idx}"
            )
            # format_content для overflow code-fence не реконструирует fence-маркеры
            # — это делает LLM через CODE_LANGUAGE в metadata.
            yield c_start, c_end, sub_raw, sub_raw, meta

    def _strategy_table(
        self,
        section: TableSection,
        prefix_headings: list[HeadingSection],
    ) -> Iterable[_ChunkSpec]:
        """Atomic если влезает; иначе row-by-row split с replicated header'ом
        в `format_content`.
        """
        raw_content = section.content
        prefix_text = self._prefix_text(prefix_headings)
        full_format = f"{prefix_text}\n\n{raw_content}" if prefix_text else raw_content
        loc = section.location
        if len(full_format) <= self._chunk_size:
            yield loc.start, loc.end, raw_content, full_format, Metadata.empty()
            return
        if not section.row_locations:
            meta = Metadata.empty().set(
                StructuralKeys.OVERFLOW_REASON, "table_too_large"
            )
            yield loc.start, loc.end, raw_content, full_format, meta
            return
        ranges = self._pack_locations(
            unit_locations=section.row_locations,
            first_chunk_prefix_size=section.row_locations[0].start - loc.start,
            chunk_start_for_first=loc.start,
        )
        for i, (first_idx, last_idx, c_start, c_end) in enumerate(ranges):
            sub_raw = self._slice(section, c_start, c_end)
            # Первый чанк уже несёт header в slice (с loc.start);
            # последующие — только rows, header реплицируется в format_content.
            if i == 0 or not section.header_text:
                sub_format = sub_raw
            else:
                sub_format = f"{section.header_text}\n{sub_raw}"
            meta = Metadata.empty().set(
                StructuralKeys.TABLE_ROW_RANGE, f"{first_idx}..{last_idx}"
            )
            yield c_start, c_end, sub_raw, sub_format, meta

    def _strategy_list(
        self,
        section: ListSection,
        prefix_headings: list[HeadingSection],
    ) -> Iterable[_ChunkSpec]:
        """Atomic если влезает; иначе item-by-item split."""
        raw_content = section.content
        prefix_text = self._prefix_text(prefix_headings)
        full_format = f"{prefix_text}\n\n{raw_content}" if prefix_text else raw_content
        loc = section.location
        if len(full_format) <= self._chunk_size:
            yield loc.start, loc.end, raw_content, full_format, Metadata.empty()
            return
        if not section.item_locations:
            meta = Metadata.empty().set(
                StructuralKeys.OVERFLOW_REASON, "list_too_large"
            )
            yield loc.start, loc.end, raw_content, full_format, meta
            return
        ranges = self._pack_locations(
            unit_locations=section.item_locations,
            first_chunk_prefix_size=section.item_locations[0].start - loc.start,
            chunk_start_for_first=loc.start,
        )
        for first_idx, last_idx, c_start, c_end in ranges:
            sub_raw = self._slice(section, c_start, c_end)
            meta = Metadata.empty().set(
                StructuralKeys.LIST_ITEM_RANGE, f"{first_idx}..{last_idx}"
            )
            yield c_start, c_end, sub_raw, sub_raw, meta


    def _pack_locations(
        self,
        unit_locations: tuple[ChunkLocation, ...],
        first_chunk_prefix_size: int,
        chunk_start_for_first: int,
    ) -> list[tuple[int, int, int, int]]:
        """Greedy bin-packing units в чанки ≤ chunk_size.

        Возвращает list of `(first_unit_idx, last_unit_idx, chunk_start, chunk_end)`.
        """
        ranges: list[tuple[int, int, int, int]] = []
        if not unit_locations:
            return ranges
        chunk_size = self._chunk_size
        cur_first = 0
        cur_start = chunk_start_for_first
        cur_size = first_chunk_prefix_size
        cur_last = -1
        for idx, loc in enumerate(unit_locations):
            unit_size = (loc.end - loc.start) + 1  # +1 за разделитель `\n`
            candidate = cur_size + unit_size
            if candidate > chunk_size and cur_last >= cur_first:
                ranges.append(
                    (cur_first, cur_last, cur_start, unit_locations[cur_last].end)
                )
                cur_first = idx
                cur_start = loc.start
                cur_size = unit_size
                cur_last = idx
            else:
                cur_size = candidate
                cur_last = idx
        if cur_first <= cur_last:
            ranges.append(
                (cur_first, cur_last, cur_start, unit_locations[cur_last].end)
            )
        return ranges


    def _build_chunk(
        self,
        section: Section[str],
        prefix_headings: list[HeadingSection],
        spec: _ChunkSpec,
        idx: int,
    ) -> Chunk[str]:
        raw_start, raw_end, raw_content, format_content, chunker_meta = spec
        # Heading-префикс отдаёт свои типизированные поля
        # (HEADING_LEVEL / HEADING_TEXT) в metadata чанка.
        heading_meta = (
            prefix_headings[-1].to_chunk_metadata()
            if prefix_headings
            else Metadata.empty()
        )
        merged_meta = (
            section.metadata.merge(section.to_chunk_metadata())
            .merge(heading_meta)
            .merge(chunker_meta)
        )
        return Chunk[str](
            chunk_id=self._id_strategy.compute(section, idx),
            source_id=section.source_id,
            format_content=format_content,
            raw_content=raw_content,
            location=ChunkLocation(start=raw_start, end=raw_end),
            section_location=section.location,
            section_type=section.SECTION_TYPE,
            anchor=section.anchor,
            chunk_index=idx,
            metadata=merged_meta,
            tags=section.tags,
        )



    @staticmethod
    def _slice(section: Section[str], start: int, end: int) -> str:
        """Slice по global raw-offset'ам; конвертирует в local для `section.content`."""
        local_start = start - section.location.start
        local_end = end - section.location.start
        return section.content[local_start:local_end]

    @staticmethod
    def _prefix_text(prefix_headings: list[HeadingSection]) -> str:
        """Объединяет heading-префиксы в один текст для `format_content`."""
        return "\n\n".join(h.content for h in prefix_headings)
