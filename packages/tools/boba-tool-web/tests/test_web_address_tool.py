"""web_address: корневой url соединения без учётных данных, без запросов."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import SecretStr

from boba.tool.web.tools import web_address
from boba.toolkit.entry import ToolMain
from boba.transport.http.profile import HttpConnection

pytestmark = [pytest.mark.anyio]


async def test_root_url_without_credentials() -> None:
    profile = HttpConnection.model_validate(
        {
            "scheme": "https",
            "host": "wiki.corp",
            "port": 8443,
            "path": "/confluence",
            "username": "svc",
            "password": SecretStr("secret"),
        }
    ).identified(connection_id=uuid4(), name="wiki")

    body = ToolMain.toolset(web_address)[0].coroutine
    if body is None:
        raise AssertionError("tool body is a coroutine")

    result = await body(connection=profile)

    assert [dict(row) for row in result.rows] == [
        {"connection": "wiki", "url": "https://wiki.corp:8443/confluence"}
    ]
