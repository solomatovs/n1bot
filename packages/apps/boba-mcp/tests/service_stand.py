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

import uvicorn
from pydantic import SecretStr

from boba.access import ProfileGrant, RoleConfig, ToolAccess
from boba.auth import SignIns
from boba.auth.config import (
    HeaderRolesConfig,
    LocalAuthConfig,
    LocalRoleProviders,
    LocalRolesConfig,
    ProxyAuthConfig,
    ProxyRoleProviders,
)
from boba.auth.profiles import ProfileProviders, RoleProfiles
from boba.auth.proxy import HmacProxySignIn, ProxyAssertions
from boba.auth.roles import HeaderRoles, LocalRoles, RoleProviders
from boba.auth.signin import CompositeSignIn, LocalSignIn
from boba.canvas.keys import WorkspaceMount
from boba.connection_broker.sealed import SealedConnectionParams
from boba.connections.manifest import ConnectionTypes
from boba.connections.sealed import SealKeys
from boba.identity.admission import RoleMappingConfig
from boba.identity.context import CallContexts
from boba.identity.run import Runs
from boba.mcp_server.auth import (
    AuthServer,
    McpClient,
    RegisteredClients,
    SealedValues,
    ServiceAuth,
    ServiceTokens,
)
from boba.mcp_server.server import (
    EndpointCatalog,
    McpEndpoints,
    RunLimits,
)
from boba.runtime.journal import DirVault, StreamJournal
from boba.runtime.storage import LocalStorageConfig
from boba.stand import fake_connection
from boba.stand.service_signin import ServiceSignIn
from boba.stand.toolstand import ToolStand
from boba.stand_core import fake_caller, fake_toolmod
from boba.stand_core.fake_toolmod import FakeConfig
from boba.toolkit.chain import CallAmbient
from boba.toolrun.callvalues import CallContextValues
from boba.toolrun.injected import InjectedConfig, StaticConfig
from boba.toolrun.stream_calls import StreamGroupsConfig
from boba.toolrun.streams import CallJournals
from boba.workspace.binaries import TrustedBinaries
from boba.workspace.launcher import MountingConfig

PROFILE = "service"

NARROW = "narrow"
"""Второй endpoint стенда: один инструмент и только роли dev."""

PROXY_SECRET = "stand-proxy-secret"
"""Ключ утверждений proxy-входа: им доверенный клиент подписывает утверждение
о пользователе."""

CLIENT_ID = "stand-chat"
CLIENT_SECRET = "stand-client-secret"
"""Доверенный клиент OAuth стенда: ему разрешён обмен утверждения на токен."""

LOCAL_LOGIN = "carol"
LOCAL_PASSWORD = "carol-pw"
"""Пользователь входа local стенда: роль dev по таблице конфига."""


class ServiceStand:
    """Сервер сервиса над фейками стенда: реестр собран цепочкой обвязок,
    как его собирает загрузчик; роли dev и ADM (роль пользователя стенда
    чата) видят всё, weak — один инструмент.
    Вход — proxy сервиса входа: роли вошедшего называет доверенный клиент;
    dev_token() и weak_token() входят так пользователями стенда alice и
    bob. Вход формой — пользователь local. port — порт, на котором стенд
    слушает."""

    STREAM: StreamGroupsConfig = StreamGroupsConfig(
        open_sec=20.0,
        stall_sec=10.0,
        poll_sec=0.1,
        pipe_bytes=65536,
        pipe_bytes_max=1 << 30,
    )

    def __init__(self, workdir: Path, limits: RunLimits, port: int) -> None:
        self.port = port
        contexts = CallContexts()
        runs = Runs(contexts)
        sealed = SealedConnectionParams(SealKeys(), ConnectionTypes.discover, contexts)
        journals = CallJournals(
            StreamJournal(DirVault(str(workdir / "journal")), 0), runs
        )
        stand = ToolStand(
            self.STREAM,
            contexts,
            journals,
            CallAmbient(),
            (
                CallContextValues(contexts),
                sealed,
                InjectedConfig(self._config_of, StaticConfig()),
            ),
        )
        tools = stand.launch(
            (
                fake_toolmod.fake_echo,
                fake_toolmod.fake_emit,
                fake_toolmod.fake_collect,
                fake_caller.fake_whoami,
                fake_caller.fake_scope,
                fake_caller.fake_sleep,
                fake_connection.fake_connection_host,
            ),
            stand.process_launcher("dag-service", workdir, 60.0),
        )

        names: list[str] = []
        for tool in tools:
            names.append(tool.name)

        granted = {
            PROFILE: ProfileGrant(tools=["*"], roles=["*"]),
            NARROW: ProfileGrant(tools=["fake_echo"], roles=["dev"]),
        }
        access = ToolAccess(
            tool_names=names,
            roles={
                "dev": RoleConfig(tools=["*"]),
                "ADM": RoleConfig(tools=["*"]),
                "weak": RoleConfig(tools=["fake_echo"]),
            },
            profiles=granted,
        )
        self.registry = stand.registry(access, (sealed,))
        proxy = ProxyAuthConfig(
            secret=SecretStr(PROXY_SECRET),
            roles=ProxyRoleProviders(header=HeaderRolesConfig()),
        )
        catalog = EndpointCatalog(granted)
        by_roles = ProfileProviders([RoleProfiles(catalog)])
        local_roles = LocalRolesConfig(
            mapping=RoleMappingConfig(root={LOCAL_LOGIN: ["dev"]})
        )
        local = LocalAuthConfig(
            users={LOCAL_LOGIN: LOCAL_PASSWORD},
            roles=LocalRoleProviders(local=local_roles),
        )
        password = CompositeSignIn(
            [
                LocalSignIn(
                    local, RoleProviders([LocalRoles(local_roles)], True), by_roles
                )
            ]
        )
        sign_ins = SignIns(
            password,
            None,
            HmacProxySignIn(
                proxy, RoleProviders([HeaderRoles(HeaderRolesConfig())], True), by_roles
            ),
        )
        # в сервисе точку workspace ставит загрузчик плагинов из профиля песочницы
        WorkspaceMount.configure("/workspace")
        public_url = f"http://127.0.0.1:{port}"
        tokens = ServiceTokens(public_url, "stand-session-secret", 300, 3600, "stand")
        sealed = SealedValues("stand-session-secret")
        declared = McpClient(secret=SecretStr(CLIENT_SECRET)).registered(CLIENT_ID)
        server = AuthServer(
            public_url,
            sign_ins,
            tokens,
            ProxyAssertions(PROXY_SECRET, 60),
            RegisteredClients(sealed, {CLIENT_ID: declared}),
            sealed,
            [f"{public_url}/mcp/{PROFILE}", f"{public_url}/mcp/{NARROW}"],
        )
        auth = ServiceAuth(
            public_url,
            server,
            tokens,
            {
                PROFILE: f"{public_url}/mcp/{PROFILE}",
                NARROW: f"{public_url}/mcp/{NARROW}",
            },
        )
        self._sign_in = ServiceSignIn(
            public_url, CLIENT_ID, CLIENT_SECRET, PROXY_SECRET
        )
        self.endpoints = McpEndpoints(
            self.registry,
            runs,
            journals,
            auth,
            limits,
            "/mcp",
            LocalStorageConfig(
                files_dir=str(workdir / "files"),
                mounting=MountingConfig(
                    mount_wait_sec=1.0,
                    mount_poll_sec=0.1,
                    shutdown_wait_sec=1.0,
                    lock_wait_sec=1.0,
                    copy_chunk_bytes=65536,
                ),
                mount_dir=str(workdir),
                binaries=TrustedBinaries(dirs=("/usr/bin", "/bin")),
            ),
        )

    def url(self, profile: str) -> str:
        return f"http://127.0.0.1:{self.port}/mcp/{profile}"

    async def dev_token(self, profile: str) -> str:
        """Токен пользователя alice с ролью dev для endpoint'а profile."""
        return await asyncio.to_thread(
            self._sign_in.token, "alice", "dev", self.url(profile)
        )

    async def weak_token(self, profile: str) -> str:
        """Токен пользователя bob с ролью weak для endpoint'а profile."""
        return await asyncio.to_thread(
            self._sign_in.token, "bob", "weak", self.url(profile)
        )

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
    def _config_of(name: str, annotation: object) -> object:
        return FakeConfig(token=SecretStr("t0ken"), limit=5)


if __name__ == "__main__":
    served = ServiceStand(
        Path(sys.argv[2]), RunLimits(max_runs=4, max_waiting=4), int(sys.argv[1])
    )
    uvicorn.run(
        served.endpoints.app(), host="127.0.0.1", port=served.port, log_level="warning"
    )
