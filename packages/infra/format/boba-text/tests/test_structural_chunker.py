"""StructuralChunker: heading-breadcrumbs + replicate-header + per-chunk raw."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass

import pytest

from boba.indexing import (
    ChunkerId,
    ChunkId,
    ChunkIdGenerator,
    ChunkLocation,
    FormatBlock,
    FormatPlan,
    HeadingSection,
    Section,
    SectionKeys,
    Sha256TextEncoder,
    SourceId,
    SplitPiece,
    Splitter,
)
from boba.text import OverlapCharSplitter, StructuralChunker

pytestmark = pytest.mark.anyio


async def _astream(
    items: Iterable[Section[str]],
) -> AsyncIterator[Section[str]]:
    """Готовые секции как поток — вход чанкера только асинхронный."""
    for item in items:
        yield item


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class _StaticIdGenerator(ChunkIdGenerator[str]):
    def compute(self, section: Section[str], chunk_index: int) -> ChunkId:
        del section
        return ChunkId(f"sc:{chunk_index}")


class _IdentitySplitter(Splitter[str]):
    """Один piece на весь body — для unit-тестов compose-логики."""

    def split(self, value: str) -> Iterable[SplitPiece[str]]:
        if not value:
            return
        yield SplitPiece(content=value, location=ChunkLocation(start=0, end=len(value)))


def _identity_factory(_extra_overhead: int) -> Splitter[str]:
    return _IdentitySplitter()


def _chunker(splitter_factory=_identity_factory) -> StructuralChunker:
    return StructuralChunker(
        chunker_id=ChunkerId("structural"),
        splitter_factory=splitter_factory,
        chunk_id_generator=_StaticIdGenerator(),
        content_hasher=Sha256TextEncoder(),
    )


# ---------------- breadcrumbs --------------------------------------------------


async def test_breadcrumbs_grow_and_pop_correctly():
    """H1 -> H2 -> H1 сбрасывает H2; HEADING_PATH в metadata следующих чанков."""
    sections = [
        HeadingSection(
            source_id=SourceId("d"), content="<h1>A</h1>", order=0, level=1, text="A"
        ),
        HeadingSection(
            source_id=SourceId("d"), content="<h2>B</h2>", order=1, level=2, text="B"
        ),
        # Простой Section с дефолтным to_format_plan — не heading.
        Section(source_id=SourceId("d"), content="body-under-B", order=2),
        HeadingSection(
            source_id=SourceId("d"), content="<h1>C</h1>", order=3, level=1, text="C"
        ),
        Section(source_id=SourceId("d"), content="body-under-C", order=4),
    ]
    chunks = [item async for item in _chunker().chunk(_astream(sections))]
    by_text = {c.format_content: c for c in chunks}
    # Body после H1>A>H2>B видит "A › B"
    body_b = by_text["A › B\n\nbody-under-B"]
    if body_b.metadata.get(SectionKeys.HEADING_PATH) != "A › B":
        raise AssertionError('body_b.metadata.get(SectionKeys.HEADING_PATH) == "A › B"')
    # Body после H1>C видит только "C"
    body_c = by_text["C\n\nbody-under-C"]
    if body_c.metadata.get(SectionKeys.HEADING_PATH) != "C":
        raise AssertionError('body_c.metadata.get(SectionKeys.HEADING_PATH) == "C"')


async def test_breadcrumbs_reset_on_source_id_change():
    """Стек не утекает между разными source_id."""
    sections = [
        HeadingSection(
            source_id=SourceId("doc1"), content="<h1>A</h1>", order=0, level=1, text="A"
        ),
        Section(source_id=SourceId("doc2"), content="hello", order=0),
    ]
    chunks = [item async for item in _chunker().chunk(_astream(sections))]
    [_, body] = chunks
    # Для doc2 стек пуст -> нет HEADING_PATH и нет prefix в format_content.
    if body.format_content != "hello":
        raise AssertionError('body.format_content == "hello"')
    if body.metadata.get(SectionKeys.HEADING_PATH) is not None:
        raise AssertionError("body.metadata.get(SectionKeys.HEADING_PATH) is None")


async def test_heading_chunk_itself_has_no_prefix_but_path_in_metadata():
    """Сам heading-чанк — atomic markdown без breadcrumb-prefix; в metadata
    HEADING_PATH полный путь после обновления стека.
    """
    sections = [
        HeadingSection(
            source_id=SourceId("d"), content="<h1>A</h1>", order=0, level=1, text="A"
        ),
        HeadingSection(
            source_id=SourceId("d"), content="<h2>B</h2>", order=1, level=2, text="B"
        ),
    ]
    chunks = [item async for item in _chunker().chunk(_astream(sections))]
    a, b = chunks
    if a.format_content != "# A":
        raise AssertionError('a.format_content == "# A"')
    if a.metadata.get(SectionKeys.HEADING_PATH) != "A":
        raise AssertionError('a.metadata.get(SectionKeys.HEADING_PATH) == "A"')
    if b.format_content != "## B":
        raise AssertionError('b.format_content == "## B"')
    if b.metadata.get(SectionKeys.HEADING_PATH) != "A › B":
        raise AssertionError('b.metadata.get(SectionKeys.HEADING_PATH) == "A › B"')


# ---------------- to_chunk_metadata + typed keys -------------------------------


async def test_to_chunk_metadata_is_merged():
    section = HeadingSection(
        source_id=SourceId("d"),
        content="<h1>X</h1>",
        order=0,
        level=3,
        text="X",
    )
    [chunk] = [item async for item in _chunker().chunk(_astream([section]))]
    if chunk.metadata.get(SectionKeys.HEADING_LEVEL) != 3:
        raise AssertionError("chunk.metadata.get(SectionKeys.HEADING_LEVEL) == 3")
    if chunk.metadata.get(SectionKeys.HEADING_TEXT) != "X":
        raise AssertionError('chunk.metadata.get(SectionKeys.HEADING_TEXT) == "X"')


# ---------------- table: replicate header + per-row raw -----------------------


@dataclass(frozen=True)
class _TableSection(Section[str]):
    """План markdown-таблицы: header реплицируется, каждый row atomic."""

    def to_format_plan(self) -> FormatPlan:
        return FormatPlan(
            blocks=(
                FormatBlock(
                    format_content="| id | int |",
                    raw_content="<tr><td>id</td><td>int</td></tr>",
                    location=ChunkLocation(start=0, end=11),
                    is_atomic=True,
                ),
                FormatBlock(
                    format_content="| foo | str |",
                    raw_content="<tr><td>foo</td><td>str</td></tr>",
                    location=ChunkLocation(start=11, end=24),
                    is_atomic=True,
                ),
            ),
            repeat_header="| name | type |\n| --- | --- |\n",
            block_glue="\n",
        )


async def test_table_replicates_header_in_each_row_chunk():
    section = _TableSection(source_id=SourceId("t"), content="<table/>", order=0)

    # Splitter режет body по \n (block_glue таблицы), один row на чанк
    # при chunk_size=15.
    def factory(extra_overhead: int) -> Splitter[str]:
        return OverlapCharSplitter(
            chunk_size=15 + extra_overhead,
            chunk_overlap=0,
            separators=("\n", " ", ""),
            extra_overhead=extra_overhead,
        )

    chunks = [item async for item in _chunker(factory).chunk(_astream([section]))]
    # Каждый row-чанк начинается с реплицированного markdown header'а.
    table_chunks = [c for c in chunks if "| name | type |" in c.format_content]
    if len(table_chunks) != 2:
        raise AssertionError("len(table_chunks) == 2")
    if "| id | int |" not in table_chunks[0].format_content:
        raise AssertionError('"| id | int |" in table_chunks[0].format_content')
    if "| foo | str |" not in table_chunks[1].format_content:
        raise AssertionError('"| foo | str |" in table_chunks[1].format_content')
    # raw_content per-row — оригинальный <tr>.
    if not (table_chunks[0].raw_content.startswith("<tr>")):
        raise AssertionError('table_chunks[0].raw_content.startswith("<tr>")')
    if table_chunks[0].raw_content == table_chunks[1].raw_content:
        raise AssertionError("table_chunks[0].raw_content != table_chunks[1].raw_cont…")


# ---------------- code: fenced wrapping ----------------------------------------


@dataclass(frozen=True)
class _CodeSection(Section[str]):
    """План fenced code-block: repeat_header/footer оборачивают тело."""

    def to_format_plan(self) -> FormatPlan:
        return FormatPlan(
            blocks=(
                FormatBlock(
                    format_content="x = 1",
                    raw_content='<pre><code class="language-py">x = 1</code></pre>',
                    location=ChunkLocation(start=0, end=5),
                    is_atomic=False,
                ),
            ),
            repeat_header="```py\n",
            repeat_footer="\n```",
            block_glue="\n",
        )


async def test_code_block_wrapped_with_fence():
    section = _CodeSection(source_id=SourceId("c"), content="<pre/>", order=0)
    [chunk] = [item async for item in _chunker().chunk(_astream([section]))]
    if chunk.format_content != "```py\nx = 1\n```":
        raise AssertionError('chunk.format_content == "```py\\nx = 1\\n```"')


# ---------------- hr: skipped --------------------------------------------------


@dataclass(frozen=True)
class _EmptyPlanSection(Section[str]):
    """Структурный маркёр (<hr>): пустой план, chunker пропускает."""

    def to_format_plan(self) -> FormatPlan:
        return FormatPlan()


async def test_hr_section_is_skipped():
    section = _EmptyPlanSection(source_id=SourceId("h"), content="<hr/>", order=0)
    chunks = [item async for item in _chunker().chunk(_astream([section]))]
    if chunks != []:
        raise AssertionError("chunks == []")


# ---------------- chunk_index continuous per source_id -------------------------


async def test_chunk_index_is_continuous_per_source():
    sections = [
        HeadingSection(
            source_id=SourceId("d"), content="<h1>A</h1>", order=0, level=1, text="A"
        ),
        Section(source_id=SourceId("d"), content="body", order=1),
    ]
    chunks = [item async for item in _chunker().chunk(_astream(sections))]
    if [c.chunk_index for c in chunks] != [0, 1]:
        raise AssertionError("[c.chunk_index for c in chunks] == [0, 1]")
