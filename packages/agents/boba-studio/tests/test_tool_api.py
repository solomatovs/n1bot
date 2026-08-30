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
from studio_stand import StandProfiles, StubAuthenticator

from boba.access import ProfileGrant, RoleConfig, ToolAccess
from boba.auth import JwtTokens
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import CallContext, HumanInitiator, ScopeKind
from boba.identity.locks import MemoryLiveLocks
from boba.identity.signin import SignedIn
from boba.runtime.config import StudioRuntimeConfig
from boba.runtime.plugins import CallSurface
from boba.runtime.users import UsersTable
from boba.studio.api.auth import ApiAuth, RequestTokens
from boba.studio.api.jwt_auth import JwtAuthenticator
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
        profiles={StandProfiles.profile(studio_config): ProfileGrant(tools=["*"], roles=["*"])},
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

        with pytest.raises(HTTPException) as caught:
            await _calling(Probe(), studio_config).serve(
                "probe", body, user
            )

        if caught.value.status_code != 403:
            raise AssertionError(caught.value.status_code)

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
        with pytest.raises(HTTPException) as caught:
            await _calling(Probe(), studio_config).serve(
                "probe", _body(studio_config), None
            )

        if caught.value.status_code != 401:
            raise AssertionError(caught.value.status_code)


class TestRoute:
    async def test_post_over_http(self, studio_config: StudioRuntimeConfig) -> None:
        """Роут FastAPI: тело валидируется, пользователь — из cookie-зависимости."""
        probe = Probe()
        user = StandProfiles.user(studio_config)

        app = FastAPI()
        ApiAuth(
            StubAuthenticator(user),
            RequestTokens(StubAuthenticator.COOKIE),
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
        authenticator = JwtAuthenticator(issuer, lambda: users)

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

        if await authenticator.user_of_token("not-a-token") is not None:
            raise AssertionError("garbage token must not authenticate")

        # токен другого приложения на той же основе: строка users заводится здесь
        stranger = issuer.issue(
            SignedIn(identifier=f"nobody-{uuid4().hex[:8]}", display_name="", metadata={})
        )
        created = await authenticator.user_of_token(stranger)
        if created is None:
            raise AssertionError("a token of a peer application must sign in")
        if not created.identifier.startswith("nobody-"):
            raise AssertionError(created.identifier)
