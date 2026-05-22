"""StructuralChunker: heading-breadcrumbs + replicate-header + per-chunk raw."""

from __future__ import annotations

from collections.abc import Iterable
from io import BytesIO

from boba.html import HtmlReader
from boba.indexing import (
    ChunkerId,
    ChunkId,
    ChunkIdGenerator,
    ChunkLocation,
    HeadingSection,
    Metadata,
    PipelineContext,
    PipelineId,
    RawDocument,
    Section,
    SectionKeys,
    Sha256TextEncoder,
    SourceId,
    SplitPiece,
    Splitter,
)
from boba.text import OverlapCharSplitter, StructuralChunker


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


def _ctx() -> PipelineContext:
    return PipelineContext(pipeline_id=PipelineId("t"))


def _chunker(splitter_factory=_identity_factory) -> StructuralChunker:
    return StructuralChunker(
        chunker_id=ChunkerId("structural"),
        splitter_factory=splitter_factory,
        chunk_id_generator=_StaticIdGenerator(),
        content_hasher=Sha256TextEncoder(),
    )


# ---------------- breadcrumbs --------------------------------------------------


def test_breadcrumbs_grow_and_pop_correctly():
    """H1 → H2 → H1 сбрасывает H2; HEADING_PATH в metadata следующих чанков."""
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
    chunks = list(_chunker().stream(_ctx(), iter(sections)))
    by_text = {c.format_content: c for c in chunks}
    # Body после H1>A>H2>B видит "A › B"
    body_b = by_text["A › B\n\nbody-under-B"]
    assert body_b.metadata.get(SectionKeys.HEADING_PATH) == "A › B"
    # Body после H1>C видит только "C"
    body_c = by_text["C\n\nbody-under-C"]
    assert body_c.metadata.get(SectionKeys.HEADING_PATH) == "C"


def test_breadcrumbs_reset_on_source_id_change():
    """Стек не утекает между разными source_id."""
    sections = [
        HeadingSection(
            source_id=SourceId("doc1"), content="<h1>A</h1>", order=0, level=1, text="A"
        ),
        Section(source_id=SourceId("doc2"), content="hello", order=0),
    ]
    chunks = list(_chunker().stream(_ctx(), iter(sections)))
    [_, body] = chunks
    # Для doc2 стек пуст → нет HEADING_PATH и нет prefix в format_content.
    assert body.format_content == "hello"
    assert body.metadata.get(SectionKeys.HEADING_PATH) is None


def test_heading_chunk_itself_has_no_prefix_but_path_in_metadata():
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
    chunks = list(_chunker().stream(_ctx(), iter(sections)))
    a, b = chunks
    assert a.format_content == "# A"
    assert a.metadata.get(SectionKeys.HEADING_PATH) == "A"
    assert b.format_content == "## B"
    assert b.metadata.get(SectionKeys.HEADING_PATH) == "A › B"


# ---------------- to_chunk_metadata + typed keys -------------------------------


def test_to_chunk_metadata_is_merged():
    section = HeadingSection(
        source_id=SourceId("d"),
        content="<h1>X</h1>",
        order=0,
        level=3,
        text="X",
    )
    [chunk] = list(_chunker().stream(_ctx(), iter([section])))
    assert chunk.metadata.get(SectionKeys.HEADING_LEVEL) == 3
    assert chunk.metadata.get(SectionKeys.HEADING_TEXT) == "X"


# ---------------- table: replicate header + per-row raw -----------------------


def test_table_replicates_header_in_each_row_chunk():
    html = (
        "<html><body><table>"
        "<thead><tr><th>name</th><th>type</th></tr></thead>"
        "<tbody>"
        "<tr><td>id</td><td>int</td></tr>"
        "<tr><td>foo</td><td>str</td></tr>"
        "</tbody></table></body></html>"
    )
    doc = RawDocument(
        handle=BytesIO(html.encode()),
        source_id=SourceId("t"),
        metadata=Metadata.empty(),
    )
    sections = list(HtmlReader().convert(doc))

    # Splitter режет body по \n (block_glue таблицы), один row на чанк
    # при chunk_size=15.
    def factory(extra_overhead: int) -> Splitter[str]:
        return OverlapCharSplitter(
            chunk_size=15 + extra_overhead,
            chunk_overlap=0,
            separators=("\n", " ", ""),
            extra_overhead=extra_overhead,
        )

    chunks = list(_chunker(factory).stream(_ctx(), iter(sections)))
    # Каждый row-чанк начинается с реплицированного markdown header'а.
    table_chunks = [c for c in chunks if "| name | type |" in c.format_content]
    assert len(table_chunks) == 2
    assert "| id | int |" in table_chunks[0].format_content
    assert "| foo | str |" in table_chunks[1].format_content
    # raw_content per-row — оригинальный <tr>.
    assert table_chunks[0].raw_content.startswith("<tr>")
    assert table_chunks[0].raw_content != table_chunks[1].raw_content


# ---------------- code: fenced wrapping ----------------------------------------


def test_code_block_wrapped_with_fence():
    html = '<html><body><pre><code class="language-py">x = 1</code></pre></body></html>'
    doc = RawDocument(
        handle=BytesIO(html.encode()),
        source_id=SourceId("c"),
        metadata=Metadata.empty(),
    )
    sections = list(HtmlReader().convert(doc))
    [chunk] = list(_chunker().stream(_ctx(), iter(sections)))
    assert chunk.format_content == "```py\nx = 1\n```"


# ---------------- hr: skipped --------------------------------------------------


def test_hr_section_is_skipped():
    html = "<html><body><hr></body></html>"
    doc = RawDocument(
        handle=BytesIO(html.encode()),
        source_id=SourceId("h"),
        metadata=Metadata.empty(),
    )
    sections = list(HtmlReader().convert(doc))
    chunks = list(_chunker().stream(_ctx(), iter(sections)))
    assert chunks == []


# ---------------- chunk_index continuous per source_id -------------------------


def test_chunk_index_is_continuous_per_source():
    sections = [
        HeadingSection(
            source_id=SourceId("d"), content="<h1>A</h1>", order=0, level=1, text="A"
        ),
        Section(source_id=SourceId("d"), content="body", order=1),
    ]
    chunks = list(_chunker().stream(_ctx(), iter(sections)))
    assert [c.chunk_index for c in chunks] == [0, 1]
