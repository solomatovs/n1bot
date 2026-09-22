"""Ридеры через роутер: поток без seek, окна страниц, карта, поиск, хэш."""

from __future__ import annotations

import hashlib

import pytest
from samples import NoSeek, PipeSource, Samples

from boba.doc import (
    BoxedHit,
    DisabledOcr,
    DocConfig,
    DocumentError,
    DocumentHint,
    DocumentKind,
    DocumentRouter,
    PageWindow,
    Sha256Stream,
    SizedPageInfo,
)

PDF_HINT = DocumentHint(media_type="application/pdf")


def test_pdf_pages_window_outline_and_boxed_search(router: DocumentRouter) -> None:
    data = Samples.pdf(["first page", "second page", "third page"])

    with router.open(NoSeek(data), PDF_HINT) as document:
        assert document.kind is DocumentKind.PDF
        assert document.page_count() == 3

        pages = list(document.pages(PageWindow(start=2, count=5)))
        assert [page.number for page in pages] == [2, 3]
        assert pages[0].text.strip() == "second page"

        outline = document.outline()
        assert len(outline) == 3
        assert isinstance(outline[0], SizedPageInfo)
        assert outline[0].width == 612.0
        assert outline[0].chars > 0

        hits = list(document.search("PAGE", PageWindow.whole(), case_sensitive=False))
        assert [hit.page for hit in hits] == [1, 2, 3]
        assert isinstance(hits[1], BoxedHit)
        assert hits[1].width > 0
        assert "second page" in hits[1].snippet

        strict = list(document.search("PAGE", PageWindow.whole(), case_sensitive=True))
        assert strict == []


def test_pdf_from_real_pipe_descriptor(router: DocumentRouter) -> None:
    data = Samples.pdf(["piped"])

    with PipeSource.of(data) as source, router.open(source, PDF_HINT) as document:
        pages = list(document.pages(PageWindow.whole()))

    assert pages[0].text.strip() == "piped"


def test_spool_rolls_to_disk_when_memory_limit_is_tiny() -> None:
    config = DocConfig(spool_memory_limit=1, text_encodings=("utf-8",))
    router = DocumentRouter(config, DisabledOcr())
    data = Samples.pdf(["on disk"])

    with router.open(NoSeek(data), PDF_HINT) as document:
        text = next(iter(document.pages(PageWindow.whole()))).text

    assert text.strip() == "on disk"


def test_sha256_stream_covers_whole_file(router: DocumentRouter) -> None:
    data = Samples.pdf(["hashed"])
    stream = Sha256Stream(NoSeek(data))

    with router.open(stream, PDF_HINT) as document:
        assert document.page_count() == 1

    stream.exhaust()

    assert stream.hexdigest() == hashlib.sha256(data).hexdigest()


def test_docx_paragraphs_table_and_block_pages(router: DocumentRouter) -> None:
    paragraphs = [f"Paragraph {index}" for index in range(90)]
    table = [["name", "value"], ["alpha", "1"]]
    data = Samples.docx(paragraphs, table)
    hint = DocumentHint(filename="report.docx")

    with router.open(NoSeek(data), hint) as document:
        assert document.kind is DocumentKind.DOCX
        assert document.page_count() == 3

        pages = list(document.pages(PageWindow.whole()))
        assert pages[0].text.startswith("Paragraph 0\nParagraph 1")
        assert "Paragraph 89" in pages[2].text
        assert "name\tvalue\nalpha\t1" in pages[2].text

        hits = list(
            document.search("alpha", PageWindow(start=3, count=1), case_sensitive=True)
        )
        assert len(hits) == 1
        assert hits[0].page == 3


def test_xlsx_sheets_as_pages(router: DocumentRouter) -> None:
    sheets = {
        "Orders": [["id", "total"], [1, 250.5], [None, None], [2, None]],
        "Notes": [["Комментарий", "ok"]],
    }
    data = Samples.xlsx(sheets)
    hint = DocumentHint(media_type="application/octet-stream", filename="book.xlsx")

    with router.open(NoSeek(data), hint) as document:
        assert document.page_count() == 2

        pages = list(document.pages(PageWindow.whole()))
        assert pages[0].text == "Orders\nid\ttotal\n1\t250.5\n2"
        assert pages[1].text == "Notes\nКомментарий\tok"

        second = list(document.pages(PageWindow(start=2, count=1)))
        assert second[0].number == 2


def test_xls_sheets_as_pages(router: DocumentRouter) -> None:
    data = Samples.xls({"Old": [["a", 1], [None, "b"]], "Empty": []})
    hint = DocumentHint(media_type="application/vnd.ms-excel")

    with router.open(NoSeek(data), hint) as document:
        assert document.kind is DocumentKind.XLS
        assert document.page_count() == 2

        pages = list(document.pages(PageWindow.whole()))
        assert pages[0].text == "Old\na\t1.0\nb"
        assert pages[1].text == "Empty"


def test_pptx_slides_table_and_notes(router: DocumentRouter) -> None:
    data = Samples.pptx(["Intro", "Plan"], [["k", "v"], ["x", "y"]], "speaker notes")
    hint = DocumentHint(filename="deck.pptx")

    with router.open(NoSeek(data), hint) as document:
        assert document.page_count() == 3

        pages = list(document.pages(PageWindow.whole()))
        assert pages[0].text == "Intro"
        assert pages[1].text == "Plan"
        assert "Table slide" in pages[2].text
        assert "k\tv\nx\ty" in pages[2].text
        assert "speaker notes" in pages[2].text


def test_rtf_with_cp1251_escapes(router: DocumentRouter) -> None:
    data = Samples.rtf("Договор № 7, total 100")
    hint = DocumentHint(media_type="application/rtf")

    with router.open(NoSeek(data), hint) as document:
        pages = list(document.pages(PageWindow.whole()))

    assert pages[0].text.strip() == "Договор № 7, total 100"


def test_text_encodings_in_order(router: DocumentRouter) -> None:
    hint = DocumentHint(media_type="text/plain")

    with router.open(NoSeek("привет".encode()), hint) as document:
        assert next(iter(document.pages(PageWindow.whole()))).text == "привет"

    with router.open(NoSeek("привет".encode("cp1251")), hint) as document:
        assert next(iter(document.pages(PageWindow.whole()))).text == "привет"

    strict = DocumentRouter(
        DocConfig(spool_memory_limit=1 << 20, text_encodings=("utf-8",)), DisabledOcr()
    )
    broken = NoSeek("привет".encode("cp1251"))
    with pytest.raises(DocumentError, match="cannot decode"), strict.open(broken, hint):
        pass


def test_image_without_ocr_gives_empty_text(router: DocumentRouter, doc_stand) -> None:
    data = Samples.png(["ignored"], doc_stand.cyrillic_font)

    with router.open(NoSeek(data), DocumentHint(media_type="image/png")) as document:
        assert document.page_count() == 1

        outline = document.outline()
        assert isinstance(outline[0], SizedPageInfo)
        assert outline[0].width == 1000.0
        assert outline[0].chars == 0


def test_unknown_and_broken_documents_fail_with_context(router: DocumentRouter) -> None:
    garbage = NoSeek(b"\x00\x01 garbage")
    with (
        pytest.raises(DocumentError, match="format not recognized"),
        router.open(garbage, DocumentHint()),
    ):
        pass

    broken_pdf = NoSeek(b"%PDF-1.4 broken")
    with (
        pytest.raises(DocumentError, match="pdf document: opening failed"),
        router.open(broken_pdf, PDF_HINT),
    ):
        pass

    not_zip = NoSeek(b"PK\x03\x04 not a zip")
    with (
        pytest.raises(DocumentError, match="docx document: opening failed"),
        router.open_as(DocumentKind.DOCX, not_zip),
    ):
        pass


def test_search_rejects_empty_query(router: DocumentRouter) -> None:
    data = Samples.docx(["text"], [])

    with (
        router.open(NoSeek(data), DocumentHint(filename="a.docx")) as document,
        pytest.raises(DocumentError, match="non-empty query"),
    ):
        list(document.search("", PageWindow.whole(), case_sensitive=False))


def test_window_beyond_document_is_empty(router: DocumentRouter) -> None:
    data = Samples.pdf(["only"])

    with router.open(NoSeek(data), PDF_HINT) as document:
        assert list(document.pages(PageWindow(start=2, count=3))) == []
