"""Чтение документов по форматам окнами страниц из потока байтов без seek.

Ошибки:
DocumentError — документ не распознан, не открыт или страница не прочитана.
"""

from boba.doc.bridge import AsyncPipe
from boba.doc.document import (
    BoxedHit,
    ByteStream,
    DisabledOcr,
    Document,
    DocumentError,
    DocumentHint,
    DocumentKind,
    Formats,
    Hit,
    OcrEngine,
    PageInfo,
    PageWindow,
    ParsedPage,
    Sha256Stream,
    SizedPageInfo,
)
from boba.doc.router import DocConfig, DocumentRouter

__all__ = [
    "AsyncPipe",
    "BoxedHit",
    "ByteStream",
    "DisabledOcr",
    "DocConfig",
    "Document",
    "DocumentError",
    "DocumentHint",
    "DocumentKind",
    "DocumentRouter",
    "Formats",
    "Hit",
    "OcrEngine",
    "PageInfo",
    "PageWindow",
    "ParsedPage",
    "Sha256Stream",
    "SizedPageInfo",
]
