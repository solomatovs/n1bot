"""Агент чата в режиме службы: [agent.process] называет адрес службы агента
(`boba serve` на стенде в цикле теста) и вход proxy; вкладка входит в
службу от имени своего пользователя, профиль агента выбирается настройкой
сессии `model` по имени профиля чата, ход идёт сессией службы, закрытие
вкладки закрывает её. Удаление треда удаляет сессию через службу от имени
пользователя треда, сохранённого со слоем чата; профиль, которого служба не
предлагает, — отказ с понятной причиной."""

from __future__ import annotations

from collections.abc import AsyncIterator
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from acp_stand import AgentHistoryStand, AgentStand, FakeModel, RecordingUpdates
from aid_stand import NoConnectionStore, RelayStand, TabSession
from pydantic import SecretStr

from boba.aid.agent.chat import (
    AgentLauncher,
    AgentSessionMeta,
    Question,
    ThreadAgent,
    ThreadAgentsIndex,
    ThreadDeleter,
    ThreadIdentity,
)
from boba.aid.agent.client import AgentProcessError
from boba.aid.agent.config import AgentConfig, AgentServiceConfig
from boba.aid.agent.settings import AgentSettingsTable
from boba.aid.domain.fields import ThreadMetaField
from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.chat.profiles import ChatProfileConfig, SelectedProfile
from boba.cli.serve import ServeHistory
from boba.connections.secrets import SecretCipher
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.db.postgres.connection import PostgresConfig
from boba.identity.context import CallContexts
from boba.mcp_client.client import (
    HttpLocation,
    HttpRoute,
    McpServers,
    McpServersConfig,
    ProxyAuth,
)
from boba.runtime.config import ProcessConfig
from boba.runtime.threads import ThreadsTable
from boba.stand.agent_service import (
    AgentServiceStand,
    ServeStandConfig,
    ServiceProfiles,
)
from boba.stand.names import StandNames
from boba.stand.signin import SignInStand
from boba.stand.site import Stand
from boba.stand.ui.fake_llm import ScenarioName
from boba.transport.http import HttpTransportConfig

pytestmark = pytest.mark.anyio

SCHEMA = StandNames().of_tree("agent_service_mode")
CIPHER_KEY = b"0123456789abcdef0123456789abcdef"
NO_SERVERS = McpServersConfig(servers={})
MODEL = "fake-model"
FOREIGN_PROFILE = "absent"


@pytest.fixture(autouse=True)
def chainlit_context(auth_token: str) -> None:
    """Контекст chainlit этим тестам не нужен: они живут ниже обработчиков.
    Секрет токенов chainlit ставит auth_token: вход вкладки подписывает им
    токен, и без него тест зависел бы от порядка прогона."""


@pytest.fixture
async def table(
    test_postgres: PostgresConfig, pool: AsyncPostgresPool
) -> AgentSettingsTable:
    """Слой чата настроек агента в схеме набора."""
    async with pool.connection() as conn:
        await PostgresSchema(SCHEMA).apply_files(
            conn, files("boba.aid.agent") / "schema", {}
        )

    return AgentSettingsTable(test_postgres, SCHEMA, SecretCipher(CIPHER_KEY), pool)


@pytest.fixture
async def threads(
    pool: AsyncPostgresPool, test_postgres: PostgresConfig
) -> ThreadsTable:
    """Таблица тредов в схеме набора: в threads.meta агент вкладки помнит сессию."""
    async with pool.connection() as conn:
        await PostgresSchema(SCHEMA).apply_files(
            conn, files("boba.runtime") / "schema", {}
        )

    return ThreadsTable(test_postgres, SCHEMA)


@pytest.fixture
async def service(
    stand: Stand, model: FakeModel, process_config: ProcessConfig, tmp_path: Path
) -> AsyncIterator[AgentServiceStand]:
    """Служба агента с историей в Postgres стенда (роль с паролем) и
    профилем агента, названным как профиль чата стенда."""
    history = AgentHistoryStand(stand)
    layer: dict[str, Any] = {
        "permissions": {"defaultMode": "bypassPermissions", "allowBypass": True},
        "sessions": {"maxOpen": 4},
    }
    profiles = ServiceProfiles(model.base_url()).add(
        SignInStand.PROFILE, ["dev"], MODEL
    )
    served = AgentServiceStand(
        process_config.krb,
        ServeHistory(schema=history.schema(), postgres=history.postgres()),
        layer,
        profiles,
        600.0,
    )
    await served.start()
    try:
        yield served
    finally:
        await served.stop()


class ServiceModeStand:
    """Запуск агента чата в режиме службы и вкладка пользователя alice с
    ролью dev."""

    LOGIN = "alice"

    def __init__(
        self,
        stand: Stand,
        service: AgentServiceStand,
        model: FakeModel,
        table: AgentSettingsTable,
        tmp_path: Path,
    ) -> None:
        history = AgentHistoryStand(stand)
        location = HttpLocation(
            scheme="http", host="127.0.0.1", port=service.port, path=""
        )
        mode = AgentServiceConfig(
            endpoint=HttpRoute(service=location, path=ServeStandConfig.PATH),
            auth=ProxyAuth(
                issuer=location,
                client_id=ServeStandConfig.CLIENT_ID,
                client_secret=SecretStr(ServeStandConfig.CLIENT_SECRET),
                secret=SecretStr(ServeStandConfig.PROXY_SECRET),
            ),
            start_timeout_sec=60.0,
            request_timeout_sec=60.0,
        )
        base = AgentStand(tmp_path / "agents", history.history()).config()
        self.config: AgentConfig = base.model_copy(update={"process": mode})
        credentials = KerberosCredentialSource(None, NoRefresh())
        self._relays = RelayStand(
            NoConnectionStore(), lambda: credentials, CallContexts()
        )
        self._servers = McpServers(NO_SERVERS)
        self.launcher = AgentLauncher(
            self.config,
            NO_SERVERS,
            table,
            self._relays.registrations,
            self._relays.relay(self._servers),
            "0.0",
        )
        self._model = model

    def identity(self, profile: str) -> ThreadIdentity:
        tab = TabSession()
        tab.password_sign_in(frozenset({"dev"}))

        return ThreadIdentity(
            thread_id=str(uuid4()),
            user_id=uuid4(),
            login=self.LOGIN,
            roles=frozenset({"dev"}),
            session=tab.live,
            profile=SelectedProfile(name=profile, config=self.profile()),
        )

    def profile(self) -> ChatProfileConfig:
        provider = {
            "kind": "openai",
            "connection": {
                "scheme": "http",
                "host": "127.0.0.1",
                "port": self._model.port,
                "path": "/v1",
            },
            "transport": HttpTransportConfig().model_dump(mode="json"),
        }
        base = SignInStand.profile().model_dump(mode="json")

        return ChatProfileConfig.model_validate(
            {**base, "provider": provider, "model": MODEL}
        )

    async def stop(self) -> None:
        await self._servers.stop()


@pytest.fixture
async def mode(
    stand: Stand,
    service: AgentServiceStand,
    model: FakeModel,
    table: AgentSettingsTable,
    tmp_path: Path,
) -> AsyncIterator[ServiceModeStand]:
    made = ServiceModeStand(stand, service, model, table, tmp_path)
    try:
        yield made
    finally:
        await made.stop()


class TestServiceMode:
    async def test_turn_goes_through_the_agent_service(
        self,
        mode: ServiceModeStand,
        service: AgentServiceStand,
        threads: ThreadsTable,
        table: AgentSettingsTable,
    ) -> None:
        identity = mode.identity(SignInStand.PROFILE)
        updates = RecordingUpdates()
        agent = ThreadAgent(identity, mode.launcher, AgentSessionMeta(threads), updates)
        try:
            await agent.start()
            await agent.ask(Question(key="first", text=ScenarioName.ANSWER.value))
            assert updates.finished, updates.updates
            session_id = agent.session_id
            assert session_id is not None
        finally:
            await agent.close()

        await service.sessions_drop_to(0)

        deleter = ThreadDeleter(mode.launcher, table, ThreadAgentsIndex())
        meta = {ThreadMetaField.AGENT_SESSION: session_id}
        await deleter.thread_deleted(identity.thread_id, meta)

        await service.sessions_drop_to(0)

    async def test_profile_the_service_does_not_offer_is_refused(
        self, mode: ServiceModeStand, threads: ThreadsTable
    ) -> None:
        identity = mode.identity(FOREIGN_PROFILE)
        agent = ThreadAgent(
            identity, mode.launcher, AgentSessionMeta(threads), RecordingUpdates()
        )
        try:
            await agent.start()
            with pytest.raises(AgentProcessError, match="is not among them"):
                await agent.ask(Question(key="first", text=ScenarioName.ANSWER.value))
        finally:
            await agent.close()
