"""Точка входа чтения документов: вид по подсказке транспорта и первым
байтам, выбор ридера формата, время жизни открытого документа и текст
документа целиком.

Ошибки:
DocumentError — вид не определён или не поддержан, документ не открыт
    или страница не прочитана.
"""

from __future__ import annotations

from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from typing import ClassVar

from boba.doc.config import DocConfig
from boba.doc.document import (
    ByteStream,
    Document,
    DocumentError,
    DocumentHint,
    DocumentKind,
    Formats,
    OcrEngine,
    PageWindow,
    ParsedPage,
    Prefixed,
)
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

__all__ = ["DocumentRouter"]


class DocumentRouter:
    """Открывает документ по потоку байтов: читает голову, определяет вид и
    отдаёт его ридеру формата; текст документа целиком склеивает из страниц.
    Один экземпляр на процесс, OCR-движок общий. Его создают тела инструментов
    doc/web/confluence и индексатор вложений."""

    PAGE_GLUE: ClassVar[str] = "\n\n"
    HEADING_STYLE: ClassVar[HeadingStyle] = HeadingStyle.ATX

    def __init__(self, config: DocConfig, ocr: OcrEngine) -> None:
        self._config = config
        self._ocr = ocr

    def detect(
        self, stream: ByteStream, hint: DocumentHint
    ) -> tuple[DocumentKind, ByteStream]:
        """Вид документа по подсказке и голове потока; голова возвращается
        в поток, который дальше читает ридер."""
        head = stream.read(Formats.HEAD_SIZE)
        kind = Formats.detect(hint, head)
        if kind is DocumentKind.UNKNOWN:
            raise DocumentError(
                f"document {hint.filename!r} ({hint.media_type!r}): format not "
                f"recognized by media type, filename or leading bytes"
            )

        return kind, Prefixed(head, stream)

    @contextmanager
    def open(
        self, stream: ByteStream, hint: DocumentHint
    ) -> Generator[Document, None, None]:
        kind, prefixed = self.detect(stream, hint)

        with self.open_as(kind, prefixed) as document:
            yield document

    def read_text(self, stream: ByteStream, hint: DocumentHint) -> str:
        """Текст всех страниц одним куском: пустые страницы опускаются."""
        kind, prefixed = self.detect(stream, hint)

        return self.read_text_as(kind, prefixed)

    def read_text_as(self, kind: DocumentKind, stream: ByteStream) -> str:
        with self.open_as(kind, stream) as document:
            return self._join(document.pages(PageWindow.whole()))

    def _join(self, pages: Iterable[ParsedPage]) -> str:
        return self.PAGE_GLUE.join(self._texts(pages))

    def _texts(self, pages: Iterable[ParsedPage]) -> Iterator[str]:
        for page in pages:
            text = page.text.strip()
            if not text:
                continue

            yield text

    @contextmanager
    def open_as(
        self, kind: DocumentKind, stream: ByteStream
    ) -> Generator[Document, None, None]:
        document = self._open(kind, stream)
        try:
            yield document
        finally:
            document.close()

    def _open(self, kind: DocumentKind, stream: ByteStream) -> Document:
        """Ридер по виду: бинарные форматы буферизуются спулом, текстовые
        читают поток до конца."""
        limit = self._config.spool_memory_limit
        match kind:
            case DocumentKind.PDF:
                return PdfDocument.open(stream, limit, self._ocr)
            case DocumentKind.DOCX:
                return DocxDocument.open(stream, limit, self._ocr)
            case DocumentKind.XLSX:
                return XlsxDocument.open(stream, limit)
            case DocumentKind.PPTX:
                return PptxDocument.open(stream, limit, self._ocr)
            case DocumentKind.XLS:
                return XlsDocument.open(stream)
            case DocumentKind.IMAGE:
                return ImageDocument.open(stream, limit, self._ocr)
            case DocumentKind.UNKNOWN:
                raise DocumentError(
                    "document: kind is unknown, nothing to open it with"
                )
            case _:
                return self._open_text(kind, stream)

    def _open_text(self, kind: DocumentKind, stream: ByteStream) -> Document:
        encodings = self._config.text_encodings
        match kind:
            case DocumentKind.RTF:
                return RtfDocument.open(stream, encodings)
            case DocumentKind.HTML:
                return HtmlDocument.open(stream, self.HEADING_STYLE)
            case _:
                return TextDocument.open(stream, encodings)
