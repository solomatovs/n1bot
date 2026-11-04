"""Formats: вид документа по media_type, имени файла и первым байтам."""

from __future__ import annotations

import pytest
from samples import Samples

from boba.doc.document import DocumentHint, Formats
from boba.doc.router import DocumentRouter


@pytest.mark.parametrize(
    ("media_type", "filename", "expected"),
    [
        ("application/pdf", "", "pdf"),
        ("application/pdf; charset=binary", "x.docx", "pdf"),
        ("", "report.DOCX", "docx"),
        ("application/octet-stream", "data.xlsx", "xlsx"),
        ("image/png", "", "image"),
        ("text/plain; charset=utf-8", "", "text"),
        ("", "notes.md", "text"),
        ("", "old.xls", "xls"),
        ("application/rtf", "", "rtf"),
        ("text/html; charset=utf-8", "", "html"),
        ("application/xhtml+xml", "", "html"),
        ("application/octet-stream", "page.HTM", "html"),
        ("application/octet-stream", "archive.zip", None),
        ("", "", None),
    ],
)
def test_kind_of_hint(
    router: DocumentRouter, media_type: str, filename: str, expected: str | None
) -> None:
    hint = DocumentHint(media_type=media_type, filename=filename)

    assert router.formats.of_hint(hint) == expected


def test_sniff_office_zip_pdf_rtf_ole(router: DocumentRouter) -> None:
    head_size = Formats.HEAD_SIZE
    cases = {
        "pdf": Samples.pdf(["one"]),
        "docx": Samples.docx(["para"], []),
        "xlsx": Samples.xlsx({"Sheet": [["a"]]}),
        "pptx": Samples.pptx(["slide"], [], ""),
        "xls": Samples.xls({"Sheet": [["a"]]}),
        "rtf": Samples.rtf("text"),
    }
    for expected, data in cases.items():
        assert router.formats.sniff(data[:head_size]) == expected, expected


def test_sniff_html_by_doctype_or_root_tag(router: DocumentRouter) -> None:
    assert router.formats.sniff(b"\xef\xbb\xbf\n  <!DOCTYPE html><html>") == "html"
    assert router.formats.sniff(b"<HTML lang=en><body>x") == "html"
    assert router.formats.sniff(b"<div>fragment</div>") is None


def test_sniff_image_and_garbage(doc_stand, router: DocumentRouter) -> None:
    png = Samples.png(["hello"], doc_stand.cyrillic_font)

    assert router.formats.sniff(png[: Formats.HEAD_SIZE]) == "image"
    assert router.formats.sniff(b"\x00\x01\x02 nothing here") is None
    assert router.formats.sniff(b"") is None


def test_detect_prefers_hint_over_bytes(router: DocumentRouter) -> None:
    hint = DocumentHint(media_type="text/plain")

    assert router.formats.detect(hint, b"%PDF-1.4") == "text"
    assert router.formats.detect(DocumentHint(), b"%PDF-1.4") == "pdf"
