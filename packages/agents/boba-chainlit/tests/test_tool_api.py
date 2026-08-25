"""REST-запуск инструмента человеком: тот же реестр, контекст под HumanInitiator.

Стенд: реестр из одного инструмента-зонда, записывающего контекст вызова,
плюс инструмент чата в chat_only; тред и пользователь — в тестовой базе.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from chainlit.auth import get_current_user
from chainlit.user import PersistedUser
from conftest import Seed
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from langchain_core.tools import tool

from boba.access import ProfileGrant, ToolAccess
from boba.chainlit.agent.toolrun.call_id import ToolCallIdField
from boba.chainlit.agent.toolrun.errors import ToolErrorGuard
from boba.chainlit.agent.toolrun.intent import ToolIntentField
from boba.chainlit.agent.toolrun.run_log import ToolRunLogger
from boba.chainlit.domain.config import RoleConfig
from boba.chainlit.domain.context import CallContext, HumanInitiator, ScopeKind
from boba.chainlit.domain.keys import ToolCallUrl
from boba.chainlit.infra.config import AppConfig, ChatProfiles
from boba.chainlit.infra.plugins import ToolRegistry, stream_source, tool_call_scope
from boba.chainlit.infra.tool_api import ToolCallBody, ToolCalling
from boba.toolkit.result import TextResult, pack_result

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


def _roles(app_config: AppConfig) -> list[str]:
    """Роли стенда: им видны профили из конфига."""
    return sorted(app_config.roles)


def _profiles(app_config: AppConfig) -> ChatProfiles:
    return ChatProfiles(app_config.profiles)


def _profile(app_config: AppConfig) -> str:
    """Первый профиль конфига, видимый ролям стенда."""
    visible = _profiles(app_config).visible_for(frozenset(_roles(app_config)))
    return next(iter(visible))


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
        ToolRunLogger.guard_all(tools, stream_source, tool_call_scope)
        ToolErrorGuard.guard_all(tools)
        return tools


def _registry(probe: Probe, app_config: AppConfig) -> ToolRegistry:
    tools = probe.tools()
    names: list[str] = []
    for tool_ in tools:
        names.append(tool_.name)

    roles: dict[str, RoleConfig] = {}
    for role in _roles(app_config):
        roles[role] = RoleConfig(tools=["*"])

    access = ToolAccess(
        tool_names=names,
        roles=roles,
        profiles={_profile(app_config): ProfileGrant(tools=["*"], roles=["*"])},
        chat_only=["canvas_open"],
    )
    return ToolRegistry(tools=tools, access=access)


def _calling(probe: Probe, seed: Seed, app_config: AppConfig) -> ToolCalling:
    async def registry() -> ToolRegistry:
        return _registry(probe, app_config)

    return ToolCalling(registry, _profiles(app_config), lambda: seed.layer)


def _tester(seed: Seed, app_config: AppConfig) -> PersistedUser:
    """Пользователь треда с ролями стенда: профиль ему виден."""
    user = seed.user
    user.metadata = {"roles": _roles(app_config)}
    return user


def _body(seed: Seed, app_config: AppConfig, **extra: Any) -> ToolCallBody:
    fields: dict[str, Any] = {
        "thread_id": seed.thread_id,
        "profile": _profile(app_config),
        "intent": "probe the context",
        "args": {"query": "x"},
    }
    fields.update(extra)
    return ToolCallBody.model_validate(fields)


class TestServe:
    async def test_tool_runs_under_human_api_context(
        self, seeded: Seed, app_config: AppConfig
    ) -> None:
        probe = Probe()
        user = _tester(seeded, app_config)

        reply = await _calling(probe, seeded, app_config).serve(
            "probe", _body(seeded, app_config), user
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
        if context.subject.profile != _profile(app_config):
            raise AssertionError(context.subject)
        if not context.subject.roles >= frozenset(_roles(app_config)):
            raise AssertionError(context.subject)
        if (
            context.scope.kind is not ScopeKind.CHAT
            or context.scope.id != seeded.thread_id
        ):
            raise AssertionError(context.scope)

        if CallContext.peek() is not None:
            raise AssertionError("the call context must not outlive the call")

    async def test_chat_only_tool_is_refused(
        self, seeded: Seed, app_config: AppConfig
    ) -> None:
        user = _tester(seeded, app_config)

        with pytest.raises(HTTPException) as caught:
            await _calling(Probe(), seeded, app_config).serve(
                "canvas_open", _body(seeded, app_config), user
            )

        if caught.value.status_code != 404:
            raise AssertionError(caught.value.status_code)

    async def test_foreign_thread_is_not_found(
        self, seeded: Seed, app_config: AppConfig
    ) -> None:
        user = _tester(seeded, app_config)
        body = _body(seeded, app_config, thread_id=str(uuid4()))

        with pytest.raises(HTTPException) as caught:
            await _calling(Probe(), seeded, app_config).serve("probe", body, user)

        if caught.value.status_code != 404:
            raise AssertionError(caught.value.status_code)

    async def test_unknown_profile_is_forbidden(
        self, seeded: Seed, app_config: AppConfig
    ) -> None:
        user = _tester(seeded, app_config)
        body = _body(seeded, app_config, profile="no-such-profile")

        with pytest.raises(HTTPException) as caught:
            await _calling(Probe(), seeded, app_config).serve("probe", body, user)

        if caught.value.status_code != 403:
            raise AssertionError(caught.value.status_code)

    async def test_roles_without_grants_see_no_tool(
        self, seeded: Seed, app_config: AppConfig
    ) -> None:
        user = _tester(seeded, app_config)
        user.metadata = {"roles": ["stranger"]}

        with pytest.raises(HTTPException) as caught:
            await _calling(Probe(), seeded, app_config).serve(
                "probe", _body(seeded, app_config), user
            )

        if caught.value.status_code not in (403, 404):
            raise AssertionError(caught.value.status_code)

    async def test_unpersisted_user_is_unauthorized(
        self, seeded: Seed, app_config: AppConfig
    ) -> None:
        with pytest.raises(HTTPException) as caught:
            await _calling(Probe(), seeded, app_config).serve(
                "probe", _body(seeded, app_config), None
            )

        if caught.value.status_code != 401:
            raise AssertionError(caught.value.status_code)


class TestRoute:
    async def test_post_over_http(self, seeded: Seed, app_config: AppConfig) -> None:
        """Роут FastAPI: тело валидируется, пользователь — из cookie-зависимости."""
        probe = Probe()
        user = _tester(seeded, app_config)

        app = FastAPI()
        app.add_api_route(
            ToolCallUrl.ROUTE,
            _calling(probe, seeded, app_config).serve,
            methods=["POST"],
        )
        app.dependency_overrides[get_current_user] = lambda: user

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://api"
        ) as client:
            response = await client.post(
                "/tools/probe", json=_body(seeded, app_config).model_dump(mode="json")
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
