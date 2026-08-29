"""Таблица users приложения: studio создаёт свою схему сам и работает с ней теми же
запросами, что слой данных чата со своей.
"""

from __future__ import annotations

import pytest
from psycopg import sql

from boba.chainlit.infra.config import AppConfig
from boba.db.postgres import AsyncPostgresPool
from boba.identity.session import UserMetadataField
from boba.identity.signin import SignedIn
from boba.runtime.users import UsersTable

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "automation_test"


@pytest.fixture
async def users(
    app_config: AppConfig, test_database: str, pool: AsyncPostgresPool
) -> UsersTable:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    cfg = app_config.data_layer.postgres.model_copy(update={"dbname": test_database})
    table = UsersTable(cfg, SCHEMA, pool)
    await table.setup()
    await table.setup()
    return table


async def test_setup_creates_the_schema_and_users_round_trip(users: UsersTable) -> None:
    signed = SignedIn(
        identifier="reader", display_name="reader", metadata={"roles": ["DEV"]}
    )
    created = await users.ensure_user(signed)
    found = await users.get_user("reader")
    assert found is not None
    assert found.id == created.id
    assert found.metadata.get("roles") == ["DEV"]

    await users.set_studio_profile(int(created.id), "search")
    again = await users.get_user("reader")
    assert again is not None
    assert again.metadata.get(UserMetadataField.STUDIO_PROFILE) == "search"
    assert again.metadata.get("roles") == ["DEV"]
