"""Фикстуры: роутер без OCR, стенд с моделями и движок OCR на сессию."""

from __future__ import annotations

import pytest

from boba.doc import DisabledOcr, DocConfig, DocumentRouter
from boba.doc.ocr import OcrLanguage, RapidOcrConfig, RapidOcrEngine
from boba.stand.doc import DocStand


@pytest.fixture
def router() -> DocumentRouter:
    config = DocConfig(spool_memory_limit=1 << 20, text_encodings=("utf-8", "cp1251"))

    return DocumentRouter(config, DisabledOcr())


@pytest.fixture(scope="session")
def doc_stand() -> DocStand:
    return DocStand.required()


@pytest.fixture(scope="session")
def ocr_engine(doc_stand: DocStand) -> RapidOcrEngine:
    config = RapidOcrConfig(
        provider="rapidocr",
        models_dir=doc_stand.ocr_models_dir,
        language=OcrLanguage.ESLAV,
        text_score=0.5,
        threads=2,
    )

    return RapidOcrEngine(config)


@pytest.fixture
def ocr_router(ocr_engine: RapidOcrEngine) -> DocumentRouter:
    config = DocConfig(spool_memory_limit=1 << 20, text_encodings=("utf-8",))

    return DocumentRouter(config, ocr_engine)
