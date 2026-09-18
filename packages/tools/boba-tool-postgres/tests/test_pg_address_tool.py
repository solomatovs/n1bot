"""pg_address: базовый url соединения из профиля, без похода в базу."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from boba.db.postgres.profile import (
    PasswordAuth,
    PostgresConfig,
    PostgresOptionsConfig,
    PostgresPoolConfig,
)
from boba.tool.pg.tools import pg_address
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.anyio]


def _profile(**parts: object) -> PostgresConfig:
    return PostgresConfig.model_validate(
        {
            "dbname": "dwh",
            "auth": PasswordAuth(
                method="password", user="app", password=SecretStr("x")
            ),
            "options": PostgresOptionsConfig(),
            "pool": PostgresPoolConfig(),
            **parts,
        }
    ).identified(connection_id=__import__("uuid").uuid4(), name="dwh")


async def test_address_of_profile() -> None:
    body = ToolMain.toolset(pg_address)[0].coroutine
    if body is None:
        raise AssertionError("tool body is a coroutine")

    result = await body(connection=_profile(host="dwh.local", port=6432))

    assert [dict(row) for row in result.rows] == [
        {"connection": "dwh", "url": "postgresql://dwh.local:6432/dwh"}
    ]


async def test_port_defaults_to_libpq() -> None:
    body = ToolMain.toolset(pg_address)[0].coroutine
    if body is None:
        raise AssertionError("tool body is a coroutine")

    result = await body(connection=_profile(host="dwh.local"))

    assert dict(result.rows[0])["url"] == "postgresql://dwh.local:5432/dwh"
