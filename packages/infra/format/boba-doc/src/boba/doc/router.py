"""Точка входа чтения документов: вид по подсказке транспорта и первым
байтам, выбор ридера формата, время жизни открытого документа.

Ошибки:
DocumentError — вид не определён или не поддержан, документ не открыт.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from pydantic import BaseModel, ConfigDict, Field

from boba.doc.document import (
    ByteStream,
    Document,
    DocumentError,
    DocumentHint,
    DocumentKind,
    Formats,
    OcrEngine,
    Prefixed,
)
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

__all__ = ["DocConfig", "DocumentRouter"]


class DocConfig(BaseModel):
    """Настройки чтения: порог буфера в памяти для форматов с произвольным
    доступом и кодировки текстовых файлов по порядку перебора."""

    model_config = ConfigDict(frozen=True)

    spool_memory_limit: int = Field(gt=0)
    text_encodings: Sequence[str] = Field(min_length=1)


class DocumentRouter:
    """Открывает документ по потоку байтов: читает голову, определяет вид и
    отдаёт его ридеру формата. Один экземпляр на процесс, OCR-движок общий."""

    def __init__(self, config: DocConfig, ocr: OcrEngine) -> None:
        self._config = config
        self._ocr = ocr

    @contextmanager
    def open(self, stream: ByteStream, hint: DocumentHint) -> Iterator[Document]:
        head = stream.read(Formats.HEAD_SIZE)
        kind = Formats.detect(hint, head)
        if kind is DocumentKind.UNKNOWN:
            raise DocumentError(
                f"document {hint.filename!r} ({hint.media_type!r}): format not "
                f"recognized by media type, filename or leading bytes"
            )

        with self.open_as(kind, Prefixed(head, stream)) as document:
            yield document

    @contextmanager
    def open_as(self, kind: DocumentKind, stream: ByteStream) -> Iterator[Document]:
        document = self._open(kind, stream)
        try:
            yield document
        finally:
            document.close()

    def _open(  # noqa: PLR0911 — по ветке match на каждый вид
        self, kind: DocumentKind, stream: ByteStream
    ) -> Document:
        limit = self._config.spool_memory_limit
        encodings = self._config.text_encodings
        match kind:
            case DocumentKind.PDF:
                return PdfDocument.open(stream, limit, self._ocr)
            case DocumentKind.DOCX:
                return DocxDocument.open(stream, limit)
            case DocumentKind.XLSX:
                return XlsxDocument.open(stream, limit)
            case DocumentKind.PPTX:
                return PptxDocument.open(stream, limit)
            case DocumentKind.XLS:
                return XlsDocument.open(stream)
            case DocumentKind.RTF:
                return RtfDocument.open(stream, encodings)
            case DocumentKind.TEXT:
                return TextDocument.open(stream, encodings)
            case DocumentKind.IMAGE:
                return ImageDocument.open(stream, limit, self._ocr)
            case DocumentKind.UNKNOWN:
                raise DocumentError(
                    "document: kind is unknown, nothing to open it with"
                )
