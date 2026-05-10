"""Markdown parser: collect_headings + split_sections + slug + edge cases."""

from __future__ import annotations

from boba.markdown.sections import (
    anchor_for,
    collect_headings,
    resolve_anchor,
    slugify,
    split_sections,
)


def test_collect_headings_atx_levels():
    text = "# A\n## B\n### C\n"
    hs = collect_headings(text)
    assert [h.level for h in hs] == [1, 2, 3]
    assert [h.text for h in hs] == ["A", "B", "C"]
    assert [h.index for h in hs] == [1, 2, 3]


def test_collect_headings_skip_in_code_fence():
    text = "# Real\n```python\n# not heading\n```\n## Real2\n"
    hs = collect_headings(text)
    assert [h.text for h in hs] == ["Real", "Real2"]


def test_collect_headings_max_depth():
    text = "# A\n### B\n##### C\n"
    hs = collect_headings(text, max_depth=3)
    assert [h.level for h in hs] == [1, 3]


def test_anchor_slug_from_text():
    hs = collect_headings("# Foo Bar 123\n")
    assert hs[0].anchor == "foo-bar-123"


def test_anchor_fallback_idx_when_slug_empty():
    hs = collect_headings("# !!!\n")
    assert hs[0].anchor is None
    assert anchor_for(hs[0]) == "idx:1"


def test_split_sections_basic():
    text = "# A\nalpha\n\n## B\nbeta gamma\n"
    secs = split_sections(text)
    assert len(secs) == 2
    assert secs[0].heading is not None
    assert secs[0].heading.text == "A"
    assert secs[0].body == "alpha"
    assert secs[1].heading is not None
    assert secs[1].heading.text == "B"
    assert "beta gamma" in secs[1].body


def test_split_sections_preamble_becomes_anchorless():
    text = "intro line\n\n# A\nbody\n"
    secs = split_sections(text)
    assert len(secs) == 2
    assert secs[0].heading is None
    assert "intro line" in secs[0].body
    assert secs[1].heading is not None
    assert secs[1].heading.text == "A"


def test_split_sections_no_headings_yields_single_anchorless():
    text = "just text\nmore text\n"
    secs = split_sections(text)
    assert len(secs) == 1
    assert secs[0].heading is None
    assert "just text" in secs[0].body


def test_split_sections_empty_text_yields_nothing():
    assert split_sections("") == []
    assert split_sections("   \n  \n") == []


def test_split_sections_no_cross_heading_bleed():
    text = "# One\nalpha\n## Two\nbeta\n# Three\ngamma\n"
    secs = split_sections(text)
    assert "beta" not in secs[0].body
    assert "gamma" not in secs[1].body
    assert "alpha" not in secs[1].body


def test_resolve_anchor_by_slug_or_idx():
    hs = collect_headings("# Foo\n## Bar\n")
    assert resolve_anchor(hs, "foo") is hs[0]
    assert resolve_anchor(hs, "#bar") is hs[1]
    assert resolve_anchor(hs, "idx:2") is hs[1]
    assert resolve_anchor(hs, "missing") is None


def test_slugify_strips_punctuation_and_lowercases():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("  spaced  ") == "spaced"
    assert slugify("!!") is None
    assert slugify("") is None


def test_atx_closing_hashes_ignored():
    """`## Title ##` — закрывающие решётки в title не считаются."""
    hs = collect_headings("## Title ##\n")
    assert hs[0].text == "Title"
