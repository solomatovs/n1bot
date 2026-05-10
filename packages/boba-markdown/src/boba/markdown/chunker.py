"""
MarkdownStructuralChunker: structured chunker для markdown

Использует `MarkdownBlockParser` для разбора Section.content на типизированные
блоки (heading, paragraph, code-fence, table, list, blockquote, hr, html),
затем применяет **per-block стратегию** chunking;

- **Heading**       → prefix-merge: присоединяется к следующему НЕ-heading
  блоку как префикс через slice [heading.start, next.end].
- **Paragraph**     → atomic если влезает; иначе char-split с overlap
  через `OverlapCharSplitter` (с overlap для retrieval-стабильности).
- **CodeFence**     → atomic. Если > chunk_size — один чанк-overflow с
  `OVERFLOW_REASON = "code_fence_too_large"`. Не режется (сохраняет fence).
- **Table**         → atomic. Если > chunk_size — overflow с `TABLE`-reason.
  (Row-by-row split с реплицированным header'ом — этап 3.)
- **List**          → atomic. Overflow → один чанк с `LIST`-reason.
- **Blockquote**    → как paragraph.
- **Html**          → как paragraph.
- **HorizontalRule**→ skip (структурный разделитель, в чанк не идёт).

**Контракт offset-tracking**: `chunk.content` всегда **slice** исходного
`Section.content`. Никакой репликации или синтеза. Структурную интерпретацию
(тип блока, язык кода, header таблицы) chunker кладёт в `metadata` через
`MarkdownStructuralKeys` — LLM получит и оригинал, и интерпретацию.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

from boba.indexing import (
    Block,
    BlockquoteBlock,
    CodeFenceBlock,
    HeadingBlock,
    HorizontalRuleBlock,
    HtmlBlock,
    ListBlock,
    ParagraphBlock,
    TableBlock,
)
from boba.markdown.blocks import MarkdownBlockParser
from boba.indexing import (
    AnchorBasedChunkId,
    Chunk,
    ChunkerId,
    ChunkIdStrategy,
    ChunkLocation,
    DigestPrefix,
    KeyEncoder,
    Metadata,
    MetadataKey,
    OverlapCharSplitter,
    Section,
    SectionChunker,
)
from boba.indexing.context import PipelineContext

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_DIGEST_PREFIX_CHARS",
    "MarkdownStructuralChunker",
    "MarkdownStructuralChunkerConfig",
    "MarkdownStructuralKeys",
    "markdown_structural_chunker",
]


DEFAULT_CHUNK_SIZE = 1500
DEFAULT_CHUNK_OVERLAP = 150
DEFAULT_DIGEST_PREFIX_CHARS = 12


class MarkdownStructuralKeys:
    """Структурные ключи metadata, проставляемые `MarkdownStructuralChunker`."""

    BLOCK_TYPE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="chunker.markdown.block_type",
        decode=str,
        encode=str,
    )
    """
    `heading` | `paragraph` | `code_fence` | `table` | `list` | `blockquote` | `html`
    """

    CODE_LANGUAGE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="chunker.markdown.code_language",
        decode=str,
        encode=str,
    )
    """
    info-string code-fence (e.g. `python`, `sql`); только для BLOCK_TYPE=code_fence
    """

    HEADING_LEVEL: ClassVar[MetadataKey[int]] = MetadataKey(
        name="chunker.markdown.heading_level",
        decode=int,
        encode=str,
    )
    """1..6 — уровень heading-префикса чанка (если есть)."""

    HEADING_TEXT: ClassVar[MetadataKey[str]] = MetadataKey(
        name="chunker.markdown.heading_text",
        decode=str,
        encode=str,
    )
    """Текст heading'а БЕЗ `#`-маркеров (если есть)."""

    LIST_ORDERED: ClassVar[MetadataKey[bool]] = MetadataKey(
        name="chunker.markdown.list_ordered",
        decode=lambda s: s == "1",
        encode=lambda v: "1" if v else "0",
    )
    """True для нумерованного списка, False для bullet; только для BLOCK_TYPE=list."""

    OVERFLOW_REASON: ClassVar[MetadataKey[str]] = MetadataKey(
        name="chunker.markdown.overflow_reason",
        decode=str,
        encode=str,
    )
    """
    `code_fence_too_large` | `table_too_large` | `list_too_large` —
    проставляется когда блок не влезает в chunk_size и эмитится atomic
    (без structured-split). Pipeline-потребитель может escalate / split / log.
    """

    TABLE_HEADER: ClassVar[MetadataKey[str]] = MetadataKey(
        name="chunker.markdown.table_header",
        decode=str,
        encode=str,
    )
    """
    Raw markdown header'а таблицы + separator-строки
    (`"| a | b |\\n|---|---|"`).
    Проставляется на КАЖДЫЙ row-чанк table-overflow split'а — позволяет LLM
    знать имена столбцов даже если в content попали только data-строки.
    """

    TABLE_ROW_RANGE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="chunker.markdown.table_row_range",
        decode=str,
        encode=str,
    )
    """
    Диапазон строк таблицы в этом чанке (`"0..3"` — строки 0,1,2,3
    включительно). Только для table-overflow split'а.
    """

    LIST_ITEM_RANGE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="chunker.markdown.list_item_range",
        decode=str,
        encode=str,
    )
    """
    Диапазон items списка в этом чанке (`"0..4"` — items 0..4 включительно).
    Только для list-overflow split'а.
    """

    CODE_FENCE_LINE_RANGE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="chunker.markdown.code_fence_line_range",
        decode=str,
        encode=str,
    )
    """
    Диапазон строк тела code-fence в этом чанке (`"0..15"` — строки 0..15
    включительно). Только для code-fence-overflow split'а.
    LLM при reconstruction'е оборачивает content в fence-маркеры по
    `CODE_LANGUAGE`.
    """


@dataclass(frozen=True)
class MarkdownStructuralChunkerConfig:
    """Конфиг markdown_structural_chunker."""

    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    digest_prefix_chars: int = DEFAULT_DIGEST_PREFIX_CHARS


class MarkdownStructuralChunker(SectionChunker[str]):
    """
    Structured `Chunker[str]` для markdown'а с per-block стратегиями.

    **Pipeline внутри `stream(ctx, sections)`**:
    ```
    Section[str]
        ↓ MarkdownBlockParser.parse(content)
    list[Block]
        ↓ heading-prefix-merge (group heading + следующий main-block)
    list[(prefix_headings, main_block)]
        ↓ per-block strategy (atomic / char-split / overflow)
    Iterable[Chunk[str]]   (content = slice оригинала + structured metadata)
    ```

    **Ключевая особенность**: chunking-стратегия **зависит от типа блока**,
    полученного от AST-парсера. Code-fence не разрывается атомарно (или
    режется line-based с CODE_FENCE_LINE_RANGE в metadata), heading
    прикрепляется как префикс к следующему блоку, table режется row-by-row
    с TABLE_HEADER в каждом чанке, paragraph режется char-based с overlap.

    **Когда применять**:
    - Markdown-pipeline (после `MarkdownReader` или `HtmlMarkdownifyReader`),
      где Section.content содержит разнообразную markdown-разметку.
    - Любая резка markdown'а — это единственный chunker в `boba-markdown`,
      все эмерджентные separator-based стратегии удалены.

    **Когда НЕ нужно**:
    - Plain prose / неструктурированный текст — используй базовый
      `OverlapCharSplitter` из `boba-indexing` напрямую (без AST).
    - Если `markdown-it-py` нельзя установить как зависимость.

    **Контракт `Chunk.content`**: всегда slice исходного `Section.content`
    (как у обычного `Chunker`'а). Структурная информация (cells таблицы,
    items списка, language code-fence) идёт в `metadata`, а не в content.
    """

    def __init__(
        self,
        chunker_id: ChunkerId,
        id_strategy: ChunkIdStrategy[str],
        chunk_size: int,
        chunk_overlap: int,
    ) -> None:
        # `SectionChunker.__init__` ожидает splitter — для structured chunker'а
        # он не нужен (per-block стратегии сами решают), но base-class требует.
        # Передаём dummy-splitter, который никогда не вызывается (мы переопределяем stream).
        super().__init__(
            chunker_id=chunker_id,
            splitter=OverlapCharSplitter(
                chunk_size=chunk_size, chunk_overlap=chunk_overlap
            ),
            id_strategy=id_strategy,
        )
        self._parser = MarkdownBlockParser()
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        # Splitter для overflow paragraph'ов (char-split с overlap).
        self._overflow_splitter = OverlapCharSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    def stream(
        self,
        ctx: PipelineContext,
        stream: Iterable[Section[str]],
    ) -> Iterable[Chunk[str]]:
        del ctx
        per_source_index: dict[str, int] = {}
        for section in stream:
            blocks = self._parser.parse(section.content)
            grouped = self._group_with_heading_prefix(blocks)
            for prefix_headings, main_block in grouped:
                for chunk_content, chunk_loc, extra_meta in self._chunks_for_group(
                    section, prefix_headings, main_block
                ):
                    key = section.source_id.to_wire()
                    idx = per_source_index.get(key, 0)
                    per_source_index[key] = idx + 1
                    yield Chunk[str](
                        chunk_id=self._id_strategy.compute(section, idx),
                        source_id=section.source_id,
                        content=chunk_content,
                        location=chunk_loc,
                        anchor=section.anchor,
                        chunk_index=idx,
                        metadata=section.metadata.merge(extra_meta),
                    )

    @staticmethod
    def _group_with_heading_prefix(
        blocks: list[Block],
    ) -> list[tuple[list[HeadingBlock], Block | None]]:
        """Группирует heading'и с следующим main-блоком (heading-prefix-merge).

        Возвращает list of `(prefix_headings, main_block)`:
        - `prefix_headings` — heading'и непосредственно перед main_block (может быть несколько).
        - `main_block` — paragraph / code / table / list / blockquote / html;
          либо None (если heading'и без followup в конце документа).

        `HorizontalRuleBlock` пропускается (структурный разделитель).
        """
        groups: list[tuple[list[HeadingBlock], Block | None]] = []
        pending: list[HeadingBlock] = []
        for b in blocks:
            if isinstance(b, HorizontalRuleBlock):
                continue
            if isinstance(b, HeadingBlock):
                pending.append(b)
                continue
            groups.append((pending, b))
            pending = []
        if pending:
            # trailing heading'и без followup — каждый как самостоятельная группа.
            for h in pending:
                groups.append(([h], None))
        return groups

    def _chunks_for_group(
        self,
        section: Section[str],
        prefix_headings: list[HeadingBlock],
        main_block: Block | None,
    ) -> Iterable[tuple[str, ChunkLocation, Metadata]]:
        """Применяет per-block стратегию; возвращает (content, location, extra_meta)."""
        if main_block is None:
            # Только heading без followup — atomic.
            assert prefix_headings, "empty group"
            head = prefix_headings[0]
            content = section.content[head.location.start : head.location.end]
            yield content, head.location, self._meta_for_heading(head)
            return

        # Slice от первого heading'а (или от main_block если префиксов нет)
        # до конца main_block. В исходном Section.content между ними `\n\n`,
        # так что slice натурально включает heading + body.
        slice_start = (
            prefix_headings[0].location.start
            if prefix_headings
            else main_block.location.start
        )
        slice_end = main_block.location.end
        full_content = section.content[slice_start:slice_end]
        full_loc = ChunkLocation(start=slice_start, end=slice_end)

        # Базовая metadata: BLOCK_TYPE + heading-info (если есть префикс).
        base_meta = self._base_meta(main_block, prefix_headings)

        # Per-block strategy.
        if isinstance(main_block, ParagraphBlock | BlockquoteBlock | HtmlBlock):
            yield from self._strategy_paragraph_like(
                section, full_content, full_loc, slice_start, base_meta
            )
        elif isinstance(main_block, CodeFenceBlock):
            yield from self._strategy_code_fence(
                section, main_block, full_content, full_loc, base_meta
            )
        elif isinstance(main_block, TableBlock):
            yield from self._strategy_table(
                section, main_block, full_content, full_loc, base_meta
            )
        elif isinstance(main_block, ListBlock):
            yield from self._strategy_list(
                section, main_block, full_content, full_loc, base_meta
            )
        else:
            # Неизвестный тип — atomic.
            yield full_content, full_loc, base_meta

    def _strategy_paragraph_like(
        self,
        section: Section[str],
        content: str,
        loc: ChunkLocation,
        slice_start: int,
        base_meta: Metadata,
    ) -> Iterable[tuple[str, ChunkLocation, Metadata]]:
        """Atomic если ≤ chunk_size; иначе char-split с overlap."""
        if len(content) <= self._chunk_size:
            yield content, loc, base_meta
            return
        # Splitter работает в координатах `content`; смещаем offset'ы в Section.content.
        for piece in self._overflow_splitter.split(content):
            piece_loc = ChunkLocation(
                start=slice_start + piece.location.start,
                end=slice_start + piece.location.end,
            )
            yield piece.content, piece_loc, base_meta

    def _strategy_atomic_with_overflow(
        self,
        content: str,
        loc: ChunkLocation,
        base_meta: Metadata,
        overflow_reason: str,
    ) -> Iterable[tuple[str, ChunkLocation, Metadata]]:
        """Всегда atomic; если > chunk_size — добавляет OVERFLOW_REASON в metadata."""
        meta = base_meta
        if len(content) > self._chunk_size:
            meta = meta.set(MarkdownStructuralKeys.OVERFLOW_REASON, overflow_reason)
        yield content, loc, meta

    def _strategy_code_fence(
        self,
        section: Section[str],
        fence: CodeFenceBlock,
        full_content: str,
        full_loc: ChunkLocation,
        base_meta: Metadata,
    ) -> Iterable[tuple[str, ChunkLocation, Metadata]]:
        """Atomic если влезает; иначе line-based split с CODE_FENCE_LINE_RANGE.

        Slice-инвариант сохраняется: `chunk.content` — slice строк тела кода
        из `Section.content`, без обрамляющих fence-маркеров. Reconstructure
        полного fence'а — на стороне LLM (через `CODE_LANGUAGE` в metadata).
        """
        if len(full_content) <= self._chunk_size:
            yield full_content, full_loc, base_meta
            return
        if not fence.code_line_locations:
            yield from self._strategy_atomic_with_overflow(
                full_content, full_loc, base_meta, "code_fence_too_large"
            )
            return
        ranges = self._pack_locations(
            unit_locations=fence.code_line_locations,
            first_chunk_prefix_size=0,
            chunk_start_for_first=fence.code_line_locations[0].start,
        )
        yield from self._emit_packed(
            section, ranges, base_meta, MarkdownStructuralKeys.CODE_FENCE_LINE_RANGE
        )

    def _strategy_table(
        self,
        section: Section[str],
        table: TableBlock,
        full_content: str,
        full_loc: ChunkLocation,
        base_meta: Metadata,
    ) -> Iterable[tuple[str, ChunkLocation, Metadata]]:
        """Atomic если влезает; иначе row-by-row split с replicated header в metadata."""
        if len(full_content) <= self._chunk_size:
            yield full_content, full_loc, base_meta
            return
        if not table.row_locations:
            # Парсер не отдал per-row offset'ы — деградируем до atomic-overflow.
            yield from self._strategy_atomic_with_overflow(
                full_content, full_loc, base_meta, "table_too_large"
            )
            return

        # Greedy bin-packing rows в чанки. Первый чанк имеет prefix-budget
        # (heading + table_header), остальные — только rows.
        ranges = self._pack_locations(
            unit_locations=table.row_locations,
            first_chunk_prefix_size=table.row_locations[0].start - full_loc.start,
            chunk_start_for_first=full_loc.start,
        )
        meta_with_header = base_meta.set(
            MarkdownStructuralKeys.TABLE_HEADER, table.header_text
        )
        yield from self._emit_packed(
            section, ranges, meta_with_header, MarkdownStructuralKeys.TABLE_ROW_RANGE
        )

    def _strategy_list(
        self,
        section: Section[str],
        lst: ListBlock,
        full_content: str,
        full_loc: ChunkLocation,
        base_meta: Metadata,
    ) -> Iterable[tuple[str, ChunkLocation, Metadata]]:
        """Atomic если влезает; иначе item-by-item split."""
        if len(full_content) <= self._chunk_size:
            yield full_content, full_loc, base_meta
            return
        if not lst.item_locations:
            yield from self._strategy_atomic_with_overflow(
                full_content, full_loc, base_meta, "list_too_large"
            )
            return

        ranges = self._pack_locations(
            unit_locations=lst.item_locations,
            first_chunk_prefix_size=lst.item_locations[0].start - full_loc.start,
            chunk_start_for_first=full_loc.start,
        )
        yield from self._emit_packed(
            section, ranges, base_meta, MarkdownStructuralKeys.LIST_ITEM_RANGE
        )

    def _pack_locations(
        self,
        unit_locations: tuple[ChunkLocation, ...],
        first_chunk_prefix_size: int,
        chunk_start_for_first: int,
    ) -> list[tuple[int, int, int, int]]:
        """Greedy bin-packing locations в чанки ≤ chunk_size.

        Возвращает list of `(first_unit_idx, last_unit_idx, chunk_start, chunk_end)`.
        Первый чанк начинается с `chunk_start_for_first` (включает префикс размером
        `first_chunk_prefix_size`); последующие — с начала первой попавшей в них unit-locations.
        """  # noqa: E501
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
            # Если кандидат превышает chunk_size И в текущем чанке уже есть unit'ы — flush.
            if candidate > chunk_size and cur_last >= cur_first:
                ranges.append(
                    (cur_first, cur_last, cur_start, unit_locations[cur_last].end)
                )
                # Новый чанк начинается с этой unit-locations (без heading-префикса).
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

    def _emit_packed(
        self,
        section: Section[str],
        ranges: list[tuple[int, int, int, int]],
        base_meta: Metadata,
        range_key: MetadataKey[str],
    ) -> Iterable[tuple[str, ChunkLocation, Metadata]]:
        """Эмитит чанки по pack'ленным range'ам с TABLE/LIST-RANGE metadata."""
        for first_idx, last_idx, c_start, c_end in ranges:
            content = section.content[c_start:c_end]
            loc = ChunkLocation(start=c_start, end=c_end)
            meta = base_meta.set(range_key, f"{first_idx}..{last_idx}")
            yield content, loc, meta

    @staticmethod
    def _meta_for_heading(head: HeadingBlock) -> Metadata:
        """Metadata для standalone heading-чанка."""
        return (
            Metadata.empty()
            .set(MarkdownStructuralKeys.BLOCK_TYPE, HeadingBlock.BLOCK_TYPE)
            .set(MarkdownStructuralKeys.HEADING_LEVEL, head.level)
            .set(MarkdownStructuralKeys.HEADING_TEXT, head.text)
        )

    @staticmethod
    def _base_meta(
        main_block: Block,
        prefix_headings: list[HeadingBlock],
    ) -> Metadata:
        """Базовая metadata: BLOCK_TYPE + опциональный heading-префикс."""
        meta = Metadata.empty().set(
            MarkdownStructuralKeys.BLOCK_TYPE, main_block.BLOCK_TYPE
        )
        if isinstance(main_block, CodeFenceBlock) and main_block.language:
            meta = meta.set(MarkdownStructuralKeys.CODE_LANGUAGE, main_block.language)
        if isinstance(main_block, ListBlock):
            meta = meta.set(MarkdownStructuralKeys.LIST_ORDERED, main_block.ordered)
        if prefix_headings:
            # Берём САМЫЙ ГЛУБОКИЙ heading из префикса (ближайший к main_block).
            top = prefix_headings[-1]
            meta = meta.set(MarkdownStructuralKeys.HEADING_LEVEL, top.level).set(
                MarkdownStructuralKeys.HEADING_TEXT, top.text
            )
        return meta


def markdown_structural_chunker(
    config: MarkdownStructuralChunkerConfig,
    encoder: KeyEncoder[str],
    prefix: DigestPrefix,
) -> MarkdownStructuralChunker:
    """
    Фабрика `MarkdownStructuralChunker` с `AnchorBasedChunkId`.

    **Сборка**:
    ```python
    config + encoder + prefix
        ↓
    MarkdownStructuralChunker(
        chunker_id="markdown_structural",
        splitter=OverlapCharSplitter(chunk_size, chunk_overlap),  # для overflow paragraph'ов
        id_strategy=AnchorBasedChunkId(encoder, prefix),
        parser=MarkdownBlockParser(),
    )
    ```

    **Pipeline-цепочка** (типичный случай):
    ```
    HtmlMarkdownifyReader  →  Section[str] (markdown content)
        ↓
    markdown_structural_chunker → Chunk[str]
        с metadata.BLOCK_TYPE / CODE_LANGUAGE / HEADING_LEVEL / OVERFLOW_REASON
    ```

    **Пример** (документ с heading + paragraph + code-fence + table → 3 чанка
    с разными BLOCK_TYPE; code-fence atomic, heading прикреплён к paragraph'у):
    ```python
    chunker = markdown_structural_chunker(
        MarkdownStructuralChunkerConfig(chunk_size=200, chunk_overlap=0),
        encoder=Sha256TextEncoder(),
        prefix=FixedDigestPrefix(12),
    )

    md = '''# Setup

    Install with pip.

    ```python
    pip install foo
    ```

    | option | default |
    |--------|---------|
    | port   | 8080    |
    '''

    sections = iter([
        Section(SourceId("doc1"), md, anchor="setup", order=0),
    ])

    chunks = list(chunker.stream(ctx, sections))
    # → [
    #     Chunk(
    #         content="# Setup\\n\\nInstall with pip.",   # heading + paragraph (slice)
    #         metadata={BLOCK_TYPE: "paragraph", HEADING_LEVEL: 1, HEADING_TEXT: "Setup", ...},
    #         ...
    #     ),
    #     Chunk(
    #         content="```python\\npip install foo\\n```",  # code-fence atomic
    #         metadata={BLOCK_TYPE: "code_fence", CODE_LANGUAGE: "python", ...},
    #         ...
    #     ),
    #     Chunk(
    #         content="| option | default |\\n|--------|---------|\\n| port   | 8080    |",
    #         metadata={BLOCK_TYPE: "table", ...},
    #         ...
    #     ),
    # ]
    ```
    """  # noqa: E501
    return MarkdownStructuralChunker(
        chunker_id=ChunkerId("markdown_structural"),
        id_strategy=AnchorBasedChunkId(encoder=encoder, prefix=prefix),
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
    )
