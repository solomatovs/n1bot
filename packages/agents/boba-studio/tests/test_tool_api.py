"""REST-запуск инструмента человеком: тот же реестр, контекст под HumanInitiator.

Стенд: реестр из одного инструмента-зонда, записывающего контекст вызова,
плюс инструмент чата в chat_only; тред и пользователь — в тестовой базе.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from langchain_core.tools import tool
from starlette.requests import Request
from studio_stand import StandProfiles

from boba.access import ProfileGrant, RoleConfig, ToolAccess
from boba.auth import AuthService, JwtTokens
from boba.chat.profiles import ChatProfiles
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import CallContext, HumanInitiator, ScopeKind
from boba.identity.errors import AuthenticationError, AuthorizationError
from boba.identity.locks import MemoryLiveLocks
from boba.identity.signin import SignedIn
from boba.identity.token import CookieSpec
from boba.runtime.config import StudioRuntimeConfig
from boba.runtime.plugins import CallSurface
from boba.runtime.users import UsersTable
from boba.stand.auth import StubAuthenticator
from boba.studio.api.auth import ApiAuth, RequestTokens
from boba.studio.api.tools import ToolCallBody, ToolCalling
from boba.toolkit.result import TextResult, pack_result
from boba.toolrun.call_id import ToolCallIdField
from boba.toolrun.errors import ToolErrorGuard
from boba.toolrun.intent import ToolIntentField
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.run_log import ToolRunLogger

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


class Probe:
    """Инструмент, отдающий наружу контекст, в котором его вызвали."""

    def __init__(self) -> None:
        self.seen: list[CallContext] = []

    def tools(self) -> list[Any]:
        seen = self.seen

        @tool(response_format="content_and_artifact")
        async def probe(query: str) -> tuple[str, Any]:
            """Зонд контекста вызова."""
            seen.append(CallContext.current())
            return pack_result(TextResult(text=f"seen {query}"))

        @tool(response_format="content_and_artifact")
        async def canvas_open(path: str) -> tuple[str, Any]:
            """Инструмент чата: сюда дойти не должно."""
            return pack_result(TextResult(text=path))

        # та же обвязка, что ставит load_tools: id и intent вызова, журнал, ошибки
        tools = [probe, canvas_open]
        ToolCallIdField.attach_all(tools)
        ToolIntentField.attach_all(tools)
        ToolRunLogger.guard_all(
            tools, CallSurface.stream_source, CallSurface.tool_call_scope
        )
        ToolErrorGuard.guard_all(tools)
        return tools


def _registry(probe: Probe, studio_config: StudioRuntimeConfig) -> ToolRegistry:
    tools = probe.tools()
    names: list[str] = []
    for tool_ in tools:
        names.append(tool_.name)

    roles: dict[str, RoleConfig] = {}
    for role in StandProfiles.roles(studio_config):
        roles[role] = RoleConfig(tools=["*"])

    access = ToolAccess(
        tool_names=names,
        roles=roles,
        profiles={
            StandProfiles.profile(studio_config): ProfileGrant(tools=["*"], roles=["*"])
        },
        chat_only=["canvas_open"],
    )
    return ToolRegistry(tools=tools, access=access)


def _calling(probe: Probe, studio_config: StudioRuntimeConfig) -> ToolCalling:
    async def registry() -> ToolRegistry:
        return _registry(probe, studio_config)

    return ToolCalling(
        registry,
        StandProfiles.profiles(studio_config),
        lambda: MemoryLiveLocks("test:0", 20),
        1.0,
    )


def _body(studio_config: StudioRuntimeConfig, **extra: Any) -> ToolCallBody:
    fields: dict[str, Any] = {
        "profile": StandProfiles.profile(studio_config),
        "intent": "probe the context",
        "args": {"query": "x"},
    }
    fields.update(extra)
    return ToolCallBody.model_validate(fields)


class TestServe:
    async def test_tool_runs_under_human_api_context(
        self, studio_config: StudioRuntimeConfig
    ) -> None:
        probe = Probe()
        user = StandProfiles.user(studio_config)

        reply = await _calling(probe, studio_config).serve(
            "probe", _body(studio_config), user
        )

        if not reply.ok or "seen x" not in reply.content:
            raise AssertionError(reply)

        context = probe.seen[0]
        if not isinstance(context.initiator, HumanInitiator):
            raise AssertionError(context.initiator)
        if context.initiator.via != "api":
            raise AssertionError(context.initiator)
        if context.subject.login != user.identifier:
            raise AssertionError(context.subject)
        if context.subject.profile != StandProfiles.profile(studio_config):
            raise AssertionError(context.subject)
        if not context.subject.roles >= frozenset(StandProfiles.roles(studio_config)):
            raise AssertionError(context.subject)
        if context.scope.kind is not ScopeKind.JOB:
            raise AssertionError(context.scope)

        if CallContext.peek() is not None:
            raise AssertionError("the call context must not outlive the call")

    async def test_chat_only_tool_is_refused(
        self, studio_config: StudioRuntimeConfig
    ) -> None:
        user = StandProfiles.user(studio_config)

        with pytest.raises(HTTPException) as caught:
            await _calling(Probe(), studio_config).serve(
                "canvas_open", _body(studio_config), user
            )

        if caught.value.status_code != 404:
            raise AssertionError(caught.value.status_code)

    async def test_unknown_profile_is_forbidden(
        self, studio_config: StudioRuntimeConfig
    ) -> None:
        user = StandProfiles.user(studio_config)
        body = _body(studio_config, profile="no-such-profile")

        with pytest.raises(AuthorizationError):
            await _calling(Probe(), studio_config).serve("probe", body, user)

    async def test_roles_without_grants_see_no_tool(
        self, studio_config: StudioRuntimeConfig
    ) -> None:
        user = StandProfiles.user(studio_config)
        user = user.model_copy(update={"metadata": {"roles": ["stranger"]}})

        with pytest.raises(HTTPException) as caught:
            await _calling(Probe(), studio_config).serve(
                "probe", _body(studio_config), user
            )

        if caught.value.status_code not in (403, 404):
            raise AssertionError(caught.value.status_code)

    async def test_unpersisted_user_is_unauthorized(
        self, studio_config: StudioRuntimeConfig
    ) -> None:
        """Без сохранённого входа субъект не собирается: зависимость поднимает 401."""
        auth = ApiAuth(
            StubAuthenticator(None),
            RequestTokens(StubAuthenticator.COOKIE),
            ChatProfiles(studio_config.profiles),
        )
        cookie = f"{StubAuthenticator.COOKIE}={StubAuthenticator.TOKEN}".encode()
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"cookie", cookie)],
        }

        with pytest.raises(AuthenticationError):
            await auth.subject_of_request(Request(scope), None)


class TestRoute:
    async def test_post_over_http(self, studio_config: StudioRuntimeConfig) -> None:
        """Роут FastAPI: тело валидируется, пользователь — из cookie-зависимости."""
        probe = Probe()
        user = StandProfiles.user(studio_config)

        app = FastAPI()
        ApiAuth(
            StubAuthenticator(user),
            RequestTokens(StubAuthenticator.COOKIE),
            ChatProfiles(studio_config.profiles),
        ).install(app)
        router = APIRouter()
        _calling(probe, studio_config).mount(router)
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://api",
            cookies=StubAuthenticator.cookies(),
        ) as client:
            response = await client.post(
                "/tools/probe", json=_body(studio_config).model_dump(mode="json")
            )
            malformed = await client.post("/tools/probe", json={"profile": "x"})

        if response.status_code != 200:
            raise AssertionError(response.text)
        if response.json()["ok"] is not True:
            raise AssertionError(response.json())
        if malformed.status_code != 422:
            raise AssertionError(malformed.text)
        if len(probe.seen) != 1:
            raise AssertionError("exactly one call reached the tool")


class TestAuthenticator:
    """JWT входа -> пользователь: строка users из таблицы, metadata из токена."""

    async def test_persisted_user_by_token(
        self, studio_config: StudioRuntimeConfig, pool: AsyncPostgresPool
    ) -> None:
        secret = studio_config.session.auth_secret
        users = UsersTable(
            studio_config.data_layer.postgres, studio_config.data_layer.db_schema, pool
        )
        await users.setup()
        tester = await users.ensure_user(
            SignedIn(
                identifier=f"tester-{uuid4().hex[:8]}",
                display_name="Tester",
                metadata={"roles": StandProfiles.roles(studio_config)},
            )
        )
        issuer = JwtTokens(secret, 60)
        authenticator = AuthService(
            tokens=issuer,
            cookie=CookieSpec(name="access_token", samesite="lax", ttl_sec=60),
            password=None,
            sso=None,
            users=users,
        )

        token = issuer.issue(
            SignedIn(
                identifier=tester.identifier,
                display_name="Tester",
                metadata=dict(tester.metadata),
            )
        )
        user = await authenticator.user_of_token(token)

        if user is None:
            raise AssertionError("persisted user expected")
        if user.id != tester.id:
            raise AssertionError((user.id, tester.id))
        if user.roles != frozenset(StandProfiles.roles(studio_config)):
            raise AssertionError(f"roles must come from the token: {user.roles}")

        with pytest.raises(AuthenticationError):
            await authenticator.user_of_token("not-a-token")

        # токен подтверждает роли, но личность не заводит: без строки users входа нет
        nobody = f"nobody-{uuid4().hex[:8]}"
        stranger = issuer.issue(
            SignedIn(identifier=nobody, display_name="", metadata={})
        )
        with pytest.raises(AuthenticationError):
            await authenticator.user_of_token(stranger)

        assert await users.get_user(nobody) is None
