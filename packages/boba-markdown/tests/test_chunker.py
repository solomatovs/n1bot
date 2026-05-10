"""MarkdownStructuralChunker: per-block strategy chunking."""

from __future__ import annotations

import pytest

# Skip if markdown-it-py not installed.
pytest.importorskip("markdown_it")

from boba.markdown import (
    MarkdownStructuralChunkerConfig,
    MarkdownStructuralKeys,
    markdown_structural_chunker,
)
from boba.indexing import (
    FixedDigestPrefix,
    Section,
    Sha256TextEncoder,
    SourceId,
)
from boba.indexing.context import PipelineContext, PipelineId


def _ctx() -> PipelineContext:
    return PipelineContext(pipeline_id=PipelineId("t"))


def _chunker(*, size: int = 1500, overlap: int = 0):
    cfg = MarkdownStructuralChunkerConfig(chunk_size=size, chunk_overlap=overlap)
    return markdown_structural_chunker(
        cfg, Sha256TextEncoder(), FixedDigestPrefix(cfg.digest_prefix_chars)
    )


def _section(content: str, *, anchor: str = "x") -> Section[str]:
    return Section(
        source_id=SourceId("doc1"), content=content, anchor=anchor, order=0
    )


def test_slice_invariant_holds_for_all_chunks():
    """`Section.content[chunk.location.start:end] == chunk.content` для всех типов."""
    md = """# Title

paragraph one.

```python
def f():
    return 1
```

| a | b |
|---|---|
| 1 | 2 |

- item one
- item two

> a quote

paragraph two.
"""
    section = _section(md)
    for c in _chunker(size=200).stream(_ctx(), iter([section])):
        sliced = md[c.location.start : c.location.end]
        assert sliced == c.content, f"slice broken: {c.metadata.to_wire()}"


def test_heading_prefix_attached_to_following_paragraph():
    md = "# Setup\n\nInstall with pip."
    [chunk] = list(_chunker().stream(_ctx(), iter([_section(md)])))
    assert chunk.content == md
    assert chunk.metadata.get(MarkdownStructuralKeys.BLOCK_TYPE) == "paragraph"
    assert chunk.metadata.get(MarkdownStructuralKeys.HEADING_LEVEL) == 1
    assert chunk.metadata.get(MarkdownStructuralKeys.HEADING_TEXT) == "Setup"


def test_nested_headings_use_deepest_as_prefix():
    """h1 → h2 → paragraph: чанк помечен HEADING_LEVEL=2 (ближайший к main_block)."""
    md = "# Top\n\n## Sub\n\nbody text."
    [chunk] = list(_chunker().stream(_ctx(), iter([_section(md)])))
    assert chunk.metadata.get(MarkdownStructuralKeys.HEADING_LEVEL) == 2
    assert chunk.metadata.get(MarkdownStructuralKeys.HEADING_TEXT) == "Sub"
    # Slice от h1 (включает оба heading'а).
    assert chunk.content.startswith("# Top")
    assert chunk.content.endswith("body text.")


def test_trailing_heading_without_followup_emitted_standalone():
    md = "para.\n\n# Trailing"
    chunks = list(_chunker().stream(_ctx(), iter([_section(md)])))
    assert len(chunks) == 2
    # First — paragraph, second — heading-only.
    assert chunks[0].metadata.get(MarkdownStructuralKeys.BLOCK_TYPE) == "paragraph"
    assert chunks[1].metadata.get(MarkdownStructuralKeys.BLOCK_TYPE) == "heading"
    assert chunks[1].content == "# Trailing"


def test_code_fence_atomic_when_fits():
    md = "```python\nprint('hi')\n```"
    [chunk] = list(_chunker(size=200).stream(_ctx(), iter([_section(md)])))
    assert chunk.content == md  # целый fence без разрыва
    assert chunk.metadata.get(MarkdownStructuralKeys.BLOCK_TYPE) == "code_fence"
    assert chunk.metadata.get(MarkdownStructuralKeys.CODE_LANGUAGE) == "python"
    # OVERFLOW_REASON не выставлен.
    assert chunk.metadata.get(MarkdownStructuralKeys.OVERFLOW_REASON) is None


def test_code_fence_overflow_split_line_by_line_with_language_in_meta():
    """Code-fence > chunk_size → line-based split.

    CODE_LANGUAGE и LINE_RANGE проставляются в metadata.
    """
    code = "\n".join(f"line_{i} = {i}" for i in range(50))
    md = f"```python\n{code}\n```"
    chunks = list(_chunker(size=100).stream(_ctx(), iter([_section(md)])))
    assert len(chunks) > 1
    ranges = []
    for c in chunks:
        assert c.metadata.get(MarkdownStructuralKeys.BLOCK_TYPE) == "code_fence"
        assert c.metadata.get(MarkdownStructuralKeys.CODE_LANGUAGE) == "python"
        rng = c.metadata.get(MarkdownStructuralKeys.CODE_FENCE_LINE_RANGE) or ""
        assert ".." in rng
        ranges.append(rng)
        # content — slice оригинала (только тело кода, без fence-маркеров).
        assert md[c.location.start : c.location.end] == c.content
        assert "```" not in c.content  # fence-маркеры не попадают в content
    # Объединение range'ов покрывает все 50 строк (0..49).
    assert int(ranges[0].split("..")[0]) == 0
    assert int(ranges[-1].split("..")[1]) == 49


def test_table_atomic_when_fits():
    md = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
    [chunk] = list(_chunker(size=200).stream(_ctx(), iter([_section(md)])))
    assert chunk.content == md
    assert chunk.metadata.get(MarkdownStructuralKeys.BLOCK_TYPE) == "table"


def test_table_overflow_split_row_by_row_with_replicated_header():
    """Большая таблица → много чанков, header в metadata.TABLE_HEADER каждого."""
    rows = "\n".join(f"| {i} | val_{i} |" for i in range(20))
    md = f"| id | name |\n|----|------|\n{rows}"
    chunks = list(_chunker(size=80).stream(_ctx(), iter([_section(md)])))
    assert len(chunks) > 1
    expected_header = "| id | name |\n|----|------|"
    ranges = []
    for c in chunks:
        assert (
            c.metadata.get(MarkdownStructuralKeys.TABLE_HEADER)
            == expected_header
        )
        assert c.metadata.get(MarkdownStructuralKeys.BLOCK_TYPE) == "table"
        rng = c.metadata.get(MarkdownStructuralKeys.TABLE_ROW_RANGE) or ""
        assert ".." in rng
        ranges.append(rng)
    # Объединение range'ов покрывает все 20 data-строк (0..19).
    assert int(ranges[0].split("..")[0]) == 0
    assert int(ranges[-1].split("..")[1]) == 19


def test_table_overflow_first_chunk_includes_header_in_content():
    """Первый row-чанк имеет slice от начала таблицы — header в content."""
    rows = "\n".join(f"| {i} | val_{i} |" for i in range(20))
    md = f"| id | name |\n|----|------|\n{rows}"
    chunks = list(_chunker(size=80).stream(_ctx(), iter([_section(md)])))
    assert chunks[0].content.startswith("| id | name |")
    # Последующие чанки — только data-строки.
    assert not chunks[1].content.startswith("| id | name |")


def test_list_atomic_with_ordered_flag():
    md = "1. one\n2. two\n3. three"
    [chunk] = list(_chunker(size=200).stream(_ctx(), iter([_section(md)])))
    assert chunk.content == md
    assert chunk.metadata.get(MarkdownStructuralKeys.BLOCK_TYPE) == "list"
    assert chunk.metadata.get(MarkdownStructuralKeys.LIST_ORDERED) is True


def test_list_overflow_split_item_by_item():
    """Большой список → много чанков, LIST_ITEM_RANGE покрывает все items."""
    items = "\n".join(f"- item number {i} with extra text" for i in range(20))
    chunks = list(_chunker(size=80).stream(_ctx(), iter([_section(items)])))
    assert len(chunks) > 1
    ranges = []
    for c in chunks:
        assert c.metadata.get(MarkdownStructuralKeys.BLOCK_TYPE) == "list"
        rng = c.metadata.get(MarkdownStructuralKeys.LIST_ITEM_RANGE) or ""
        assert ".." in rng
        ranges.append(rng)
    assert int(ranges[0].split("..")[0]) == 0
    assert int(ranges[-1].split("..")[1]) == 19


def test_list_overflow_chunk_content_is_slice_of_section():
    """Каждый item-чанк — slice исходного Section.content без репликации."""
    items_md = "\n".join(f"- item number {i} with extra text" for i in range(20))
    chunks = list(_chunker(size=80).stream(_ctx(), iter([_section(items_md)])))
    for c in chunks:
        assert items_md[c.location.start : c.location.end] == c.content


def test_paragraph_atomic_when_fits():
    md = "short paragraph."
    [chunk] = list(_chunker(size=200).stream(_ctx(), iter([_section(md)])))
    assert chunk.content == md
    assert chunk.metadata.get(MarkdownStructuralKeys.BLOCK_TYPE) == "paragraph"


def test_paragraph_split_with_overlap_when_too_large():
    md = " ".join(["word"] * 100)  # ~500 chars
    chunks = list(_chunker(size=80, overlap=20).stream(_ctx(), iter([_section(md)])))
    assert len(chunks) > 1
    # Все чанки имеют BLOCK_TYPE=paragraph и slice-инвариант.
    for c in chunks:
        assert c.metadata.get(MarkdownStructuralKeys.BLOCK_TYPE) == "paragraph"
        assert md[c.location.start : c.location.end] == c.content


# ----------------------------- horizontal rule skipped -------------------------


def test_horizontal_rule_skipped():
    md = "para before.\n\n---\n\npara after."
    chunks = list(_chunker(size=200).stream(_ctx(), iter([_section(md)])))
    types = [c.metadata.get(MarkdownStructuralKeys.BLOCK_TYPE) for c in chunks]
    # Только paragraph'ы; hr эмиттится skip.
    assert types == ["paragraph", "paragraph"]


def test_chunk_index_continues_across_sections_of_same_source():
    md1 = "# A\n\npara A."
    md2 = "# B\n\npara B."
    sections = iter(
        [
            _section(md1, anchor="a"),
            _section(md2, anchor="b"),
        ]
    )
    chunks = list(_chunker(size=200).stream(_ctx(), sections))
    # chunk_index сквозной (как в SectionChunker).
    assert [c.chunk_index for c in chunks] == [0, 1]


def test_empty_section_yields_nothing():
    chunks = list(_chunker().stream(_ctx(), iter([_section("")])))
    assert chunks == []
