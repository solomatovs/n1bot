"""Сервер сервиса над фейками стенда: общий для тестов сервиса и клиента.

Тесты сервиса поднимают его в своём процессе; тесты клиента чата запускают
этот файл субпроцессом в окружении сервиса: `python service_stand.py <port>
<workdir> [<срок токена, с>]` — клиент живёт на других версиях библиотек и
видит сервер только по сети. Endpoint'ы стенда: `/service` (все
инструменты, всем ролям), `/narrow` (один инструмент, только роли dev) и
`/offers` (все инструменты с правилами предложения соединений по ролям и
признаку клиента).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import uvicorn
from pydantic import BaseModel, ConfigDict, SecretStr

from boba.access import ProfileGrant, RoleConfig, ToolAccess, ToolPolicy
from boba.auth import SessionService, SignIns
from boba.auth.assembly import AuthServices
from boba.auth.config import (
    HeaderRolesConfig,
    LocalAuthConfig,
    LocalRoleProviders,
    LocalRolesConfig,
    ProxyAuthConfig,
    ProxyRoleProviders,
)
from boba.canvas.keys import WorkspaceMount
from boba.config import ConfigLoader, bind
from boba.connections.manifest import ConnectionTypes
from boba.identity.admission import RoleMappingConfig
from boba.identity.api import StoredUser, UserRows
from boba.identity.context import CallContexts
from boba.identity.lifetime import SessionLifetime
from boba.identity.principal import IdentityRule, UserIdentity
from boba.identity.run import Runs
from boba.krb.connections import KerberosCredentialSource
from boba.ldap import Ldap3Directory
from boba.mcp_server.auth import ServiceMark
from boba.mcp_server.catalog import EndpointCatalog, McpEndpoint
from boba.mcp_server.config.models import McpAppConfig, RunLimits
from boba.mcp_server.connections import NoRefresh
from boba.mcp_server.connections.params import ConnectionParams
from boba.mcp_server.connections.store import ConnectionStore
from boba.mcp_server.connections.tools import ConnectionTools
from boba.mcp_server.connections.user_connections import ArmedConnections
from boba.mcp_server.prompts import PromptFile
from boba.mcp_server.server import FileUploadTool, McpEndpoints, RunLimitMiddleware
from boba.oauth.server.clients import RegisteredClients, TrustedClient
from boba.oauth.server.provider import AuthServer
from boba.oauth.server.resources import ServiceAuth
from boba.oauth.server.sealed import SealedValues
from boba.oauth.server.tokens import ServiceTokens
from boba.stand import fake_caller, fake_connection, fake_toolmod
from boba.stand.connections import DisabledConnectionRepository
from boba.stand.roles import MemoryRoles
from boba.stand.service_signin import ServiceSignIn
from boba.stand.toolstand import ToolStand
from boba.storage.client import LocalStorageClient, LocalStorageConfig
from boba.storage.journal import DirVault, StreamJournal
from boba.tool_io.chain import CallAmbient
from boba.toolrun.callvalues import CallContextValues
from boba.toolrun.hosted import PluginSection
from boba.toolrun.injected import InjectedConfig, StaticConfig, StaticSection
from boba.toolrun.stream_calls import StreamGroupsConfig, WorkflowTool
from boba.toolrun.streams import CallJournals

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

ROLELESS_LOGIN = "ghost"
ROLELESS_PASSWORD = "ghost-pw"
"""Учётка входа local без единой роли: вход ей запрещён."""

TOKEN_TTL_SEC = 300
"""Срок токена доступа стенда, пока тест не назвал свой."""


class MemoryUserRows(UserRows):
    """Хранилище identity для OAuth стенда; production использует PostgreSQL."""

    def __init__(self) -> None:
        self._by_identity: dict[UserIdentity, StoredUser] = {}
        self._by_id: dict[UUID, StoredUser] = {}

    async def stored(self, identity: UserIdentity) -> StoredUser | None:
        return self._by_identity.get(identity)

    async def stored_by_id(self, user_id: UUID) -> StoredUser | None:
        return self._by_id.get(user_id)

    async def upsert(
        self,
        identity: UserIdentity,
        roles: frozenset[str] | None = None,
    ) -> StoredUser:
        current = self._by_identity.get(identity)
        stored = StoredUser(
            id=(
                current.id
                if current is not None
                else uuid5(NAMESPACE_URL, f"boba-user:{identity.identifier}")
            ),
            created_at=(
                current.created_at if current is not None else datetime.now(UTC)
            ),
            identity=identity,
            roles=(
                current.roles
                if roles is None and current is not None
                else roles or frozenset()
            ),
        )
        self._by_identity[identity] = stored
        self._by_id[stored.id] = stored
        return stored


class EndpointPrompts(BaseModel):
    """Промпты секции endpoint'а стенда: ключ prompts конфига сервиса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompts: dict[str, PromptFile]


class ServiceStand:
    """Сервер сервиса над фейками стенда: реестр собран цепочкой обвязок,
    как его собирает загрузчик. Права выдаются endpoint'ам: /service
    публикует полный список, /narrow — только fake_echo и file_upload.
    Вход — proxy сервиса входа: роли вошедшего называет доверенный клиент;
    dev_token(), weak_token() и analyst_token() входят так пользователями
    стенда alice, bob и dave. Вход формой — пользователь local. port — порт,
    на котором стенд слушает. Endpoint offers публикует имена из каталога
    только роли ANALYST."""

    PROMPT: ClassVar[str] = "sql_review"
    PROMPT_FILE: ClassVar[str] = (
        'description = "Review an SQL statement"\n'
        "\n"
        "[arguments.sql]\n"
        'description = "The statement"\n'
        "required = true\n"
        "\n"
        "[arguments.dialect]\n"
        'description = "The SQL dialect"\n'
        "required = false\n"
        "\n"
        "[[messages]]\n"
        'role = "user"\n'
        'text = "Review {sql} in {{dialect}}: {dialect}."\n'
        "\n"
        "[[messages]]\n"
        'role = "assistant"\n'
        'text = "Reviewing."\n'
    )
    """Промпт endpoint'а PROFILE: обязательный и необязательный аргументы,
    скобка текстом и две роли."""

    OFFERS: ClassVar[str] = "offers"
    STORED: ClassVar[str] = "stored"
    """Endpoint с именованными соединениями из каталога для роли analyst."""

    ANALYST: ClassVar[str] = "analyst"

    PLUGINS: ClassVar[tuple[str, ...]] = (
        "boba.stand.fake_toolmod:PLUGIN",
        "boba.stand.fake_caller:PLUGIN",
        "boba.stand.fake_connection:PLUGIN",
    )
    """Объявления фейковых модулей: тела идут их программой плагина."""

    STREAM: StreamGroupsConfig = StreamGroupsConfig(
        open_sec=20.0,
        stall_sec=10.0,
        poll_sec=0.1,
        pipe_bytes=65536,
        pipe_bytes_max=1 << 30,
    )

    def __init__(  # noqa: PLR0913, PLR0915 — сборка HTTP-стенда
        self,
        workdir: Path,
        limits: RunLimits,
        port: int,
        prefix: str,
        token_ttl_sec: int = TOKEN_TTL_SEC,
        connections: ConnectionStore | None = None,
        dynamic: bool = False,
        journaled: bool = True,
    ) -> None:
        self.port = port
        self.public_url = f"http://127.0.0.1:{port}{prefix}"
        contexts = CallContexts()
        runs = Runs(contexts)
        self._connections = connections
        self._credentials = KerberosCredentialSource(None, NoRefresh())
        repository = connections
        if repository is None:
            repository = DisabledConnectionRepository()

        resolver = ArmedConnections(repository, self._credentials, contexts)
        params = ConnectionParams(
            ConnectionTypes.discover(),
            contexts,
            StaticConfig(),
            resolver,
        )
        journal_store: StreamJournal | None = None
        if journaled:
            vault = DirVault(str(workdir / "journal"))
            journal_store = StreamJournal(vault, 0)

        journals = CallJournals(journal_store, runs)
        stand = ToolStand(
            self.STREAM,
            contexts,
            journals,
            CallAmbient(),
            (
                CallContextValues(contexts),
                params,
                InjectedConfig(StaticSection()),
            ),
        )
        exported = [
            fake_toolmod.fake_echo,
            fake_toolmod.fake_emit,
            fake_toolmod.fake_collect,
            fake_caller.fake_whoami,
            fake_caller.fake_scope,
            fake_caller.fake_progress,
            fake_caller.fake_sleep,
            fake_caller.fake_query,
            fake_connection.fake_connection_host,
        ]
        if dynamic:
            exported.append(fake_caller.fake_gate)

        tools = stand.launch(
            exported,
            stand.process_launcher("dag-service", workdir, 60.0, self.PLUGINS),
            PluginSection(values={"token": "t0ken", "limit": 5}),
        )

        own: set[str] = set()
        if connections is not None:
            catalog_tools = ConnectionTools(repository, contexts).build()
            stand.adopt(catalog_tools)
            tools.extend(catalog_tools)
            for entry in catalog_tools:
                own.add(entry.name)

        names: list[str] = []
        for tool in tools:
            names.append(tool.name)

        tools_for_all = list(names)
        tools_for_all.append(FileUploadTool.NAME)
        tools_for_all.append(WorkflowTool.NAME)
        roles = frozenset({"dev", "ADM", self.ANALYST, "weak"})
        all_tools = tuple(tools_for_all)
        endpoints = (
            self._endpoint(PROFILE, all_tools, roles, self._prompts(workdir)),
            self._endpoint(
                NARROW,
                ("fake_echo", FileUploadTool.NAME),
                frozenset({"dev"}),
                {},
            ),
            self._endpoint(self.OFFERS, all_tools, roles, {}),
            self._endpoint(self.STORED, all_tools, roles, {}),
        )
        if prefix:
            endpoints += (
                McpEndpoint(
                    id=uuid4(),
                    prefix_url="/",
                    title="root",
                    tools=all_tools,
                    roles=roles,
                ),
            )
        catalog = EndpointCatalog(endpoints)
        self.catalog = catalog
        role_access = {}
        for role in roles:
            role_access[role] = RoleConfig(tools=["*"])

        profile_access = {}
        for endpoint in endpoints:
            profile_access[endpoint.prefix_url] = ProfileGrant(tools=["*"], roles=["*"])

        access: ToolPolicy = ToolAccess(names, role_access, profile_access)
        if dynamic:
            access = catalog

        self.registry = replace(stand.registry(access, (params,)), own=frozenset(own))
        proxy = ProxyAuthConfig(
            identity=IdentityRule(),
            secret=SecretStr(PROXY_SECRET),
            roles=ProxyRoleProviders(header=HeaderRolesConfig()),
        )
        local_roles = LocalRolesConfig(
            mapping=RoleMappingConfig(root={LOCAL_LOGIN: ["dev"]})
        )
        local = LocalAuthConfig(
            identity=IdentityRule(),
            users={
                LOCAL_LOGIN: LOCAL_PASSWORD,
                ROLELESS_LOGIN: ROLELESS_PASSWORD,
            },
            roles=LocalRoleProviders(local=local_roles),
        )
        proxies = [proxy]
        auth_services = AuthServices(
            Ldap3Directory(),
            catalog,
            MemoryRoles({"dev", "ADM", "DEV", "read", "wrt", "weak", "analyst"}),
            "stand-session-secret",
        )
        sign_ins = McpAppConfig.AUTH_BACKENDS.build([local, *proxies], auth_services)
        # в сервисе точку workspace ставит загрузчик плагинов из профиля песочницы
        WorkspaceMount.configure("/workspace")
        public_url = self.public_url
        tokens = ServiceTokens(
            public_url,
            "stand-session-secret",
            SessionLifetime(ttl_sec=token_ttl_sec, max_sec=3600),
            "stand",
            ServiceMark.TOKEN_SALT,
        )
        self._sign_ins = sign_ins
        self._tokens = tokens
        self._proxies = proxies
        self._users = MemoryUserRows()
        self._sign_in = ServiceSignIn(
            public_url, CLIENT_ID, CLIENT_SECRET, PROXY_SECRET
        )
        storage = LocalStorageConfig(
            files_dir=str(workdir / "files"),
        )
        self.runs = runs
        self._journals = journals
        self._params = params
        self._limits = RunLimitMiddleware(limits, McpEndpoints.OPERATIONS)
        self._storage = storage
        self._files = LocalStorageClient(storage)
        self.endpoints = self.build(catalog)

    def build(self, snapshot: EndpointCatalog) -> McpEndpoints:
        auth = self._service_auth(
            self.public_url, snapshot, self._sign_ins, self._tokens, self._proxies
        )
        return McpEndpoints(
            self.registry,
            self.runs,
            self._journals,
            auth,
            snapshot.endpoints(),
            self._params,
            self._limits,
            self._storage,
            self._files,
        )

    def _service_auth(
        self,
        public_url: str,
        catalog: EndpointCatalog,
        sign_ins: SignIns,
        tokens: ServiceTokens,
        proxies: Sequence[ProxyAuthConfig],
    ) -> ServiceAuth:
        sealed = SealedValues("stand-session-secret", ServiceMark.SEALED_SALT)
        declared = TrustedClient(
            secret=SecretStr(CLIENT_SECRET),
        )
        resources = catalog.resources(public_url)
        server = AuthServer(
            public_url,
            sign_ins,
            tokens,
            proxies,
            RegisteredClients(sealed, {CLIENT_ID: declared}),
            sealed,
            list(resources.values()),
            SessionService(self._users, self.catalog),
        )
        return ServiceAuth(
            public_url,
            server,
            tokens,
            ServiceMark.NAME,
            resources,
            profiles=self.catalog,
        )

    @staticmethod
    def _endpoint(
        name: str,
        tools: tuple[str, ...],
        roles: frozenset[str],
        prompts: dict[str, PromptFile],
    ) -> McpEndpoint:
        return McpEndpoint(
            id=uuid4(),
            prefix_url=f"/{name}",
            title=name,
            tools=tools,
            roles=roles,
            prompts=prompts,
        )

    def _prompts(self, workdir: Path) -> dict[str, PromptFile]:
        """Промпты endpoint'а PROFILE тем же путём, что у сервиса: файл PROMPT
        в каталоге prompts/<endpoint> подключает конфиг ключом
        ${toml_dir:prompts/<endpoint>}."""
        folder = workdir / "prompts" / PROFILE
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{self.PROMPT}.toml").write_text(self.PROMPT_FILE, encoding="utf-8")
        config = workdir / "prompts.toml"
        config.write_text(
            f'[endpoint]\nprompts = "${{toml_dir:prompts/{PROFILE}}}"\n',
            encoding="utf-8",
        )

        return bind(ConfigLoader([config]).load(), "endpoint", EndpointPrompts).prompts

    def url(self, profile: str) -> str:
        return f"{self.public_url}/{profile}"

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

    async def analyst_token(self, profile: str) -> str:
        """Токен пользователя dave с ролью ANALYST для endpoint'а profile."""
        return await asyncio.to_thread(
            self._sign_in.token, "dave", self.ANALYST, self.url(profile)
        )

    @asynccontextmanager
    async def serving(self) -> AsyncGenerator[None, None]:
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


if __name__ == "__main__":
    ttl_sec = TOKEN_TTL_SEC
    if len(sys.argv) > 3:
        ttl_sec = int(sys.argv[3])

    served = ServiceStand(
        Path(sys.argv[2]),
        RunLimits(max_runs=4, max_waiting=4),
        int(sys.argv[1]),
        "",
        ttl_sec,
    )
    uvicorn.run(
        served.endpoints.app(), host="127.0.0.1", port=served.port, log_level="warning"
    )
