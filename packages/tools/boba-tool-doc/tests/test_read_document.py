"""Тесты doc-tool'ов поверх реального liteparse (DocEngine + _Search)."""

from __future__ import annotations

import pytest

from boba.tool.doc._engine import DocEngine
from boba.tool.doc.config import DocPluginConfig
from boba.tool.doc.search import _Search

# Двухстраничный PDF: стр.1 "Alpha page one", стр.2 "Beta page two Alpha again".
_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R 6 0 R]/Count 2>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 4 0 R\
/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 50>>stream
BT /F1 20 Tf 20 200 Td (Alpha page one) Tj ET
endstream endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
6 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 7 0 R\
/Resources<</Font<</F1 5 0 R>>>>>>endobj
7 0 obj<</Length 60>>stream
BT /F1 20 Tf 20 200 Td (Beta page two Alpha again) Tj ET
endstream endobj
trailer<</Root 1 0 R/Size 8>>
%%EOF"""


@pytest.fixture
def cfg() -> DocPluginConfig:
    return DocPluginConfig(ocr_enabled=False)


def test_parse_full_text(cfg: DocPluginConfig):
    result = DocEngine.parse(cfg, _PDF, "doc.pdf")
    assert result.num_pages == 2
    assert "Alpha page one" in result.text
    assert "Beta page two" in result.text


def test_target_pages_selects_subset(cfg: DocPluginConfig):
    result = DocEngine.parse(cfg, _PDF, "doc.pdf", target_pages="2")
    assert [p.page_num for p in result.pages] == [2]
    assert "Beta page two" in result.text
    assert "page one" not in result.text


def test_clip_truncates():
    assert DocEngine.clip("abc", 10) == ("abc", False)
    assert DocEngine.clip("abcdef", 3) == ("abc", True)


def test_window_middle_has_more():
    chunk, end, total, has_more = DocEngine.window("abcdefghij", 2, 3)
    assert chunk == "cde"
    assert (end, total, has_more) == (5, 10, True)


def test_window_to_end_no_more():
    chunk, end, total, has_more = DocEngine.window("abcdefghij", 7, 100)
    assert chunk == "hij"
    assert (end, total, has_more) == (10, 10, False)


def test_window_beyond_end_empty():
    chunk, end, total, has_more = DocEngine.window("abc", 10, 5)
    assert chunk == ""
    assert (end, total, has_more) == (10, 3, False)


def test_search_finds_matches_on_both_pages(cfg: DocPluginConfig):
    native = DocEngine.parse_native(cfg, _PDF, "doc.pdf")
    rows = _Search.run(native, "alpha", context=10, max_matches=50)
    assert [r["page"] for r in rows] == [1, 2]
    assert all(set(r) >= {"page", "x", "y", "width", "height", "snippet"} for r in rows)
    assert rows[0]["width"] > 0


def test_search_respects_max_matches(cfg: DocPluginConfig):
    native = DocEngine.parse_native(cfg, _PDF, "doc.pdf")
    rows = _Search.run(native, "alpha", context=10, max_matches=1)
    assert len(rows) == 1


def test_search_no_match(cfg: DocPluginConfig):
    native = DocEngine.parse_native(cfg, _PDF, "doc.pdf")
    assert _Search.run(native, "zzz", context=10, max_matches=50) == []
