"""Тесты LiteParseEngine поверх реального liteparse."""

from __future__ import annotations

import pytest

from boba.liteparse.engine import LiteParseEngine
from boba.text.document import LiteParseError, LiteParseParams

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

_TESSDATA = "/usr/share/tessdata"


@pytest.fixture
def params() -> LiteParseParams:
    return LiteParseParams(ocr_enabled=False, tessdata_path=_TESSDATA)


@pytest.fixture
def pdf_path(tmp_path) -> str:
    path = tmp_path / "doc.pdf"
    path.write_bytes(_PDF)
    return str(path)


def test_parse_full_text(params: LiteParseParams, pdf_path: str):
    result = LiteParseEngine.parse(params, pdf_path)
    assert result.num_pages == 2
    assert "Alpha page one" in result.text
    assert "Beta page two" in result.text


def test_parse_pages_selects_subset(params: LiteParseParams, pdf_path: str):
    result = LiteParseEngine.parse_pages(params, pdf_path, "2")
    assert [p.page_num for p in result.pages] == [2]
    assert "Beta page two" in result.text
    assert "page one" not in result.text


def test_parse_bytes_matches_parse(params: LiteParseParams):
    result = LiteParseEngine.parse_bytes(params, _PDF, "doc.pdf")
    assert result.num_pages == 2
    assert "Alpha page one" in result.text


def test_parse_invalid_raises_liteparse_error(params: LiteParseParams):
    with pytest.raises(LiteParseError):
        LiteParseEngine.parse_bytes(params, b"not a real pdf", "broken.pdf")


def test_ocr_without_tessdata_raises(pdf_path: str):
    params = LiteParseParams(
        ocr_enabled=True, tessdata_path="/нет-такого-каталога"
    )
    with pytest.raises(LiteParseError, match="каталога моделей"):
        LiteParseEngine.parse(params, pdf_path)


def test_native_search_items_finds_on_both_pages(
    params: LiteParseParams, pdf_path: str
):
    native = LiteParseEngine.parse_native(params, pdf_path)
    pages_with_hit = [
        page.page_num
        for page in native.pages
        if LiteParseEngine.search_items(page.text_items, "alpha")
    ]
    assert pages_with_hit == [1, 2]
