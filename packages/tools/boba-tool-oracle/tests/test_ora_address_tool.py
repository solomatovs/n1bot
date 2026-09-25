"""ora_address: базовый url соединения из профиля, без похода в базу; нормализация
текста команды для ora_query."""

from __future__ import annotations

import uuid

import pytest
from pydantic import SecretStr

from boba.db.oracle.connection import OracleConfig, PasswordAuth
from boba.tool.ora.tools import ora_address
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.anyio]


def _profile(**parts: object) -> OracleConfig:
    return OracleConfig.model_validate(
        {
            "host": "db1",
            "port": 1521,
            "service": "orclpdb1",
            "connect_timeout": 10,
            "call_timeout": 30000,
            "arraysize": 2000,
            "auth": PasswordAuth(
                method="password", user="app", password=SecretStr("x")
            ),
            **parts,
        }
    ).identified(connection_id=uuid.uuid4(), name="dwh")


async def test_address_of_profile() -> None:
    body = ToolMain.toolset(ora_address)[0].coroutine
    if body is None:
        raise AssertionError("tool body is a coroutine")

    result = await body(connection=_profile(host="dwh.local", port=1522))

    assert [dict(row) for row in result.rows] == [
        {"connection": "dwh", "url": "oracle://dwh.local:1522/orclpdb1"}
    ]
