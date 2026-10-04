"""Сервер сервиса над фейками стенда: общий для тестов сервиса и клиента.

Тесты сервиса поднимают его в своём процессе; тесты клиента чата запускают
этот файл субпроцессом в окружении сервиса: `python service_stand.py <port>
<workdir>` — клиент живёт на других версиях библиотек и видит сервер только
по сети. Endpoint'ы стенда: `/mcp/service` (все инструменты, всем ролям) и
`/mcp/narrow` (один инструмент, только роли dev).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import uvicorn
from pydantic import SecretStr

from boba.access import ProfileGrant, RoleConfig, ToolAccess
from boba.auth import AuthService, JwtTokens
from boba.auth.config import HeaderRolesConfig, ProxyAuthConfig, ProxyRoleProviders
from boba.auth.profiles import ProfileProviders
from boba.auth.proxy import HmacProxySignIn
from boba.auth.roles import HeaderRoles, RoleProviders
from boba.connection_broker.sealed import SealedConnectionParams
from boba.connections.manifest import ConnectionTypes
from boba.connections.sealed import SealKeys
from boba.dag_service.auth import ProxySignInRoute, SessionTokenVerifier
from boba.dag_service.server import CallSchemas, DagEndpoints, RunLimits
from boba.identity.api import AuthenticatedUser
from boba.identity.context import CallContexts
from boba.identity.run import Runs
from boba.identity.session import Login
from boba.identity.signin import SignedIn
from boba.identity.token import CookieSpec, SessionRenewal
from boba.runtime.journal import DirVault, StreamJournal
from boba.stand import fake_connection
from boba.stand_core import fake_caller, fake_toolmod
from boba.stand_core.fake_toolmod import FakeConfig
from boba.toolkit.chain import CallAmbient
from boba.toolrun.bridge import ToolBridge
from boba.toolrun.callvalues import CallContextValues
from boba.toolrun.injected import InjectedConfig, StaticConfig
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller
from boba.toolrun.registry import ToolChain, ToolRegistry
from boba.toolrun.stream_calls import StreamGroupsConfig
from boba.toolrun.streams import CallJournals

PROFILE = "service"
DEV_TOKEN = "dev-token"
WEAK_TOKEN = "weak-token"

NARROW = "narrow"
"""Второй endpoint стенда: один инструмент и только роли dev."""

PROXY_SECRET = "stand-proxy-secret"
"""Ключ подписи proxy-входа: им доверенный клиент подписывает заголовки."""


class MemoryUsers:
    """Строки users стенда в памяти: вход заводит строку, как в приложении
    (протокол AuthUsers сервиса входа)."""

    def __init__(self) -> None:
        self._users: dict[str, AuthenticatedUser] = {}

    async def get_user(self, identifier: Login) -> AuthenticatedUser | None:
        return self._users.get(identifier)

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        user = AuthenticatedUser(
            id=uuid5(NAMESPACE_URL, f"stand:{signed.identifier}"),
            identifier=signed.identifier,
            sign_in=signed.sign_in,
        )
        self._users[signed.identifier] = user

        return user


class ServiceStand:
    """Сервер сервиса над фейками стенда: реестр собран цепочкой обвязок,
    как его собирает загрузчик; роли dev и ADM (роль пользователя стенда
    чата) видят всё, weak — один инструмент.
    Вход — готовые токены dev и weak либо вход proxy сервиса входа: роли
    вошедшего приходят заголовком доверенного клиента. port — порт, на
    котором стенд слушает."""

    STREAM: StreamGroupsConfig = StreamGroupsConfig(
        open_sec=20.0,
        stall_sec=10.0,
        poll_sec=0.1,
        pipe_bytes=65536,
        pipe_bytes_max=1 << 30,
    )

    def __init__(self, workdir: Path, limits: RunLimits, port: int) -> None:
        self.port = port
        self.schemas = CallSchemas()
        contexts = CallContexts()
        runs = Runs(contexts)
        ambient = CallAmbient()
        launcher = ProcessToolCaller(
            "dag-service",
            ProcessLauncherConfig(
                provider="process",
                workdir=str(workdir),
                timeout_sec=60.0,
                channel_limit_bytes=4_000_000,
                stderr_tail_bytes=8192,
                kill_grace_sec=0.5,
            ),
            contexts,
            ambient,
        )

        sealed = SealedConnectionParams(SealKeys(), ConnectionTypes.discover, contexts)

        tools: list[Any] = []
        for payload in (
            fake_toolmod.fake_echo,
            fake_toolmod.fake_emit,
            fake_toolmod.fake_collect,
            fake_caller.fake_whoami,
            fake_caller.fake_sleep,
            fake_connection.fake_connection_host,
        ):
            tools.append(ToolBridge.as_structured_tool(payload.model_copy()))

        names: list[str] = []
        for tool in tools:
            names.append(tool.name)

        journals = CallJournals(
            StreamJournal(DirVault(str(workdir / "journal")), 0), runs
        )
        journals.mark_streamable(names)

        chain = ToolChain(
            self.STREAM,
            journals,
            contexts,
            ambient,
            (
                CallContextValues(contexts),
                sealed,
                InjectedConfig(self._config_of, StaticConfig()),
            ),
            (),
        )
        specs = chain.launch(tools, launcher)

        access = ToolAccess(
            tool_names=names,
            roles={
                "dev": RoleConfig(tools=["*"]),
                "ADM": RoleConfig(tools=["*"]),
                "weak": RoleConfig(tools=["fake_echo"]),
            },
            profiles={
                PROFILE: ProfileGrant(tools=["*"], roles=["*"]),
                NARROW: ProfileGrant(tools=["fake_echo"], roles=["dev"]),
            },
        )
        chain.seal(tools, access, specs)

        self.registry = ToolRegistry(
            tools=tools,
            access=access,
            stream_config=self.STREAM,
            own=frozenset(),
            node_args=(sealed,),
            specs=specs,
            ambient=ambient,
        )
        proxy = ProxyAuthConfig(
            secret=SecretStr(PROXY_SECRET),
            roles=ProxyRoleProviders(header=HeaderRolesConfig()),
        )
        auth = AuthService(
            tokens=JwtTokens("stand-session-secret", 300, "stand"),
            cookie=CookieSpec(name="access_token", samesite="lax", ttl_sec=300),
            password=None,
            sso=None,
            proxy=HmacProxySignIn(
                proxy,
                RoleProviders([HeaderRoles(HeaderRolesConfig())], True),
                ProfileProviders([]),
            ),
            users=MemoryUsers(),
            renewal=SessionRenewal.of(300, 3600),
        )
        verifier = SessionTokenVerifier(
            auth,
            {
                DEV_TOKEN: self._claims("alice", "dev"),
                WEAK_TOKEN: self._claims("bob", "weak"),
            },
        )
        self.endpoints = DagEndpoints(
            self.registry,
            runs,
            journals,
            verifier,
            limits,
            "/mcp",
            [ProxySignInRoute(proxy, auth).route()],
        )

    def url(self, profile: str) -> str:
        return f"http://127.0.0.1:{self.port}/mcp/{profile}"

    @asynccontextmanager
    async def serving(self) -> AsyncIterator[None]:
        """Слушает порт стенда на время блока, в текущем цикле событий."""
        server = uvicorn.Server(
            uvicorn.Config(
                self.endpoints.app(),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
            )
        )
        task = asyncio.create_task(server.serve())
        try:
            while not server.started:
                if task.done():
                    await task

                await asyncio.sleep(0.02)

            yield
        finally:
            server.should_exit = True
            await task

    @staticmethod
    def _claims(login: str, role: str) -> dict[str, Any]:
        return {"client_id": "stand", "scopes": [], "login": login, "roles": [role]}

    @staticmethod
    def _config_of(name: str, annotation: object) -> object:
        return FakeConfig(token=SecretStr("t0ken"), limit=5)


if __name__ == "__main__":
    served = ServiceStand(
        Path(sys.argv[2]), RunLimits(max_runs=4, max_waiting=4), int(sys.argv[1])
    )
    uvicorn.run(
        served.endpoints.app(), host="127.0.0.1", port=served.port, log_level="warning"
    )
