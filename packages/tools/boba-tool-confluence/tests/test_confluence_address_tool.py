"""confluence_address: корень сервиса из конфига и формы url его объектов."""

from __future__ import annotations

import pytest

from boba.tool.confluence.tools import ConfluenceToolsConfig, confluence_address
from boba.toolkit.entry import ToolMain
from boba.transport.http.profile import HttpConnection

pytestmark = [pytest.mark.anyio]


async def test_root_and_shapes() -> None:
    cfg = ConfluenceToolsConfig(
        confluence=HttpConnection.model_validate(
            {"scheme": "https", "host": "cwiki.apache.org", "path": "/confluence"}
        )
    )

    body = ToolMain.toolset(confluence_address)[0].coroutine
    if body is None:
        raise AssertionError("tool body is a coroutine")

    result = await body(cfg=cfg)

    assert [dict(row) for row in result.rows] == [
        {"url": "https://cwiki.apache.org/confluence"}
    ]
    assert result.note is not None
    assert "confluence_page:" in result.note
