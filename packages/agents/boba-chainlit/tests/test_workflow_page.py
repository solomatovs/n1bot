"""Раздача страницы workflow: штамп префикса в index.html, 404 без сборки."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from boba.chainlit.workflow.page import PageStamp, WorkflowPage

pytestmark = pytest.mark.anyio

INDEX = """<!doctype html>
<html><head><!--BOBA_PAGE--><title>t</title></head>
<body><div id="root"></div></body></html>
"""


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Страница не зависит от сессии chainlit."""


async def _get(app_root: Path, path: str) -> tuple[int, str]:
    app = FastAPI()
    WorkflowPage(str(app_root), "/boba-debug").mount(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://p") as c:
        reply = await c.get(path)

    return reply.status_code, reply.text


async def test_index_is_stamped_for_any_page_path(tmp_path: Path) -> None:
    public = tmp_path / WorkflowPage.PUBLIC_DIR
    public.mkdir(parents=True)
    (public / WorkflowPage.INDEX).write_text(INDEX)

    status, text = await _get(tmp_path, "/workflow/run/abc")

    assert status == 200
    assert '<base href="/boba-debug/public/workflow/">' in text
    assert '"prefix": "/boba-debug"' in text
    assert '"socketPath": "/boba-debug/ws/socket.io"' in text
    assert PageStamp.PLACEHOLDER not in text


async def test_missing_build_is_reported(tmp_path: Path) -> None:
    status, text = await _get(tmp_path, "/workflow/")

    assert status == 404
    assert "not built" in text
