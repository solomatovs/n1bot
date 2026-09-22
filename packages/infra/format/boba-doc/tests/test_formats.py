"""Formats: вид документа по media_type, имени файла и первым байтам."""

from __future__ import annotations

from pathlib import Path

import pytest
from samples import Samples

from boba.doc import DocumentHint, DocumentKind, Formats


@pytest.mark.parametrize(
    ("media_type", "filename", "expected"),
    [
        ("application/pdf", "", DocumentKind.PDF),
        ("application/pdf; charset=binary", "x.docx", DocumentKind.PDF),
        ("", "report.DOCX", DocumentKind.DOCX),
        ("application/octet-stream", "data.xlsx", DocumentKind.XLSX),
        ("image/png", "", DocumentKind.IMAGE),
        ("text/plain; charset=utf-8", "", DocumentKind.TEXT),
        ("", "notes.md", DocumentKind.TEXT),
        ("", "old.xls", DocumentKind.XLS),
        ("application/rtf", "", DocumentKind.RTF),
        ("application/octet-stream", "archive.zip", DocumentKind.UNKNOWN),
        ("", "", DocumentKind.UNKNOWN),
    ],
)
def test_kind_of_hint(media_type: str, filename: str, expected: DocumentKind) -> None:
    hint = DocumentHint(media_type=media_type, filename=filename)

    assert Formats.of_hint(hint) is expected


def test_sniff_office_zip_pdf_rtf_ole(tmp_path: Path) -> None:
    head_size = Formats.HEAD_SIZE
    cases = {
        DocumentKind.PDF: Samples.pdf(["one"]),
        DocumentKind.DOCX: Samples.docx(["para"], []),
        DocumentKind.XLSX: Samples.xlsx({"Sheet": [["a"]]}),
        DocumentKind.PPTX: Samples.pptx(["slide"], [], ""),
        DocumentKind.XLS: Samples.xls({"Sheet": [["a"]]}),
        DocumentKind.RTF: Samples.rtf("text"),
    }
    for expected, data in cases.items():
        assert Formats.sniff(data[:head_size]) is expected, expected


def test_sniff_image_and_garbage(doc_stand) -> None:
    png = Samples.png(["hello"], doc_stand.cyrillic_font)

    assert Formats.sniff(png[: Formats.HEAD_SIZE]) is DocumentKind.IMAGE
    assert Formats.sniff(b"\x00\x01\x02 nothing here") is DocumentKind.UNKNOWN
    assert Formats.sniff(b"") is DocumentKind.UNKNOWN


def test_detect_prefers_hint_over_bytes() -> None:
    hint = DocumentHint(media_type="text/plain")

    assert Formats.detect(hint, b"%PDF-1.4") is DocumentKind.TEXT
    assert Formats.detect(DocumentHint(), b"%PDF-1.4") is DocumentKind.PDF
