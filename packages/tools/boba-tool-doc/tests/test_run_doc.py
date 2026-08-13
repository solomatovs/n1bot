"""Ручной прогон операций doc: DocumentOps вызывается напрямую.

Путь к документу — хостовый: файл читается процессом теста, каталога
/workspace песочницы здесь нет. Лимиты парсера берутся из [tool.doc].
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.settings import bind
from boba.tool.doc import DocToolsConfig
from boba.tool.doc.payload import DocumentOps
from boba.tool.doc.protocol import (
    DocPagesRequest,
    DocParams,
    DocPathRequest,
    DocSearchRequest,
)

pytestmark = [pytest.mark.run]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    PATH: ClassVar[str] = "/tmp/example.pdf"  # noqa: S108

    PAGES: ClassVar[str] = "1-5"

    QUERY: ClassVar[str] = "alpha"

    OCR_ENABLED: ClassVar[bool] = False

    NUM_WORKERS: ClassVar[int] = 1

    OCR_LANGUAGE: ClassVar[str] = "rus+eng"


@pytest.fixture(scope="module")
def doc_params(raw_config):
    cfg = bind(raw_config, path="tool.doc", model=DocToolsConfig)

    return DocParams(
        ocr_enabled=RunArgs.OCR_ENABLED,
        ocr_language=RunArgs.OCR_LANGUAGE,
        max_pages=cfg.max_pages,
        tessdata_path=cfg.tessdata_path,
        num_workers=RunArgs.NUM_WORKERS,
        max_text_chars=cfg.max_text_chars,
    )


@pytest.fixture(scope="module")
def doc_config(raw_config):
    return bind(raw_config, path="tool.doc", model=DocToolsConfig)


def test_run_read_document(doc_params, payload, chunks) -> None:
    request = DocPagesRequest(
        op=DocPagesRequest.OP,
        path=RunArgs.PATH,
        pages=RunArgs.PAGES,
        params=doc_params,
    )

    trailer = DocumentOps.read_document(payload.of(request), chunks.write)

    print(chunks.text())
    print(trailer)


def test_run_document_outline(doc_params, payload, chunks) -> None:
    request = DocPathRequest(
        op=DocPathRequest.OUTLINE,
        path=RunArgs.PATH,
        params=doc_params,
    )

    trailer = DocumentOps.document_outline(payload.of(request), chunks.write)

    print(chunks.rows())
    print(trailer)


def test_run_search_document(doc_config, doc_params, payload, chunks) -> None:
    request = DocSearchRequest(
        op=DocSearchRequest.OP,
        path=RunArgs.PATH,
        query=RunArgs.QUERY,
        context_chars=doc_config.search_context_chars,
        max_matches=doc_config.search_max_matches,
        params=doc_params,
    )

    trailer = DocumentOps.search_document(payload.of(request), chunks.write)

    print(chunks.rows())
    print(trailer)
