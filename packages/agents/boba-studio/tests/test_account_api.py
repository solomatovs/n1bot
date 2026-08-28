"""Ресурсы входа: /v1/me и /v1/profiles по cookie, без базы и реестра."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from studio_stand import NoRefs, StubAuthenticator

from boba.chat.openai import OpenAiConfig
from boba.chat.profiles import ChatProfileConfig, ChatProfiles
from boba.chat.provider import OpenAiChatConfig
from boba.identity.api import AuthenticatedUser
from boba.studio.api.app import ApiAccess, ApiApp
from boba.studio.api.urls import AccountUrl, ApiVersion, ConnectionUrl

pytestmark = pytest.mark.anyio


def _profile(default: bool, roles: list[str]) -> ChatProfileConfig:
    return ChatProfileConfig.model_validate(
        {
            "display_name": "Stand",
            "description": "stand profile",
            "default": default,
            "roles": roles,
            "tools": ["echo"],
            "backend": OpenAiChatConfig(
                provider="openai",
                openai=OpenAiConfig(base_url="https://fake-llm/v1", api_key="k"),
            ),
            "model": "fake",
            "system_prompt": "stand",
        }
    )


def _profiles() -> ChatProfiles:
    return ChatProfiles(
        {
            "general": _profile(default=True, roles=["*"]),
            "admin": _profile(default=False, roles=["ADM"]),
        }
    )


def _user(roles: list[str]) -> AuthenticatedUser:
    return AuthenticatedUser(
        id="7",
        identifier="reader",
        metadata={
            "roles": roles,
            "provider": "KerberosAuth",
            "principal": "reader@EXAMPLE",
            "sso_ticket": "sealed",
        },
    )


async def _client(user: AuthenticatedUser | None) -> AsyncClient:
    access = ApiAccess(
        StubAuthenticator(user),
        StubAuthenticator.COOKIE,
        NoRefs.store,  # type: ignore[arg-type]
    )
    app = ApiApp.build(NoRefs.refs(), access, _profiles(), None)

    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api",
        cookies=StubAuthenticator.cookies(),
    )


@pytest.fixture
async def reader() -> AsyncIterator[AsyncClient]:
    async with await _client(_user(["DEV"])) as client:
        yield client


@pytest.fixture
async def anonymous() -> AsyncIterator[AsyncClient]:
    async with await _client(None) as client:
        yield client


async def test_me_reports_subject_and_sign_in(reader: AsyncClient) -> None:
    reply = await reader.get(f"{ApiVersion.V1}{AccountUrl.ME}")

    assert reply.status_code == 200, reply.text
    assert reply.json() == {
        "id": 7,
        "login": "reader",
        "roles": ["DEV"],
        "profile": "general",
        "sign_in": {
            "provider": "KerberosAuth",
            "principal": "reader@EXAMPLE",
            "ticket": True,
        },
    }


async def test_me_refuses_a_profile_hidden_from_the_roles(reader: AsyncClient) -> None:
    reply = await reader.get(
        f"{ApiVersion.V1}{AccountUrl.ME}", params={"profile": "admin"}
    )

    assert reply.status_code == 403


async def test_profiles_lists_only_visible_ones(reader: AsyncClient) -> None:
    reply = await reader.get(f"{ApiVersion.V1}{AccountUrl.PROFILES}")

    assert reply.status_code == 200, reply.text
    views = reply.json()
    assert [view["name"] for view in views] == ["general"]
    assert views[0]["display_name"] == "Stand"
    assert views[0]["default"] is True
    assert views[0]["tools"] == ["echo"]
    assert "system_prompt" not in views[0]


async def test_anonymous_is_unauthorized(anonymous: AsyncClient) -> None:
    for path in (AccountUrl.ME, AccountUrl.PROFILES, ConnectionUrl.CONNECTIONS):
        reply = await anonymous.get(f"{ApiVersion.V1}{path}")

        assert reply.status_code == 401, path


async def test_connections_without_the_section_is_503(reader: AsyncClient) -> None:
    reply = await reader.get(f"{ApiVersion.V1}{ConnectionUrl.CONNECTIONS}")

    assert reply.status_code == 503
    assert "disabled" in reply.json()["detail"]


async def test_connection_schema_describes_kinds_and_secrets(
    reader: AsyncClient,
) -> None:
    reply = await reader.get(f"{ApiVersion.V1}{ConnectionUrl.SCHEMA}")

    assert reply.status_code == 200, reply.text
    schema = reply.json()
    assert schema["discriminator"]["propertyName"] == "kind"
    assert set(schema["discriminator"]["mapping"]) == {"postgres", "clickhouse", "web"}
    auth = schema["$defs"]["HttpProfile"]["properties"]["auth"]
    assert auth["discriminator"]["propertyName"] == "method"
    bearer = schema["$defs"]["BearerAuth"]["properties"]["token"]
    assert bearer["format"] == "password"
