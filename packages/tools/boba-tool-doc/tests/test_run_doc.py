"""Ручной прогон doc-инструментов: функции вызываются напрямую с явным cfg.

Путь к документу — хостовый: файл читается процессом теста, каталога
/workspace песочницы здесь нет. Лимиты парсера берутся из [tool.doc].

По умолчанию берётся образец стенда, чтобы прогон был самодостаточным; для
разбора своего документа пропишите его путь в RunArgs.PATH.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.config import bind
from boba.stand.samples import SamplePdf
from boba.tool.doc.tools import (
    DocToolSection,
    document_outline,
    read_document,
    search_document,
)
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    PATH: ClassVar[str] = ""
    """Свой документ; пусто — образец стенда."""

    PAGES: ClassVar[str] = "1-2"

    QUERY: ClassVar[str] = SamplePdf.WORD


@pytest.fixture(scope="module")
def doc_cfg(raw_config) -> DocToolSection:
    return bind(raw_config, path="tool.doc", model=DocToolSection)


@pytest.fixture(scope="module")
def document(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Разбираемый документ: свой из RunArgs или образец стенда."""
    if RunArgs.PATH:
        return RunArgs.PATH

    return str(SamplePdf.written(tmp_path_factory.mktemp("doc")))


async def test_run_read_document(doc_cfg: DocToolSection, document: str) -> None:
    body = ToolMain.toolset(read_document)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, _artifact = await body(path=document, pages=RunArgs.PAGES, cfg=doc_cfg)

    print(content)


async def test_run_document_outline(doc_cfg: DocToolSection, document: str) -> None:
    body = ToolMain.toolset(document_outline)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, _artifact = await body(path=document, cfg=doc_cfg)

    print(content)


async def test_run_search_document(doc_cfg: DocToolSection, document: str) -> None:
    body = ToolMain.toolset(search_document)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, _artifact = await body(path=document, query=RunArgs.QUERY, cfg=doc_cfg)

    print(content)
