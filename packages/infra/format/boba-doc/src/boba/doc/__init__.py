"""Чтение документов по форматам окнами страниц из потока байтов без seek.

Приложению хватает секций конфига (boba.doc.config) и контракта документа
(boba.doc.document). Ридеры, роутер и мост живут за extra `readers`:
boba.doc.readers, boba.doc.router, boba.doc.bridge; OCR — за extra `ocr`
в boba.doc.ocr.

Ошибки:
OcrUnavailableError (boba.doc.config) — вызов просил OCR при provider = off.
DocumentError (boba.doc.document) — документ не распознан, не открыт или
    страница не прочитана.
"""

from boba.doc.config import (
    DisabledOcrConfig,
    DocConfig,
    DocSection,
    OcrConfig,
    OcrLanguage,
    OcrModel,
    OcrUnavailableError,
    OpenAiOcrConfig,
    RapidOcrConfig,
)

__all__ = [
    "DisabledOcrConfig",
    "DocConfig",
    "DocSection",
    "OcrConfig",
    "OcrLanguage",
    "OcrModel",
    "OcrUnavailableError",
    "OpenAiOcrConfig",
    "RapidOcrConfig",
]
