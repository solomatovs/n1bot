"""Ручной прогон doc-инструментов: функции вызываются напрямую с явным cfg.

Путь к документу — хостовый: файл читается процессом теста, каталога
/workspace песочницы здесь нет. Лимиты парсера берутся из [tool.doc].
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.settings import bind
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

    PATH: ClassVar[str] = "/tmp/example.pdf"  # noqa: S108

    PAGES: ClassVar[str] = "1-5"

    QUERY: ClassVar[str] = "alpha"


@pytest.fixture(scope="module")
def doc_cfg(raw_config) -> DocToolSection:
    return bind(raw_config, path="tool.doc", model=DocToolSection)


async def test_run_read_document(doc_cfg: DocToolSection) -> None:
    body = ToolMain.toolset(read_document)[0].coroutine
    assert body is not None

    content, _artifact = await body(
        path=RunArgs.PATH, pages=RunArgs.PAGES, cfg=doc_cfg
    )

    print(content)


async def test_run_document_outline(doc_cfg: DocToolSection) -> None:
    body = ToolMain.toolset(document_outline)[0].coroutine
    assert body is not None

    content, _artifact = await body(path=RunArgs.PATH, cfg=doc_cfg)

    print(content)


async def test_run_search_document(doc_cfg: DocToolSection) -> None:
    body = ToolMain.toolset(search_document)[0].coroutine
    assert body is not None

    content, _artifact = await body(
        path=RunArgs.PATH, query=RunArgs.QUERY, cfg=doc_cfg
    )

    print(content)
