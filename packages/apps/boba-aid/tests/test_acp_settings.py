"""Настройки агента: профиль модели из профиля чата, файл настроек ключами
договора запуска в приватном каталоге 0700/0600 без серверов MCP, хуков,
режима сессии и области вызовов, строка подключения истории, рукопожатие
настоящего `boba --stdio` v2 по этому файлу — секреты не попадают в argv и
окружение ребёнка, каталог удаляется с процессом, — и удаление сессии треда
коротким процессом по текущему профилю."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from importlib.resources import files
from ipaddress import ip_address
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID, uuid4

import mcp_types as mt
import pytest
from acp.exceptions import RequestError
from acp.experimental.v2 import schema
from acp_stand import (
    API_KEY,
    AgentHistoryStand,
    AgentStand,
    FakeModel,
    ProcFiles,
    RecordingUpdates,
    TlsFiles,
)
from aid_stand import NoConnectionStore, RelayStand, TabSession
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from foreign_agent import ForeignAgent

from boba.aid.agent.chat import (
    AgentLauncher,
    AgentSessionMeta,
    Question,
    ThreadAgent,
    ThreadAgentsIndex,
    ThreadDeleter,
    ThreadIdentity,
)
from boba.aid.agent.client import (
    AcpClientError,
    AgentClient,
    AgentCommand,
    AgentFailureKind,
    AgentProcess,
    AgentProcessError,
)
from boba.aid.agent.config import (
    PostgresAgentHistory,
)
from boba.aid.agent.settings import (
    AgentSettingsError,
    AgentWorkdirs,
    HistoryEnvironment,
    ProfileModelSpecs,
    SettingsFileBuilder,
)
from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.chat.profiles import (
    ChatProfileConfig,
    ChatProfiles,
    ReasoningReturn,
    SelectedProfile,
)
from boba.core.mcp_names import McpToolNames
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.db.postgres.connection import (
    LibpqConnection,
    PostgresAuthError,
    PostgresConfig,
)
from boba.db.postgres.connection.auth import CertificateAuth
from boba.identity.context import CallContexts
from boba.kerberos import KeytabAuth
from boba.krb.credentials import KerberosEnv
from boba.llm.onnx import OnnxProvider
from boba.mcp_client.client import McpServerConfig, McpServers, McpServersConfig
from boba.runtime.threads import ThreadsTable
from boba.runtime.users import UsersTable
from boba.stand.names import StandNames
from boba.stand.signin import SignInStand
from boba.stand.site import Stand
from boba.stand.ui.fake_llm import FailureName, ScenarioName
from boba.transport.acp.protocol import (
    AcpErrorCode,
    McpMessage,
    McpVersion,
    StopReason,
)
from boba.transport.http import HttpTransportConfig

pytestmark = pytest.mark.anyio

SCHEMA = StandNames().of_tree("acp_settings")
NO_SERVERS = McpServersConfig(servers={})


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Контекст chainlit этим тестам не нужен: они живут ниже обработчиков."""


@pytest.fixture
async def users(test_postgres: PostgresConfig, pool: AsyncPostgresPool) -> UsersTable:
    """Строки users набора: запуск агента читает их только при удалении
    треда в режиме службы."""
    return UsersTable(test_postgres, SCHEMA, pool)


@pytest.fixture
def specs() -> ProfileModelSpecs:
    return ProfileModelSpecs()


class TestProfileModel:
    """Профиль модели агента из профиля чата: только удалённый провайдер."""

    def test_local_provider_is_refused(self, specs: ProfileModelSpecs) -> None:
        profile = ChatProfileConfig.model_validate(
            {
                "display_name": "Local",
                "description": "onnx profile",
                "roles": ["*"],
                "tools": [],
                "provider": OnnxProvider(kind="onnx", model_dir="/nonexistent"),
                "model": "local",
                "limits": SignInStand.limits(),
            }
        )
        with pytest.raises(AgentSettingsError, match="remote provider"):
            specs.model("local", profile)


class TestHistoryDsn:
    """Строка подключения истории агента из подключения процесса [postgres]."""

    def test_password_auth_renders_a_libpq_dsn(self, stand: Stand) -> None:
        postgres = AgentHistoryStand(stand).postgres()
        dsn = LibpqConnection(postgres).dsn().get_secret_value()
        assert f"user={stand.pg_probe_user}" in dsn
        assert f"password={stand.pg_probe_password.get_secret_value()}" in dsn
        assert f"dbname={stand.pg_probe_database}" in dsn
        assert "gssencmode=disable" in dsn

    async def test_certificate_auth_is_refused(
        self, test_postgres: PostgresConfig
    ) -> None:
        certificate = test_postgres.model_copy(
            update={
                "auth": CertificateAuth(
                    method="certificate", user="boba", sslcert="/c.pem", sslkey="/k.pem"
                )
            }
        )
        with pytest.raises(PostgresAuthError, match="cannot be handed to another"):
            LibpqConnection(certificate).auth()

    async def test_kerberos_role_renders_a_dsn_without_password_and_libkrb5_env(
        self, test_postgres: PostgresConfig
    ) -> None:
        auth = test_postgres.auth
        if not isinstance(auth, KeytabAuth):
            got = type(auth).__name__
            pytest.fail(
                f"the stand postgres must be entered with a keytab: expected "
                f"KeytabAuth in [postgres].auth of the stand config, got {got}"
            )

        history = PostgresAgentHistory.model_validate(
            {"kind": "postgres", "schema": "agent", "postgres": test_postgres}
        )
        dsn = LibpqConnection(test_postgres).dsn().get_secret_value()
        assert f"user={auth.principal.partition('@')[0]}" in dsn
        assert "password" not in dsn
        assert f"dbname={test_postgres.dbname}" in dsn
        assert "gssencmode=require" in dsn

        environment = await HistoryEnvironment(history).prepare()
        assert set(environment) == {KerberosEnv.CCACHE, KerberosEnv.CONFIG}
        assert Path(environment[KerberosEnv.CONFIG]).is_file()
        ccache = environment[KerberosEnv.CCACHE]
        assert Path(ccache.partition(":")[2]).is_file(), ccache

    async def test_password_role_needs_no_environment(self, stand: Stand) -> None:
        history = AgentHistoryStand(stand)
        environment = HistoryEnvironment(history.history())
        assert await environment.prepare() == {}


class TestSettingsFile:
    """Файл настроек в приватном каталоге: форма агента, права, хук."""

    def test_file_has_the_agent_form_and_private_modes(
        self, tmp_path: Path, stand: Stand
    ) -> None:
        model = FakeModel()
        history_stand = AgentHistoryStand(stand)
        history = history_stand.history()
        config = AgentStand(tmp_path / "agents", history).config(
            "http://127.0.0.1:8000/boba"
        )
        builder = SettingsFileBuilder(config, NO_SERVERS)
        launch = model.launch("Answer briefly")

        document = builder.build(launch.profile, launch.model, None, {})
        workdir = AgentWorkdirs(tmp_path / "agents").create("thread-1")
        workdir.write(document, launch.system_prompt)

        assert stat.S_IMODE(workdir.path.stat().st_mode) == 0o700
        assert stat.S_IMODE(workdir.settings_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(workdir.prompt_path.stat().st_mode) == 0o600
        assert workdir.prompt_path.read_text(encoding="utf-8") == "Answer briefly"

        body = json.loads(workdir.settings_path.read_text(encoding="utf-8"))
        assert body["model"] == launch.profile
        provider = body["models"][launch.profile]["provider"]
        assert provider["kind"] == "openai"
        assert provider["baseUrl"] == model.base_url()
        assert provider["auth"] == {"method": "bearer", "token": API_KEY}
        assert body["models"][launch.profile]["contextWindow"] == 32000
        assert body["permissions"] == {"allowBypass": True}
        assert body["history"]["kind"] == "postgres"
        assert body["history"]["schema"] == history.db_schema
        dsn = LibpqConnection(history_stand.postgres()).dsn().get_secret_value()
        assert body["history"]["dsn"] == dsn
        assert body["systemPromptFile"] == "system_prompt.txt"
        assert body["mcp"] is None
        assert "mcpServers" not in body
        assert "hooks" not in body

        workdir.remove()
        assert not workdir.exists()


class TestAgentHandshake:
    """Настоящий `boba --stdio` v2 принимает файл и держит секреты только в нём."""

    async def test_agent_initializes_and_leaks_nothing(
        self, tmp_path: Path, stand: Stand, model: FakeModel
    ) -> None:
        history = AgentHistoryStand(stand)
        agent_stand = AgentStand(tmp_path / "agents", history.history())
        launch = model.launch()
        builder = SettingsFileBuilder(agent_stand.config(), NO_SERVERS)
        document = builder.build(launch.profile, launch.model, None, {})
        workdir = AgentWorkdirs(tmp_path / "agents").create("thread-2")
        workdir.write(document, launch.system_prompt)

        client = AgentClient(RecordingUpdates())
        credentials = KerberosCredentialSource(None, NoRefresh())
        relays = RelayStand(NoConnectionStore(), lambda: credentials, CallContexts())
        servers = McpServers(McpServersConfig(servers={}))
        relay = relays.relay(servers)
        process = AgentProcess(agent_stand.command(), workdir, client, relay, (), "0.0")
        try:
            response = await process.start()
            assert response.protocol_version == 2
            assert response.info.name == "boba"
            assert response.capabilities is not None
            assert response.capabilities.session is not None
            assert response.capabilities.session.mcp is not None
            assert response.capabilities.session.mcp.acp is not None

            files = ProcFiles(process.pid)
            cmdline = files.cmdline()
            assert str(workdir.settings_path) in cmdline
            joined = " ".join(cmdline)
            assert API_KEY not in joined
            assert stand.pg_probe_password.get_secret_value() not in joined

            environ = files.environ()
            values = " ".join(environ.values())
            assert API_KEY not in values
            assert stand.pg_probe_password.get_secret_value() not in values
            assert set(environ) <= {"HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER"}
            assert "BOBA_CONFIG_PATH" not in environ
        finally:
            await process.close()
            await servers.stop()

        assert not workdir.exists()
        assert not process.alive()
        assert not os.path.exists(f"/proc/{process.pid}/cmdline") or _is_zombie(
            process.pid
        )


class TestRetiredServers:
    """Замена вышедшего процесса агента вкладки и закрытие вкладки снимают
    serverId прежнего процесса: ретранслятор отвечает на них UNKNOWN_SERVER."""

    ENVELOPE: ClassVar[dict[str, Any]] = {
        mt.PROTOCOL_VERSION_META_KEY: McpVersion.V2026_07_28.value,
        mt.CLIENT_CAPABILITIES_META_KEY: {},
    }

    async def test_replacement_and_close_revoke_the_server_ids(  # noqa: PLR0913 — фикстуры теста
        self,
        tmp_path: Path,
        stand: Stand,
        users: UsersTable,
        threads: ThreadsTable,
        model: FakeModel,
        auth_token: str,
        standard: McpServerConfig,
    ) -> None:
        credentials = KerberosCredentialSource(None, NoRefresh())
        relays = RelayStand(NoConnectionStore(), lambda: credentials, CallContexts())
        servers_config = McpServersConfig(servers={"standard": standard})
        servers = McpServers(servers_config)
        relay = relays.relay(servers)
        history = AgentHistoryStand(stand)
        agent_stand = AgentStand(tmp_path / "agents", history.history())
        launcher = AgentLauncher(
            agent_stand.config(),
            servers_config,
            SignInStand.profiles(),
            users,
            relays.registrations,
            relay,
            "0.0",
            agent_stand.agents_dir(),
        )
        tab = TabSession()
        tab.password_sign_in(frozenset({"dev"}))
        config = self._profile(model)
        identity = ThreadIdentity(
            thread_id=str(uuid4()),
            user_id=uuid4(),
            login=TabSession.LOGIN,
            roles=frozenset({"dev"}),
            session=tab.live,
            profile=SelectedProfile(name=SignInStand.PROFILE, config=config),
        )
        meta = AgentSessionMeta(threads)
        updates = RecordingUpdates()
        agent = ThreadAgent(identity, launcher, meta, updates, relays.menus(servers))
        try:
            await agent.start()
            first = agent.process._servers[0].server_id
            relays.registrations.of(first)

            # вышедший процесс заменяется новым перед следующим ходом
            process = agent.process
            assert isinstance(process, AgentProcess)
            os.kill(process.pid, signal.SIGKILL)
            await process.exited()
            await agent.ask(Question(key="draft", text=ScenarioName.ANSWER.value))
            assert updates.finished, updates.updates
            second = agent.process._servers[0].server_id
            assert second != first
            relays.registrations.of(second)
            with pytest.raises(RequestError):
                relays.registrations.of(first)

            after_restart = McpMessage(
                server_id=first,
                request_id="mcp:after-restart",
                method="tools/list",
                params={"_meta": self.ENVELOPE},
            )
            with pytest.raises(RequestError) as restarted:
                await relay.answer(after_restart, asyncio.Queue[McpMessage]().put)

            await agent.close()
            with pytest.raises(RequestError):
                relays.registrations.of(second)

            after_close = McpMessage(
                server_id=second,
                request_id="mcp:after-close",
                method="tools/list",
                params={"_meta": self.ENVELOPE},
            )
            with pytest.raises(RequestError) as closed:
                await relay.answer(after_close, asyncio.Queue[McpMessage]().put)
        finally:
            await agent.close()
            await servers.stop()

        assert restarted.value.code == AcpErrorCode.UNKNOWN_SERVER
        assert closed.value.code == AcpErrorCode.UNKNOWN_SERVER

    @staticmethod
    def _profile(model: FakeModel) -> ChatProfileConfig:
        """Профиль стенда с сервером standard и провайдером фейковой модели."""
        provider = {
            "kind": "openai",
            "connection": {
                "scheme": "http",
                "host": "127.0.0.1",
                "port": model.port,
                "path": "/v1",
                "auth": {"method": "bearer", "token": API_KEY},
            },
            "transport": HttpTransportConfig().model_dump(mode="json"),
        }
        base = SignInStand.profile().model_dump(mode="json")

        return ChatProfileConfig.model_validate(
            {**base, "provider": provider, "model": "fake-model", "mcp": ["standard"]}
        )


class TestForkCut:
    """Правка вопроса ведётся веткой session/fork, только если агент объявил
    срез ветки по messageId вопроса; чужому агенту без объявления правка
    отказывает понятным сообщением, а не ведёт ветку наугад."""

    FOREIGN: ClassVar[Path] = Path(__file__).with_name("foreign_agent.py")

    async def test_edit_is_refused_by_an_agent_without_the_cut(  # noqa: PLR0913 — фикстуры теста
        self,
        tmp_path: Path,
        stand: Stand,
        users: UsersTable,
        test_postgres: PostgresConfig,
        pool: AsyncPostgresPool,
        auth_token: str,
    ) -> None:
        credentials = KerberosCredentialSource(None, NoRefresh())
        relays = RelayStand(NoConnectionStore(), lambda: credentials, CallContexts())
        servers = McpServers(McpServersConfig(servers={}))
        relay = relays.relay(servers)
        history = AgentHistoryStand(stand)
        agent_stand = AgentStand(tmp_path / "agents", history.history())
        config = agent_stand.config()
        foreign = config.process.model_copy(
            update={"command": sys.executable, "args": [str(self.FOREIGN)]}
        )
        launcher = AgentLauncher(
            config.model_copy(update={"process": foreign}),
            NO_SERVERS,
            SignInStand.profiles(),
            users,
            relays.registrations,
            relay,
            "0.0",
            agent_stand.agents_dir(),
        )
        tab = TabSession()
        tab.password_sign_in(frozenset({"dev"}))
        identity = ThreadIdentity(
            thread_id=str(uuid4()),
            user_id=uuid4(),
            login=TabSession.LOGIN,
            roles=frozenset({"dev"}),
            session=tab.live,
            profile=SelectedProfile(
                name=SignInStand.PROFILE, config=SignInStand.profile()
            ),
        )
        runtime_schema = files("boba.runtime") / "schema"
        async with pool.connection() as conn:
            await PostgresSchema(SCHEMA).apply_files(conn, runtime_schema, {})

        threads = ThreadsTable(test_postgres, SCHEMA)
        updates = RecordingUpdates()
        agent = ThreadAgent(
            identity,
            launcher,
            AgentSessionMeta(threads),
            updates,
            relays.menus(servers),
        )
        try:
            await agent.start()
            assert not agent.process.forks_by_message()

            await agent.ask(Question(key="draft", text="first"))
            assert updates.asked, updates.updates

            with pytest.raises(AcpClientError) as refused:
                await agent.ask(
                    Question(key=updates.asked[0], text="edited", edited=True)
                )
        finally:
            await agent.close()
            await servers.stop()

        assert "does not declare 'boba/upToMessageId'" in str(refused.value)


class ChatLaunch:
    """Чат теста ниже обработчиков chainlit: треды в Postgres стенда,
    запуск агента AgentLauncher по профилю чата и [agent], агент вкладки
    ThreadAgent, ретранслятор стенда. Файл настроек строится по серверам
    filed, ретранслятор ходит к серверам relayed: так видно, чьим сроком
    обрывается вызов."""

    def __init__(
        self,
        root: Path,
        stand: Stand,
        users: UsersTable,
        threads: ThreadsTable,
    ) -> None:
        self._root = root
        self._history = AgentHistoryStand(stand)
        self._users = users
        self._threads = threads
        credentials = KerberosCredentialSource(None, NoRefresh())
        self._relays = RelayStand(
            NoConnectionStore(), lambda: credentials, CallContexts()
        )
        self._servers: list[McpServers] = []
        self._agents: list[ThreadAgent] = []

    def agent(
        self,
        profile: ChatProfileConfig,
        filed: McpServersConfig,
        relayed: McpServersConfig,
        updates: RecordingUpdates,
    ) -> ThreadAgent:
        servers = McpServers(relayed)
        self._servers.append(servers)
        agent_stand = AgentStand(self._root / "agents", self._history.history())
        launcher = AgentLauncher(
            agent_stand.config(),
            filed,
            SignInStand.profiles(),
            self._users,
            self._relays.registrations,
            self._relays.relay(servers),
            "0.0",
            agent_stand.agents_dir(),
        )
        tab = TabSession()
        tab.password_sign_in(frozenset({"dev"}))
        identity = ThreadIdentity(
            thread_id=str(uuid4()),
            user_id=uuid4(),
            login=TabSession.LOGIN,
            roles=frozenset({"dev"}),
            session=tab.live,
            profile=SelectedProfile(name=SignInStand.PROFILE, config=profile),
        )
        agent = ThreadAgent(
            identity,
            launcher,
            AgentSessionMeta(self._threads),
            updates,
            self._relays.menus(servers),
        )
        self._agents.append(agent)

        return agent

    async def close(self) -> None:
        for agent in self._agents:
            await agent.close()

        for servers in self._servers:
            await servers.stop()


@pytest.fixture
async def threads(
    pool: AsyncPostgresPool, test_postgres: PostgresConfig
) -> ThreadsTable:
    """Таблица тредов в схеме теста: в threads.meta агент вкладки помнит сессию."""
    runtime_schema = files("boba.runtime") / "schema"
    async with pool.connection() as conn:
        await PostgresSchema(SCHEMA).apply_files(conn, runtime_schema, {})

    return ThreadsTable(test_postgres, SCHEMA)


@pytest.fixture
async def chat_launch(
    tmp_path: Path,
    stand: Stand,
    users: UsersTable,
    threads: ThreadsTable,
    auth_token: str,
) -> AsyncIterator[ChatLaunch]:
    launch = ChatLaunch(tmp_path, stand, users, threads)
    try:
        yield launch
    finally:
        await launch.close()


@pytest.fixture
async def tls_model(tmp_path: Path) -> AsyncIterator[FakeModel]:
    """Фейковая модель по https с самоподписанным сертификатом 127.0.0.1."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path = tmp_path / "llm.key"
    cert_path = tmp_path / "llm.crt"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))

    fake = FakeModel(TlsFiles(key=key_path, certificate=cert_path))
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


class TestLaunchConfig:
    """Конфиг запуска — единственный канал настроек чата для агента: профиль
    чата и [agent] доходят до настоящего `boba --stdio` файлом настроек и
    действуют на его запросы к модели и серверам."""

    NO_SERVERS: ClassVar[McpServersConfig] = McpServersConfig(servers={})

    def _profile(
        self,
        connection: dict[str, Any],
        transport: HttpTransportConfig,
        mcp: tuple[str, ...] = (),
    ) -> ChatProfileConfig:
        provider = {
            "kind": "openai",
            "connection": connection,
            "transport": transport.model_dump(mode="json"),
        }
        base = SignInStand.profile().model_dump(mode="json")

        return ChatProfileConfig.model_validate(
            {**base, "provider": provider, "model": "fake-model", "mcp": list(mcp)}
        )

    def _connection(self, model: FakeModel, **fields: Any) -> dict[str, Any]:
        """Секция connection провайдера профиля: адрес фейковой модели."""
        return {
            "scheme": "http",
            "host": "127.0.0.1",
            "port": model.port,
            "path": "/v1",
            "auth": {"method": "bearer", "token": API_KEY},
            **fields,
        }

    def _failure(self, updates: RecordingUpdates) -> str:
        """Причина отказа хода: пометка агента об ошибке перед idle отказом —
        слова человеку и короткая причина."""
        assert updates.finished, updates.updates
        _, idle = updates.finished[-1]
        assert idle.stop_reason == StopReason.REFUSAL, idle
        errors: list[str] = []
        for _, update in updates.updates:
            if not isinstance(update, schema.SessionNotice):
                continue

            if update.severity == "error":
                errors.append(f"{update.title} ({update.description})")

        assert errors, updates.updates

        return errors[-1]

    async def test_request_timeout_and_retries_of_the_provider_act(
        self, chat_launch: ChatLaunch, model: FakeModel
    ) -> None:
        connection = self._connection(
            model, timeout_sec=0.5, retry_attempts=2, retry_backoff_sec=0.0
        )
        profile = self._profile(connection, HttpTransportConfig())
        updates = RecordingUpdates()
        agent = chat_launch.agent(profile, self.NO_SERVERS, self.NO_SERVERS, updates)
        await agent.start()

        await agent.ask(Question(key="draft", text=FailureName.HANG.value))

        failure = self._failure(updates)
        assert failure.endswith("(no answer in time)"), failure
        requests = len(model.app.requests)
        assert requests % 2 == 0, requests
        assert requests >= 2, requests

    async def test_tls_check_follows_ssl_verify(
        self, chat_launch: ChatLaunch, tls_model: FakeModel
    ) -> None:
        text = ScenarioName.ANSWER.value
        trusting = self._connection(tls_model, scheme="https", ssl_verify=False)
        accepted = RecordingUpdates()
        agent = chat_launch.agent(
            self._profile(trusting, HttpTransportConfig()),
            self.NO_SERVERS,
            self.NO_SERVERS,
            accepted,
        )
        await agent.start()
        await agent.ask(Question(key="trusting", text=text))

        _, idle = accepted.finished[-1]
        assert idle.stop_reason == "end_turn", idle
        assert "plain streamed answer" in "".join(accepted.texts())

        checking = self._connection(
            tls_model, scheme="https", ssl_verify=True, retry_attempts=1
        )
        refused = RecordingUpdates()
        agent = chat_launch.agent(
            self._profile(checking, HttpTransportConfig()),
            self.NO_SERVERS,
            self.NO_SERVERS,
            refused,
        )
        await agent.start()
        await agent.ask(Question(key="checking", text=text))

        failure = self._failure(refused)
        assert failure.endswith("(the certificate check failed)"), failure

    async def test_transport_section_of_the_provider_acts(
        self, chat_launch: ChatLaunch, model: FakeModel, tmp_path: Path
    ) -> None:
        dumps = tmp_path / "dump"
        transport = HttpTransportConfig.model_validate(
            {"dump": {"enable": True, "path": str(dumps)}}
        )
        updates = RecordingUpdates()
        agent = chat_launch.agent(
            self._profile(self._connection(model), transport),
            self.NO_SERVERS,
            self.NO_SERVERS,
            updates,
        )
        await agent.start()

        await agent.ask(Question(key="draft", text=ScenarioName.ANSWER.value))

        _, idle = updates.finished[-1]
        assert idle.stop_reason == "end_turn", idle
        written = list(dumps.rglob("*"))
        assert any(path.is_file() for path in written), written

    @pytest.mark.parametrize(
        ("policy", "returned"),
        [(ReasoningReturn.NEVER, False), (ReasoningReturn.TURN, True)],
    )
    async def test_reasoning_goes_back_within_the_turn_by_the_model_config(
        self,
        chat_launch: ChatLaunch,
        model: FakeModel,
        policy: ReasoningReturn,
        returned: bool,
    ) -> None:
        spec = {"name": "no_such_tool", "arguments": {}}
        updates = RecordingUpdates()
        profile = self._profile(self._connection(model), HttpTransportConfig())
        limits = profile.limits.model_copy(update={"reasoning_return": policy})
        agent = chat_launch.agent(
            profile.model_copy(update={"limits": limits}),
            self.NO_SERVERS,
            self.NO_SERVERS,
            updates,
        )
        await agent.start()

        await agent.ask(
            Question(key="draft", text=f"{ScenarioName.CALL.value} {json.dumps(spec)}")
        )

        assert len(model.app.requests) == 2, model.app.requests
        assistant: list[dict[str, Any]] = []
        for message in model.app.requests[1]["messages"]:
            if message["role"] == "assistant":
                assistant.append(message)

        assert len(assistant) == 1, assistant
        reasoning = assistant[0].get("reasoning_content")
        assert (reasoning == "I will call no_such_tool") is returned, assistant

    async def test_tool_call_is_cut_by_the_deadline_of_the_profile_servers(
        self, chat_launch: ChatLaunch, model: FakeModel, standard: McpServerConfig
    ) -> None:
        limits = standard.limits.model_copy(update={"call_timeout_sec": 1.0})
        brief = standard.model_copy(update={"limits": limits})
        filed = McpServersConfig(servers={"standard": brief})
        relayed = McpServersConfig(servers={"standard": standard})
        name = McpToolNames().full("standard", "sleep_for")
        spec = {"name": name, "arguments": {"seconds": 5}}
        updates = RecordingUpdates()
        agent = chat_launch.agent(
            self._profile(
                self._connection(model), HttpTransportConfig(), ("standard",)
            ),
            filed,
            relayed,
            updates,
        )
        await agent.start()

        await agent.ask(
            Question(key="draft", text=f"{ScenarioName.CALL.value} {json.dumps(spec)}")
        )

        texts: list[str] = []
        for _, update in updates.updates:
            if not isinstance(update, schema.SessionToolCallUpdate):
                continue

            for item in update.content or ():
                if isinstance(item, schema.ContentToolCallContent):
                    block = item.content
                    if isinstance(block, schema.TextContentBlock):
                        texts.append(block.text)

        joined = " ".join(texts)
        assert "within 1.0 s" in joined, joined


class TestFailedLaunch:
    """Несостоявшийся запуск агента не оставляет файла настроек с секретами,
    а агент, который молчит на метод сессии, обрывается сроком из конфига."""

    async def test_missing_program_leaves_no_private_dir(
        self,
        tmp_path: Path,
        stand: Stand,
        users: UsersTable,
    ) -> None:
        history = AgentHistoryStand(stand)
        agent_stand = AgentStand(tmp_path / "agents", history.history())
        config = agent_stand.config()
        missing = config.process.model_copy(
            update={"command": str(tmp_path / "no-such-agent")}
        )
        credentials = KerberosCredentialSource(None, NoRefresh())
        relays = RelayStand(NoConnectionStore(), lambda: credentials, CallContexts())
        servers = McpServers(NO_SERVERS)
        launcher = AgentLauncher(
            config.model_copy(update={"process": missing}),
            NO_SERVERS,
            SignInStand.profiles(),
            users,
            relays.registrations,
            relays.relay(servers),
            "0.0",
            agent_stand.agents_dir(),
        )
        tab = TabSession()
        tab.password_sign_in(frozenset({"dev"}))
        identity = ThreadIdentity(
            thread_id=str(uuid4()),
            user_id=uuid4(),
            login=TabSession.LOGIN,
            roles=frozenset({"dev"}),
            session=tab.live,
            profile=SelectedProfile(
                name=SignInStand.PROFILE, config=SignInStand.profile()
            ),
        )
        launch = launcher.prepare(identity)
        try:
            with pytest.raises(AgentProcessError) as refused:
                await launcher.launch(identity, launch, AgentClient(RecordingUpdates()))
        finally:
            await servers.stop()

        assert "did not start" in str(refused.value)
        assert "[agent.process].command" in str(refused.value)
        failure = refused.value.failure
        assert failure.kind is AgentFailureKind.START_FAILED, failure
        assert failure.peer == "agent process 'no-such-agent'", failure
        assert str(tmp_path) not in failure.cause, failure
        assert list((tmp_path / "agents").iterdir()) == []

    async def test_silent_session_request_is_cut_by_request_timeout(
        self, tmp_path: Path, stand: Stand
    ) -> None:
        foreign = ForeignProcessStand(tmp_path, stand, ForeignAgentFlag.SILENT)
        process = await foreign.started()
        try:
            with pytest.raises(AgentProcessError) as cut:
                await process.open_session()
        finally:
            await foreign.close()

        assert "session/new" in str(cut.value)
        assert "within 1.0s" in str(cut.value)
        assert "request_timeout_sec" in str(cut.value)


class TestLatePrompt:
    """Ответ на session/prompt приходит, когда агент вставил вопрос своим
    ходом, — у вопроса во время хода после идущего хода; срок
    request_timeout_sec к нему не относится, ожидание обрывает только выход
    процесса."""

    async def test_prompt_answered_after_the_request_timeout_is_taken(
        self, tmp_path: Path, stand: Stand
    ) -> None:
        foreign = ForeignProcessStand(tmp_path, stand, ForeignAgentFlag.NONE)
        process = await foreign.started()
        try:
            opened = await process.open_session()
            text = schema.TextContentBlock(text=ForeignAgent.LATE)
            message_id = await process.prompt(opened.session_id, [text])
        finally:
            await foreign.close()

        assert ForeignAgent.LATE_SEC > foreign.REQUEST_TIMEOUT_SEC
        assert UUID(message_id)

    async def test_agent_exiting_before_the_answer_is_a_lost_connection(
        self, tmp_path: Path, stand: Stand
    ) -> None:
        foreign = ForeignProcessStand(tmp_path, stand, ForeignAgentFlag.NONE)
        process = await foreign.started()
        late_exit = f"{ForeignAgent.LATE} {ForeignAgent.EXIT}"
        try:
            opened = await process.open_session()
            text = schema.TextContentBlock(text=late_exit)
            with pytest.raises(AgentProcessError) as lost:
                await process.prompt(opened.session_id, [text])
        finally:
            await foreign.close()

        assert lost.value.failure.kind is AgentFailureKind.CONNECTION_LOST
        assert "session/prompt" in str(lost.value)
        assert "request_timeout_sec" not in str(lost.value)


class ForeignAgentFlag(StrEnum):
    """Ключи запуска чужого агента foreign_agent.py."""

    NONE = ""
    SILENT = ForeignAgent.SILENT


class ForeignProcessStand:
    """Процесс чужого агента foreign_agent.py вкладки без серверов чата: чат
    ждёт ответа на метод сессии REQUEST_TIMEOUT_SEC."""

    REQUEST_TIMEOUT_SEC: ClassVar[float] = 1.0

    def __init__(self, tmp_path: Path, stand: Stand, flag: ForeignAgentFlag) -> None:
        history = AgentHistoryStand(stand)
        agent_stand = AgentStand(tmp_path / "agents", history.history())
        args = [str(TestForkCut.FOREIGN)]
        if flag is not ForeignAgentFlag.NONE:
            args.append(flag.value)

        command = AgentCommand(
            executable=sys.executable,
            args=tuple(args),
            start_timeout_sec=30.0,
            request_timeout_sec=self.REQUEST_TIMEOUT_SEC,
            stop_timeout_sec=5.0,
        )
        builder = SettingsFileBuilder(agent_stand.config(), NO_SERVERS)
        launch = FakeModel().launch()
        document = builder.build(launch.profile, launch.model, None, {})
        workdir = AgentWorkdirs(tmp_path / "agents").create("thread-foreign")
        workdir.write(document, launch.system_prompt)
        credentials = KerberosCredentialSource(None, NoRefresh())
        relays = RelayStand(NoConnectionStore(), lambda: credentials, CallContexts())
        self._servers = McpServers(NO_SERVERS)
        self._process = AgentProcess(
            command,
            workdir,
            AgentClient(RecordingUpdates()),
            relays.relay(self._servers),
            (),
            "0.0",
        )

    async def started(self) -> AgentProcess:
        await self._process.start()

        return self._process

    async def close(self) -> None:
        await self._process.close()
        await self._servers.stop()


class DeletionStand:
    """Тред своего процесса агента и его удаление ниже обработчиков chainlit:
    вкладка открывает сессию под профилем чата, удаление идёт запуском с
    другим, текущим набором [profiles]."""

    def __init__(
        self, root: Path, stand: Stand, users: UsersTable, threads: ThreadsTable
    ) -> None:
        self.history = AgentHistoryStand(stand)
        self._agent = AgentStand(root / "agents", self.history.history())
        self._config = self._agent.config()
        self._users = users
        self._threads = threads
        credentials = KerberosCredentialSource(None, NoRefresh())
        self._relays = RelayStand(
            NoConnectionStore(), lambda: credentials, CallContexts()
        )
        self._servers = McpServers(NO_SERVERS)

    def profile(self, model: FakeModel) -> ChatProfileConfig:
        provider = {
            "kind": "openai",
            "connection": {
                "scheme": "http",
                "host": "127.0.0.1",
                "port": model.port,
                "path": "/v1",
            },
            "transport": HttpTransportConfig().model_dump(mode="json"),
        }
        base = SignInStand.profile().model_dump(mode="json")

        return ChatProfileConfig.model_validate(
            {**base, "provider": provider, "model": "fake-model"}
        )

    def launcher(self, profiles: ChatProfiles) -> AgentLauncher:
        return AgentLauncher(
            self._config,
            NO_SERVERS,
            profiles,
            self._users,
            self._relays.registrations,
            self._relays.relay(self._servers),
            "0.0",
            self._agent.agents_dir(),
        )

    async def opened(self, profile: ChatProfileConfig) -> tuple[str, str]:
        """Тред с одним ходом под профилем profile: id треда и сессии агента."""
        launcher = self.launcher(ChatProfiles({SignInStand.PROFILE: profile}))
        tab = TabSession()
        tab.password_sign_in(frozenset({"dev"}))
        identity = ThreadIdentity(
            thread_id=str(uuid4()),
            user_id=uuid4(),
            login=TabSession.LOGIN,
            roles=frozenset({"dev"}),
            session=tab.live,
            profile=SelectedProfile(name=SignInStand.PROFILE, config=profile),
        )
        updates = RecordingUpdates()
        agent = ThreadAgent(
            identity,
            launcher,
            AgentSessionMeta(self._threads),
            updates,
            self._relays.menus(self._servers),
        )
        try:
            await agent.start()
            await agent.ask(Question(key="first", text=ScenarioName.ANSWER.value))
            session_id = agent.session_id
        finally:
            await agent.close()

        assert updates.finished, updates.updates
        assert session_id is not None

        return identity.thread_id, session_id

    async def delete(self, thread_id: str, profiles: ChatProfiles) -> None:
        stored = await self._threads.get(UUID(thread_id))
        assert stored is not None
        deleter = ThreadDeleter(self.launcher(profiles), ThreadAgentsIndex())

        await deleter.thread_deleted(stored)

    async def stop(self) -> None:
        await self._servers.stop()


@pytest.fixture
async def deletion(
    tmp_path: Path,
    stand: Stand,
    users: UsersTable,
    threads: ThreadsTable,
    auth_token: str,
) -> AsyncIterator[DeletionStand]:
    made = DeletionStand(tmp_path, stand, users, threads)
    try:
        yield made
    finally:
        await made.stop()


class TestThreadDeletion:
    """Удаление треда своим процессом агента: короткий процесс собирается по
    профилю треда из текущих [profiles] и стирает сессию из истории; профиль,
    исчезнувший из конфига, — отказ с понятной причиной, сессия цела."""

    async def test_session_is_deleted_after_the_inference_moved(
        self, deletion: DeletionStand, model: FakeModel
    ) -> None:
        """Тред открыт с одним адресом модели и удалён после смены адреса в
        профиле: процесс удаления собран по новому адресу, к прежнему
        запросов нет."""
        thread_id, session_id = await deletion.opened(deletion.profile(model))
        assert await deletion.history.rows(session_id) > 0
        asked = len(model.app.requests)

        moved = FakeModel()
        await moved.start()
        try:
            profiles = ChatProfiles({SignInStand.PROFILE: deletion.profile(moved)})
            await deletion.delete(thread_id, profiles)
        finally:
            await moved.stop()

        assert await deletion.history.rows(session_id) == 0
        assert len(model.app.requests) == asked

    async def test_thread_whose_profile_is_gone_is_refused(
        self, deletion: DeletionStand, model: FakeModel
    ) -> None:
        thread_id, session_id = await deletion.opened(deletion.profile(model))
        profiles = ChatProfiles({"other": deletion.profile(model)})

        with pytest.raises(AgentSettingsError) as refused:
            await deletion.delete(thread_id, profiles)

        text = str(refused.value)
        assert thread_id in text, text
        assert f"{SignInStand.PROFILE!r} of the thread is not in [profiles]" in text
        assert "['other']" in text, text
        assert await deletion.history.rows(session_id) > 0


def _is_zombie(pid: int) -> bool:
    status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")

    return "zombie" in status
