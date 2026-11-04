"""Готовый набор ридеров документов и признаков их форматов.

Ошибки:
DocumentError — регистрации форматов конфликтуют.
"""

from __future__ import annotations

import io
from functools import partial

from PIL import Image, UnidentifiedImageError

from boba.doc.config import DocConfig
from boba.doc.document import FormatSpec, OcrEngine
from boba.doc.html import HeadingStyle, HtmlDocument
from boba.doc.readers import (
    DocxDocument,
    ImageDocument,
    PdfDocument,
    PptxDocument,
    RtfDocument,
    TextDocument,
    XlsDocument,
    XlsxDocument,
)
from boba.doc.router import DocumentRouter, ReaderSpec


class StandardDocuments:
    """Подключает встроенные ридеры к общему роутеру с кодировками и OCR приложения."""

    def __init__(self, config: DocConfig, ocr: OcrEngine) -> None:
        self.readers = (
            ReaderSpec(
                FormatSpec(
                    PdfDocument.KIND,
                    ("application/pdf",),
                    ("pdf",),
                    recognize=self._pdf,
                ),
                partial(PdfDocument.open, ocr=ocr),
            ),
            ReaderSpec(
                FormatSpec(
                    DocxDocument.KIND,
                    (
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    ),
                    ("docx",),
                    recognize=partial(self._office, entry=b"word/"),
                ),
                partial(DocxDocument.open, ocr=ocr),
            ),
            ReaderSpec(
                FormatSpec(
                    XlsxDocument.KIND,
                    (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    ),
                    ("xlsx", "xlsm"),
                    recognize=partial(self._office, entry=b"xl/"),
                ),
                XlsxDocument.open,
            ),
            ReaderSpec(
                FormatSpec(
                    PptxDocument.KIND,
                    (
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    ),
                    ("pptx",),
                    recognize=partial(self._office, entry=b"ppt/"),
                ),
                partial(PptxDocument.open, ocr=ocr),
            ),
            ReaderSpec(
                FormatSpec(
                    XlsDocument.KIND,
                    ("application/vnd.ms-excel",),
                    ("xls",),
                    recognize=self._xls,
                ),
                XlsDocument.open,
            ),
            ReaderSpec(
                FormatSpec(
                    RtfDocument.KIND,
                    ("application/rtf", "text/rtf"),
                    ("rtf",),
                    recognize=self._rtf,
                ),
                partial(RtfDocument.open, encodings=config.text_encodings),
            ),
            ReaderSpec(
                FormatSpec(
                    HtmlDocument.KIND,
                    ("text/html", "application/xhtml+xml"),
                    ("htm", "html", "xhtml"),
                    recognize=self._html,
                ),
                partial(HtmlDocument.open, heading_style=HeadingStyle.ATX),
            ),
            ReaderSpec(
                FormatSpec(
                    TextDocument.KIND,
                    ("application/json", "application/xml", "application/x-yaml"),
                    (
                        "txt",
                        "md",
                        "csv",
                        "tsv",
                        "log",
                        "json",
                        "xml",
                        "yaml",
                        "yml",
                        "sql",
                    ),
                    ("text/",),
                ),
                partial(TextDocument.open, encodings=config.text_encodings),
            ),
            ReaderSpec(
                FormatSpec(
                    ImageDocument.KIND,
                    (
                        "image/png",
                        "image/jpeg",
                        "image/gif",
                        "image/bmp",
                        "image/webp",
                        "image/tiff",
                    ),
                    ("png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff"),
                    ("image/",),
                    self._image,
                ),
                partial(ImageDocument.open, ocr=ocr),
            ),
        )
        self.router = DocumentRouter(self.readers)

    @staticmethod
    def _pdf(head: bytes) -> bool:
        return head.startswith(b"%PDF")

    @staticmethod
    def _rtf(head: bytes) -> bool:
        return head.startswith(b"{\\rtf")

    @staticmethod
    def _xls(head: bytes) -> bool:
        return head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")

    @staticmethod
    def _office(head: bytes, entry: bytes) -> bool:
        if not head.startswith(b"PK\x03\x04"):
            return False

        return entry in head

    @staticmethod
    def _html(head: bytes) -> bool:
        opening = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        return opening.startswith((b"<!doctype html", b"<html"))

    @staticmethod
    def _image(head: bytes) -> bool:
        try:
            with Image.open(io.BytesIO(head)):
                return True
        except (UnidentifiedImageError, OSError, ValueError):
            return False
