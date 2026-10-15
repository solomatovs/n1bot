"""Настройки агента: слои пользователь → чат в Postgres стенда с шифрованием
секретов, правка панели в оба слоя, файл настроек по форме агента в приватном
каталоге 0700/0600 без серверов MCP, хуков и области вызовов, строка
подключения истории, и рукопожатие настоящего `boba acp` v2 по этому файлу —
секреты не попадают в argv и окружение ребёнка, каталог удаляется с
процессом."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from uuid import uuid4

import pytest
from acp_stand import (
    API_KEY,
    AgentHistoryStand,
    AgentStand,
    FakeModel,
    ProcFiles,
    RecordingUpdates,
)
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import SecretStr, ValidationError

from boba.chainlit.agent.client import AgentClient, AgentProcess
from boba.chainlit.agent.config import (
    PostgresAgentHistory,
)
from boba.chainlit.agent.server import ProcessKeys
from boba.chainlit.agent.settings import (
    AgentSettingsChange,
    AgentSettingsError,
    AgentSettingsLayers,
    AgentSettingsTable,
    AgentWorkdirs,
    HistoryEnvironment,
    PermissionModeName,
    ProfileModelSpecs,
    SettingsFileBuilder,
    UserAgentSettings,
)
from boba.chat.profiles import ChatProfileConfig
from boba.connections.secrets import SecretCipher
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.connection.auth import CertificateAuth
from boba.kerberos import KeytabAuth
from boba.krb.credentials import KerberosEnv
from boba.llm.onnx import OnnxProvider
from boba.mcp_client.client import McpServers, McpServersConfig
from boba.stand.names import StandNames
from boba.stand.signin import SignInStand
from boba.stand.site import Stand

pytestmark = pytest.mark.anyio

SCHEMA = StandNames().of("acp_settings")
CIPHER_KEY = b"0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Контекст chainlit этим тестам не нужен: они живут ниже обработчиков."""


@pytest.fixture
async def table(
    test_postgres: PostgresConfig, pool: AsyncPostgresPool
) -> AgentSettingsTable:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    table = AgentSettingsTable(test_postgres, SCHEMA, SecretCipher(CIPHER_KEY), pool)
    await table.setup()

    return table


@pytest.fixture
def specs() -> ProfileModelSpecs:
    return ProfileModelSpecs(context_window=32000, max_output_tokens=4096)


class TestLayers:
    """Слои настроек: профиль чата → слой пользователя → копия в слой чата."""

    async def test_chat_layer_is_copied_from_the_user_layer_once(
        self, table: AgentSettingsTable, specs: ProfileModelSpecs
    ) -> None:
        layers = AgentSettingsLayers(table, specs)
        user_id = uuid4()
        thread_id = uuid4()
        profile = SignInStand.profile()

        await table.save_user_layer(
            user_id,
            SignInStand.PROFILE,
            UserAgentSettings(model="user-picked", api_key=SecretStr("user-key")),
        )

        chat = await layers.chat_layer(thread_id, user_id, SignInStand.PROFILE, profile)
        assert chat.model.model == "user-picked"
        assert chat.model.provider.api_key is not None
        assert chat.model.provider.api_key.get_secret_value() == "user-key"
        assert chat.model.provider.base_url == "https://fake-llm/v1"
        assert chat.system_prompt == "stand"

        # слой пользователя сменился, слой чата живёт своей жизнью
        await table.save_user_layer(
            user_id, SignInStand.PROFILE, UserAgentSettings(model="changed-later")
        )
        again = await layers.chat_layer(
            thread_id, user_id, SignInStand.PROFILE, profile
        )
        assert again == chat

        fresh = await layers.chat_layer(uuid4(), user_id, SignInStand.PROFILE, profile)
        assert fresh.model.model == "changed-later"

    async def test_secrets_are_encrypted_at_rest(
        self, table: AgentSettingsTable, pool: AsyncPostgresPool
    ) -> None:
        user_id = uuid4()
        await table.save_user_layer(
            user_id, "general", UserAgentSettings(api_key=SecretStr("sk-very-secret"))
        )
        async with pool.connection() as conn:
            row = await conn.execute(
                sql.SQL("select settings::text from {}.agent_user_settings").format(
                    sql.Identifier(SCHEMA)
                )
            )
            fetched = await row.fetchone()

        assert fetched is not None
        raw = fetched[0]
        assert "sk-very-secret" not in raw
        assert "enc:v1:" in raw

        loaded = await table.user_layer(user_id, "general")
        assert loaded.api_key is not None
        assert loaded.api_key.get_secret_value() == "sk-very-secret"

    async def test_missing_rows_read_as_empty_layers(
        self, table: AgentSettingsTable
    ) -> None:
        assert await table.user_layer(uuid4(), "general") == UserAgentSettings()
        assert await table.chat_layer(uuid4()) is None

        thread_id = uuid4()
        await table.delete_chat_layer(thread_id)
        assert await table.chat_layer(thread_id) is None

    async def test_retired_keys_of_a_stored_user_layer_are_ignored(
        self, table: AgentSettingsTable, pool: AsyncPostgresPool
    ) -> None:
        """Строка прошлой версии с дополнением системного промпта читается:
        снятый ключ отбрасывается, остальные поля на месте."""
        user_id = uuid4()
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL(
                    "insert into {}.agent_user_settings (user_id, profile, settings) "
                    "values (%s, %s, %s)"
                ).format(sql.Identifier(SCHEMA)),
                (
                    user_id,
                    "general",
                    Jsonb({"model": "kept", "system_prompt": "be terse"}),
                ),
            )

        loaded = await table.user_layer(user_id, "general")
        assert loaded == UserAgentSettings(model="kept")

    async def test_change_writes_the_user_layer_and_the_chat_layer(
        self, table: AgentSettingsTable, specs: ProfileModelSpecs
    ) -> None:
        layers = AgentSettingsLayers(table, specs)
        user_id = uuid4()
        thread_id = uuid4()
        other_thread = uuid4()
        profile = SignInStand.profile()
        opened = await layers.chat_layer(
            thread_id, user_id, SignInStand.PROFILE, profile
        )
        untouched = await layers.chat_layer(
            other_thread, user_id, SignInStand.PROFILE, profile
        )
        assert opened.mode is PermissionModeName.BYPASS

        edited = UserAgentSettings(model="picked", api_key=SecretStr("user-key"))
        changed = await layers.change(
            thread_id,
            user_id,
            SignInStand.PROFILE,
            profile,
            AgentSettingsChange(
                user=edited, mode=PermissionModeName.DEFAULT, model_name=None
            ),
        )

        assert changed.model.model == "picked"
        assert changed.model.provider.api_key == SecretStr("user-key")
        assert changed.model.provider.base_url == opened.model.provider.base_url
        assert changed.mode is PermissionModeName.DEFAULT
        assert changed.system_prompt == "stand"
        assert await table.chat_layer(thread_id) == changed
        assert await table.user_layer(user_id, SignInStand.PROFILE) == edited
        # чужой открытый тред живёт со своим слоем, новый берёт слой пользователя
        assert await table.chat_layer(other_thread) == untouched
        fresh = await layers.chat_layer(uuid4(), user_id, SignInStand.PROFILE, profile)
        assert fresh.model.model == "picked"
        assert fresh.mode is PermissionModeName.BYPASS

    async def test_session_choice_alone_keeps_the_provider_of_the_chat(
        self, table: AgentSettingsTable, specs: ProfileModelSpecs
    ) -> None:
        """Слой пользователя сменился в другом чате; правка одного режима в
        этом чате его провайдера не трогает."""
        layers = AgentSettingsLayers(table, specs)
        user_id = uuid4()
        thread_id = uuid4()
        profile = SignInStand.profile()
        opened = await layers.chat_layer(
            thread_id, user_id, SignInStand.PROFILE, profile
        )
        elsewhere = UserAgentSettings(model="changed-elsewhere")
        await table.save_user_layer(user_id, SignInStand.PROFILE, elsewhere)

        changed = await layers.change(
            thread_id,
            user_id,
            SignInStand.PROFILE,
            profile,
            AgentSettingsChange(
                user=elsewhere, mode=PermissionModeName.PLAN, model_name=None
            ),
        )

        assert changed.model == opened.model
        assert changed.mode is PermissionModeName.PLAN

    def test_local_provider_is_refused(self, specs: ProfileModelSpecs) -> None:
        profile = ChatProfileConfig.model_validate(
            {
                "display_name": "Local",
                "description": "onnx profile",
                "roles": ["*"],
                "tools": [],
                "provider": OnnxProvider(kind="onnx", model_dir="/nonexistent"),
                "model": "local",
            }
        )
        with pytest.raises(AgentSettingsError, match="remote provider"):
            specs.model("local", profile)


class TestHistoryDsn:
    """Строка подключения истории агента из [agent.history]."""

    def test_password_auth_renders_a_libpq_dsn(self, stand: Stand) -> None:
        history = AgentHistoryStand(stand).history()
        dsn = history.dsn().get_secret_value()
        assert f"user={stand.pg_probe_user}" in dsn
        assert f"password={stand.pg_probe_password.get_secret_value()}" in dsn
        assert f"dbname={stand.pg_probe_database}" in dsn
        assert "gssencmode" not in dsn

    def test_certificate_auth_is_refused(self, test_postgres: PostgresConfig) -> None:
        certificate = test_postgres.model_copy(
            update={
                "auth": CertificateAuth(
                    method="certificate", user="boba", sslcert="/c.pem", sslkey="/k.pem"
                )
            }
        )
        with pytest.raises(ValidationError, match="cannot be handed to the agent"):
            PostgresAgentHistory.model_validate(
                {"kind": "postgres", "postgres": certificate, "schema": "agent"}
            )

    async def test_kerberos_role_renders_a_dsn_without_password_and_libkrb5_env(
        self, test_postgres: PostgresConfig
    ) -> None:
        auth = test_postgres.auth
        if not isinstance(auth, KeytabAuth):
            pytest.skip("the stand postgres is not entered with a keytab")

        history = PostgresAgentHistory.model_validate(
            {"kind": "postgres", "postgres": test_postgres, "schema": "agent"}
        )
        dsn = history.dsn().get_secret_value()
        assert f"user={auth.principal.partition('@')[0]}" in dsn
        assert "password" not in dsn
        assert f"dbname={test_postgres.dbname}" in dsn

        environment = await HistoryEnvironment(history).prepare()
        assert set(environment) == {KerberosEnv.CCACHE, KerberosEnv.CONFIG}
        assert Path(environment[KerberosEnv.CONFIG]).is_file()
        ccache = environment[KerberosEnv.CCACHE]
        assert Path(ccache.partition(":")[2]).is_file(), ccache

    async def test_password_role_needs_no_environment(self, stand: Stand) -> None:
        history = AgentHistoryStand(stand).history()
        assert await HistoryEnvironment(history).prepare() == {}


class TestSettingsFile:
    """Файл настроек в приватном каталоге: форма агента, права, хук."""

    def test_file_has_the_agent_form_and_private_modes(
        self, tmp_path: Path, stand: Stand
    ) -> None:
        model = FakeModel()
        history = AgentHistoryStand(stand).history()
        config = AgentStand(tmp_path / "agents", history).config(
            "http://127.0.0.1:8000/boba"
        )
        builder = SettingsFileBuilder(config)
        chat = model.chat_settings("Answer briefly")

        document = builder.build(chat)
        workdir = AgentWorkdirs(tmp_path / "agents").create("thread-1")
        workdir.write(document, chat.system_prompt)

        assert stat.S_IMODE(workdir.path.stat().st_mode) == 0o700
        assert stat.S_IMODE(workdir.settings_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(workdir.prompt_path.stat().st_mode) == 0o600
        assert workdir.prompt_path.read_text(encoding="utf-8") == "Answer briefly"

        body = json.loads(workdir.settings_path.read_text(encoding="utf-8"))
        assert body["model"] == chat.model_name
        provider = body["models"][chat.model_name]["provider"]
        assert provider == {
            "kind": "openai",
            "baseUrl": model.base_url(),
            "apiKey": API_KEY,
        }
        assert body["models"][chat.model_name]["contextWindow"] == 32000
        assert body["permissions"] == {
            "defaultMode": "bypassPermissions",
            "allowBypass": True,
        }
        assert body["history"]["kind"] == "postgres"
        assert body["history"]["schema"] == history.db_schema
        assert body["history"]["dsn"] == history.dsn().get_secret_value()
        assert body["systemPromptFile"] == "system_prompt.txt"
        assert "mcp" not in body
        assert "mcpServers" not in body
        assert "hooks" not in body

        workdir.remove()
        assert not workdir.exists()

    def test_permission_mode_of_the_chat_layer_is_the_default_mode(
        self, tmp_path: Path, stand: Stand
    ) -> None:
        config = AgentStand(tmp_path, AgentHistoryStand(stand).history()).config()
        chat = FakeModel().chat_settings().with_session(PermissionModeName.PLAN, None)

        body = json.loads(SettingsFileBuilder(config).build(chat).render())

        assert body["permissions"] == {"defaultMode": "plan", "allowBypass": True}


class TestAgentHandshake:
    """Настоящий `boba acp` v2 принимает файл и держит секреты только в нём."""

    async def test_agent_initializes_and_leaks_nothing(
        self, tmp_path: Path, stand: Stand
    ) -> None:
        model = FakeModel()
        await model.start()
        history = AgentHistoryStand(stand).history()
        agent_stand = AgentStand(tmp_path / "agents", history)
        chat = model.chat_settings()
        document = SettingsFileBuilder(agent_stand.config()).build(chat)
        workdir = AgentWorkdirs(tmp_path / "agents").create("thread-2")
        workdir.write(document, chat.system_prompt)

        client = AgentClient(RecordingUpdates())
        relay = agent_stand.relay(
            ProcessKeys(), McpServers(McpServersConfig(servers={}))
        )
        process = AgentProcess(agent_stand.command(), workdir, client, relay, (), "0.0")
        try:
            response = await process.start()
            assert response.info.name == "boba"

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
            await model.stop()
            await process.close()

        assert not workdir.exists()
        assert not process.alive()
        assert not os.path.exists(f"/proc/{process.pid}/cmdline") or _is_zombie(
            process.pid
        )


def _is_zombie(pid: int) -> bool:
    status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")

    return "zombie" in status
