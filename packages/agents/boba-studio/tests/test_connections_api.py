"""/v1/connections: общие строки видны, свои (личный грант) создаются, правятся и
удаляются владельцем; секреты в ответах скрыты, маскированные назад не принимаются."""

from __future__ import annotations

import base64
import secrets as std_secrets
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import sql
from pydantic import BaseModel, SecretStr
from studio_stand import NoUsers, StandProfiles, StubAuthenticator, StubRefs

from boba.chat.profiles import ChatProfiles
from boba.connection_broker.store import ConnectionsConfig, ConnectionStore
from boba.connections.http import HttpProfile
from boba.connections.profile import GrantTarget
from boba.db.postgres import AsyncPostgresPool
from boba.runtime.config import StudioRuntimeConfig
from boba.studio.api.app import ApiAccess, ApiApp
from boba.studio.api.urls import ApiVersion, ConnectionUrl

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "connections_api"
ROLE = "api-reader"


def _secrets_of(model: BaseModel) -> list[str]:
    """Значения всех SecretStr модели на любой глубине: их в ответе быть не должно."""
    found: list[str] = []
    for value in model.__dict__.values():
        if isinstance(value, SecretStr):
            found.append(value.get_secret_value())
            continue

        if isinstance(value, BaseModel):
            found.extend(_secrets_of(value))

    return found


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(std_secrets.token_bytes(32)).decode())


def _profile_name(studio_config: StudioRuntimeConfig) -> str:
    for name, profile in studio_config.profiles.items():
        if profile.default:
            return name

    raise AssertionError("no default profile in the config")


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> ConnectionStore:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    cfg = ConnectionsConfig(enable=True, db_schema=SCHEMA, encryption_key=_key())
    built = ConnectionStore(cfg, pool)
    await built.setup()
    await built.sync_roles([ROLE])
    return built


@pytest.fixture
async def granted(store: ConnectionStore, studio_config: StudioRuntimeConfig) -> dict[str, UUID]:
    roles = await store.roles()
    postgres = await store.add("main", studio_config.data_layer.postgres)
    await store.grant(postgres, GrantTarget.role(roles[ROLE]))

    web = await store.add("site", HttpProfile(base_url="https://example.test"))
    await store.grant(web, GrantTarget.role(roles[ROLE]))

    stranger = await store.add("secret", HttpProfile(base_url="https://other.test"))
    await store.grant(stranger, GrantTarget.user(UUID(int=999_999)))

    return {"postgres": postgres, "web": web, "stranger": stranger}


def _query(studio_config: StudioRuntimeConfig, **extra: str) -> dict[str, str]:
    query = {"profile": _profile_name(studio_config)}
    query.update(extra)
    return query


def _web_body(name: str, url: str) -> dict[str, object]:
    return {
        "name": name,
        "profile": {"kind": "web", "base_url": url, "ssl_verify": False},
    }


@pytest.fixture
async def client(
    store: ConnectionStore, studio_config: StudioRuntimeConfig
) -> AsyncIterator[AsyncClient]:
    user = StandProfiles.user(studio_config).model_copy(update={"metadata": {"roles": [ROLE]}})
    access = ApiAccess(
        StubAuthenticator(user),
        StubAuthenticator.COOKIE,
        NoUsers.source,
    )
    app: FastAPI = ApiApp.build(
        StubRefs.of(lambda: store, lambda: None),
        access,
        ChatProfiles(studio_config.profiles),
        None,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api",
        cookies=StubAuthenticator.cookies(),
    ) as built:
        yield built


async def test_lists_granted_rows_with_masked_secrets(
    client: AsyncClient, granted: dict[str, int], studio_config: StudioRuntimeConfig
) -> None:
    reply = await client.get(
        f"{ApiVersion.V1}{ConnectionUrl.CONNECTIONS}",
        params={"profile": _profile_name(studio_config)},
    )

    assert reply.status_code == 200, reply.text
    rows = {row["name"]: row for row in reply.json()}
    assert set(rows) == {"main", "site"}
    assert rows["main"]["kind"] == "postgres"
    assert rows["site"]["kind"] == "web"
    assert rows["site"]["mine"] is False
    assert rows["site"]["profile"]["base_url"] == "https://example.test"

    for secret in _secrets_of(studio_config.data_layer.postgres):
        assert secret not in reply.text


async def test_kind_filter_narrows_the_list(
    client: AsyncClient, granted: dict[str, int], studio_config: StudioRuntimeConfig
) -> None:
    reply = await client.get(
        f"{ApiVersion.V1}{ConnectionUrl.CONNECTIONS}",
        params={"profile": _profile_name(studio_config), "kind": "web"},
    )

    assert reply.status_code == 200, reply.text
    assert [row["id"] for row in reply.json()] == [str(granted["web"])]


async def test_owner_creates_replaces_and_deletes_own_connection(
    client: AsyncClient, granted: dict[str, int], studio_config: StudioRuntimeConfig
) -> None:
    created = await client.post(
        f"{ApiVersion.V1}{ConnectionUrl.CONNECTIONS}",
        params=_query(studio_config),
        json=_web_body("own", "https://own.test"),
    )
    assert created.status_code == 200, created.text
    row = created.json()
    assert row["mine"] is True
    assert row["kind"] == "web"

    listed = await client.get(
        f"{ApiVersion.V1}{ConnectionUrl.CONNECTIONS}", params=_query(studio_config)
    )
    mine = {r["name"]: r["mine"] for r in listed.json()}
    assert mine == {"main": False, "site": False, "own": True}

    replaced = await client.put(
        f"{ApiVersion.V1}/connections/{row['id']}",
        params=_query(studio_config),
        json=_web_body("own-2", "https://own-2.test"),
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["name"] == "own-2"
    assert replaced.json()["profile"]["base_url"] == "https://own-2.test"

    deleted = await client.delete(
        f"{ApiVersion.V1}/connections/{row['id']}", params=_query(studio_config)
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True}

    gone = await client.delete(
        f"{ApiVersion.V1}/connections/{row['id']}", params=_query(studio_config)
    )
    assert gone.status_code == 404


async def test_shared_and_foreign_rows_are_not_editable(
    client: AsyncClient, granted: dict[str, int], studio_config: StudioRuntimeConfig
) -> None:
    shared = await client.put(
        f"{ApiVersion.V1}/connections/{granted['web']}",
        params=_query(studio_config),
        json=_web_body("site", "https://hijack.test"),
    )
    assert shared.status_code == 403, shared.text

    shared_delete = await client.delete(
        f"{ApiVersion.V1}/connections/{granted['web']}", params=_query(studio_config)
    )
    assert shared_delete.status_code == 403

    foreign = await client.delete(
        f"{ApiVersion.V1}/connections/{granted['stranger']}", params=_query(studio_config)
    )
    assert foreign.status_code == 404


async def test_name_must_be_free_among_visible_rows(
    client: AsyncClient, granted: dict[str, int], studio_config: StudioRuntimeConfig
) -> None:
    taken = await client.post(
        f"{ApiVersion.V1}{ConnectionUrl.CONNECTIONS}",
        params=_query(studio_config),
        json=_web_body("site", "https://dup.test"),
    )

    assert taken.status_code == 409, taken.text


async def test_masked_secret_is_rejected(
    client: AsyncClient, granted: dict[str, int], studio_config: StudioRuntimeConfig
) -> None:
    body = {
        "name": "pg-own",
        "profile": {
            "kind": "postgres",
            "host": "db.test",
            "port": 5432,
            "dbname": "boba",
            "auth": {"method": "password", "user": "u", "password": "**********"},
        },
    }

    reply = await client.post(
        f"{ApiVersion.V1}{ConnectionUrl.CONNECTIONS}",
        params=_query(studio_config),
        json=body,
    )

    assert reply.status_code == 422, reply.text
    assert "masked secret" in reply.text


async def test_check_of_a_stored_row_and_of_a_draft(
    client: AsyncClient, granted: dict[str, int], studio_config: StudioRuntimeConfig
) -> None:
    """Сохранённый postgres стенда открывается; черновик с недоступным хостом — нет."""
    stored = await client.post(
        f"{ApiVersion.V1}/connections/{granted['postgres']}/check",
        params=_query(studio_config),
    )
    assert stored.status_code == 200, stored.text
    assert stored.json()["ok"] is True
    assert "PostgreSQL" in stored.json()["message"]

    draft = await client.post(
        f"{ApiVersion.V1}{ConnectionUrl.CHECK}",
        params=_query(studio_config),
        json={
            "profile": {
                "kind": "web",
                "base_url": "http://127.0.0.1:9",
                "ssl_verify": False,
            }
        },
    )
    assert draft.status_code == 200, draft.text
    assert draft.json()["ok"] is False
    assert draft.json()["message"] != ""

    foreign = await client.post(
        f"{ApiVersion.V1}/connections/{granted['stranger']}/check",
        params=_query(studio_config),
    )
    assert foreign.status_code == 404
