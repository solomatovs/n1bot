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
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import uvicorn
from pydantic import BaseModel, ConfigDict, SecretStr

from boba.access import RoleConfig, ToolAccess
from boba.auth.config import (
    HeaderRolesConfig,
    LocalAuthConfig,
    LocalRoleProviders,
    LocalRolesConfig,
    ProxyAuthConfig,
    ProxyRoleProviders,
)
from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.auth.proxy import ProxyAssertionVerifier
from boba.canvas.keys import WorkspaceMount
from boba.config import ConfigLoader, bind
from boba.connection_broker.params import ConnectionParams
from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.tools import ConnectionTools
from boba.connection_broker.user_connections import ArmedConnections
from boba.connections.manifest import ConnectionTypes
from boba.connections.offers import OfferedSource
from boba.identity.admission import RoleMappingConfig
from boba.identity.context import CallContexts
from boba.identity.principal import IdentityRule
from boba.identity.run import Runs
from boba.ldap import Ldap3Directory
from boba.mcp_server.auth import ServiceMark
from boba.mcp_server.prompts import PromptFile
from boba.mcp_server.server import (
    ConnectionOfferRule,
    EndpointCatalog,
    McpEndpoint,
    McpEndpoints,
    RunLimits,
)
from boba.oauth.server import (
    AuthServer,
    RegisteredClients,
    SealedValues,
    ServiceAuth,
    ServiceTokens,
    TrustedClient,
)
from boba.runtime.journal import DirVault, StreamJournal
from boba.runtime.signin import SignInAssembly
from boba.runtime.storage import LocalStorageClient, LocalStorageConfig
from boba.stand import fake_caller, fake_connection, fake_toolmod
from boba.stand.service_signin import ServiceSignIn
from boba.stand.toolstand import ToolStand
from boba.tool_io.chain import CallAmbient
from boba.toolrun.callvalues import CallContextValues
from boba.toolrun.hosted import PluginSection
from boba.toolrun.injected import InjectedConfig, StaticConfig, StaticSection
from boba.toolrun.stream_calls import StreamGroupsConfig
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


class EndpointPrompts(BaseModel):
    """Промпты секции endpoint'а стенда: ключ prompts конфига сервиса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompts: dict[str, PromptFile]


class ServiceStand:
    """Сервер сервиса над фейками стенда: реестр собран цепочкой обвязок,
    как его собирает загрузчик; роли dev и ADM (роль пользователя стенда
    чата) видят всё, weak — один инструмент.
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

    STORAGE: ClassVar[ConnectionOfferRule] = ConnectionOfferRule(
        roles=["*"], offer=[OfferedSource.STORED]
    )
    """Правило по умолчанию: имена соединений из каталога MCP."""

    STREAM: StreamGroupsConfig = StreamGroupsConfig(
        open_sec=20.0,
        stall_sec=10.0,
        poll_sec=0.1,
        pipe_bytes=65536,
        pipe_bytes_max=1 << 30,
    )

    def __init__(  # noqa: PLR0913 — конфиг реального HTTP-стенда
        self,
        workdir: Path,
        limits: RunLimits,
        port: int,
        prefix: str,
        token_ttl_sec: int = TOKEN_TTL_SEC,
        connections: ConnectionStore | None = None,
    ) -> None:
        self.port = port
        self.public_url = f"http://127.0.0.1:{port}{prefix}"
        contexts = CallContexts()
        runs = Runs(contexts)
        self._connections = connections
        self._credentials = KerberosCredentialSource(None, NoRefresh())
        resolver = None
        if connections is not None:
            resolver = ArmedConnections(self._store, self._credential_source, contexts)
        params = ConnectionParams(
            ConnectionTypes.discover,
            contexts,
            StaticConfig(),
            stored=resolver,
        )
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
                params,
                InjectedConfig(StaticSection()),
            ),
        )
        tools = stand.launch(
            (
                fake_toolmod.fake_echo,
                fake_toolmod.fake_emit,
                fake_toolmod.fake_collect,
                fake_caller.fake_whoami,
                fake_caller.fake_scope,
                fake_caller.fake_progress,
                fake_caller.fake_sleep,
                fake_caller.fake_query,
                fake_connection.fake_connection_host,
            ),
            stand.process_launcher("dag-service", workdir, 60.0, self.PLUGINS),
            PluginSection(values={"token": "t0ken", "limit": 5}),
        )

        own: set[str] = set()
        if connections is not None:
            catalog_tools = ConnectionTools(self._store, contexts).build()
            stand.adopt(catalog_tools)
            tools.extend(catalog_tools)
            for entry in catalog_tools:
                own.add(entry.name)

        names: list[str] = []
        for tool in tools:
            names.append(tool.name)

        both = ConnectionOfferRule(roles=[self.ANALYST], offer=[OfferedSource.STORED])
        granted = {
            PROFILE: McpEndpoint(
                tools=["*"],
                roles=["*"],
                connections=[self.STORAGE],
                prompts=self._prompts(workdir),
            ),
            NARROW: McpEndpoint(
                tools=["fake_echo"],
                roles=["dev"],
                connections=[self.STORAGE],
                prompts={},
            ),
            self.OFFERS: McpEndpoint(
                tools=["*"],
                roles=["*"],
                connections=[both],
                prompts={},
            ),
        }
        if connections is not None:
            stored_rule = ConnectionOfferRule(roles=["*"], offer=[OfferedSource.STORED])
            granted[self.STORED] = McpEndpoint(
                tools=["*"], roles=["*"], connections=[stored_rule], prompts={}
            )
        catalog = EndpointCatalog(granted)
        grants = catalog.grants(
            {
                "dev": RoleConfig(tools=["*"]),
                "ADM": RoleConfig(tools=["*"]),
                self.ANALYST: RoleConfig(tools=["*"]),
                "weak": RoleConfig(tools=["fake_echo"]),
            }
        )
        access = ToolAccess(names, grants.roles, grants.profiles, grants.check)
        self.registry = replace(stand.registry(access, (params,)), own=frozenset(own))
        proxy = ProxyAuthConfig(
            identity=IdentityRule(realm="corporate"),
            secret=SecretStr(PROXY_SECRET),
            roles=ProxyRoleProviders(header=HeaderRolesConfig()),
        )
        local_roles = LocalRolesConfig(
            mapping=RoleMappingConfig(root={LOCAL_LOGIN: ["dev"]})
        )
        local = LocalAuthConfig(
            identity=IdentityRule(realm="local-stand"),
            users={
                LOCAL_LOGIN: LOCAL_PASSWORD,
                ROLELESS_LOGIN: ROLELESS_PASSWORD,
            },
            roles=LocalRoleProviders(local=local_roles),
        )
        proxies = [proxy]
        for realm in ("local-stand", "aid-local", "restricted"):
            proxies.append(
                ProxyAuthConfig(
                    identity=IdentityRule(realm=realm),
                    path=f"/auth/proxy/{realm}",
                    secret=proxy.secret,
                    roles=proxy.roles,
                )
            )

        sign_ins = SignInAssembly(Ldap3Directory(), catalog).build(
            [local, *proxies], "stand-session-secret"
        )
        # в сервисе точку workspace ставит загрузчик плагинов из профиля песочницы
        WorkspaceMount.configure("/workspace")
        public_url = self.public_url
        tokens = ServiceTokens(
            public_url,
            "stand-session-secret",
            token_ttl_sec,
            3600,
            "stand",
            ServiceMark.TOKEN_SALT,
        )
        sealed = SealedValues("stand-session-secret", ServiceMark.SEALED_SALT)
        declared = TrustedClient(
            secret=SecretStr(CLIENT_SECRET),
            realms=frozenset({"corporate", "local-stand", "aid-local"}),
        )
        resources: dict[str, str] = {}
        for name in granted:
            resources[name] = f"{public_url}/{name}"
        server = AuthServer(
            public_url,
            sign_ins,
            tokens,
            ProxyAssertionVerifier(proxies),
            RegisteredClients(sealed, {CLIENT_ID: declared}),
            sealed,
            list(resources.values()),
        )
        auth = ServiceAuth(
            public_url,
            server,
            tokens,
            ServiceMark.NAME,
            resources,
        )
        self._sign_in = ServiceSignIn(
            public_url, CLIENT_ID, CLIENT_SECRET, PROXY_SECRET
        )
        storage = LocalStorageConfig(
            files_dir=str(workdir / "files"),
        )
        self.endpoints = McpEndpoints(
            self.registry,
            runs,
            journals,
            auth,
            granted,
            params,
            limits,
            storage,
            LocalStorageClient(storage),
        )

    def _store(self) -> ConnectionStore:
        if self._connections is None:
            raise RuntimeError("the HTTP stand has no connection store")
        return self._connections

    def _credential_source(self) -> KerberosCredentialSource:
        return self._credentials

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
