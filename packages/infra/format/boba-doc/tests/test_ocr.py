"""OCR на моделях PP-OCR: картинка, многокадровый tiff, скан-pdf через роутер."""

from __future__ import annotations

from pathlib import Path

import pytest
from samples import NoSeek, Samples

from boba.doc.config import OcrLanguage, RapidOcrConfig
from boba.doc.document import DocumentError, DocumentHint, PageWindow
from boba.doc.ocr import OcrEngines, RapidOcrEngine
from boba.doc.router import DocumentRouter
from boba.stand.doc import DocStand

LINES = ["Договор поставки от 12 мая", "Quarterly report 2026 total 1250"]


def test_png_russian_and_english_lines(
    ocr_router: DocumentRouter, doc_stand: DocStand
) -> None:
    data = Samples.png(LINES, doc_stand.cyrillic_font)

    with ocr_router.open(
        NoSeek(data), DocumentHint(media_type="image/png")
    ) as document:
        pages = list(document.pages(PageWindow.whole()))

    lines = pages[0].text.splitlines()
    assert len(lines) == 2
    assert "Договор" in lines[0]
    assert "поставки" in lines[0]
    assert "Quarterly" in lines[1]
    assert "1250" in lines[1]


def test_multiframe_tiff_pages(ocr_router: DocumentRouter, doc_stand: DocStand) -> None:
    data = Samples.tiff([["первый кадр"], ["second frame"]], doc_stand.cyrillic_font)

    with ocr_router.open(NoSeek(data), DocumentHint(filename="scan.tiff")) as document:
        assert document.page_count() == 2

        pages = list(document.pages(PageWindow(start=2, count=1)))

    assert pages[0].number == 2
    assert "second" in pages[0].text


def test_scanned_pdf_goes_through_ocr(
    ocr_router: DocumentRouter, doc_stand: DocStand
) -> None:
    data = Samples.scanned_pdf(["Счёт на оплату 2026"], doc_stand.cyrillic_font)

    with ocr_router.open(
        NoSeek(data), DocumentHint(media_type="application/pdf")
    ) as document:
        pages = list(document.pages(PageWindow.whole()))
        hits = list(
            document.search(
                "оплату", PageWindow.whole(), case_sensitive=False, context=80
            )
        )

    assert "оплату" in pages[0].text
    assert hits[0].page == 1


def test_missing_models_fail_with_names(tmp_path: Path) -> None:
    config = RapidOcrConfig(
        provider="rapidocr",
        models_dir=tmp_path,
        language=OcrLanguage.ESLAV,
        text_score=0.5,
        threads=1,
    )

    with pytest.raises(DocumentError, match=r"eslav_PP-OCRv5_rec_mobile\.onnx"):
        RapidOcrEngine(config)

    with pytest.raises(DocumentError, match="lacks"):
        OcrEngines.check(config)
