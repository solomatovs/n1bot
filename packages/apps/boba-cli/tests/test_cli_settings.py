"""Слои настроек: порядок приоритета, слияние таблиц и списков, профиль из
переменных окружения, `.mcp.json` формата Claude Code, секреты значениями в
файле с правами 0600, история `memory`, понятные отказы."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from boba.agent.history import HistoryError, MemoryHistoryStore, SessionId
from boba.agent.queue import QueueEntry, QueueKind
from boba.agent.records import PermissionMode, UserRecord
from boba.cli.settings import (
    Arguments,
    EnvName,
    HistoryKind,
    JsonlHistorySettings,
    McpServerBuilder,
    MemoryHistorySettings,
    PostgresBuilder,
    PostgresHistorySettings,
    ProfileBuilder,
    Settings,
    SettingsError,
    SettingsSource,
)
from boba.mcp_client.client import BearerAuth, HttpEndpoint, StdioCommand
from boba.stand.agent import Collected, FakeServer, Protocol, Served, StandAgents
from boba.stand.ui.fake_llm import ScenarioName

pytestmark = pytest.mark.anyio

OWNER_ONLY = 0o600
WORLD_READABLE = 0o644
API_KEY = "sk-file-secret"
DSN = "postgresql://agent:pg-file-secret@db.example.org:5432/boba"
SESSION = SessionId(value="settings-memory-1")

ENV = {
    EnvName.LLM_KIND.value: "openai",
    EnvName.LLM_BASE_URL.value: "http://127.0.0.1:9/v1",
    EnvName.LLM_MODEL.value: "fake-model",
}


def write(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def settings(
    tmp_path: Path,
    arguments: Arguments | None = None,
    env: dict[str, str] | None = None,
) -> Settings:
    if arguments is None:
        arguments = Arguments()

    if env is None:
        env = dict(ENV)

    return Settings(arguments, env, tmp_path / "home", tmp_path / "project")


class TestLayers:
    def test_env_profile_alone_is_enough(self, tmp_path: Path) -> None:
        effective = settings(tmp_path).effective()

        assert effective.model == "default"
        assert (
            effective.selected_model().provider.base_url
            == ENV[EnvName.LLM_BASE_URL.value]
        )
        assert isinstance(effective.history, JsonlHistorySettings)
        assert effective.history.root == str(tmp_path / "home" / ".boba" / "history")
        assert effective.permission_mode is PermissionMode.DEFAULT
        assert effective.system_prompt == []

    def test_without_any_model_the_error_names_the_keys(self, tmp_path: Path) -> None:
        with pytest.raises(SettingsError, match="BOBA_LLM_KIND"):
            settings(tmp_path, env={}).effective()

    def test_files_arguments_and_policy_win_in_order(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        project = tmp_path / "project"
        write(
            home / ".boba" / "settings.json",
            {
                "agentName": "User",
                "permissions": {"allow": ["a"]},
                "limits": {"maxTurns": 5, "apiRetries": 1},
            },
        )
        write(
            project / ".boba" / "settings.json",
            {"agentName": "Project", "permissions": {"allow": ["b"], "deny": ["x"]}},
        )
        write(
            project / ".boba" / "settings.local.json",
            {"agentName": "Local", "limits": {"maxTurns": 7}},
        )
        arguments = Arguments(
            settings=(
                json.dumps(
                    {"agentName": "Flag", "permissions": {"defaultMode": "plan"}}
                ),
            ),
            max_turns=9,
            policy=str(tmp_path / "policy.json"),
        )
        write(tmp_path / "policy.json", {"permissions": {"allowBypass": False}})

        effective = settings(tmp_path, arguments).effective()

        assert effective.agent_name == "Flag"
        assert effective.limits.max_turns == 9
        assert effective.limits.api_retries == 1
        assert list(effective.permissions.allow) == ["a", "b"]
        assert list(effective.permissions.deny) == ["x"]
        assert effective.permission_mode is PermissionMode.PLAN
        assert effective.allow_bypass is False

    def test_setting_sources_limit_the_files(self, tmp_path: Path) -> None:
        write(tmp_path / "home" / ".boba" / "settings.json", {"agentName": "User"})
        arguments = Arguments(setting_sources=(SettingsSource.PROJECT,))

        effective = settings(tmp_path, arguments).effective()

        assert effective.agent_name == "Boba"

    def test_broken_file_is_a_readable_error(self, tmp_path: Path) -> None:
        path = tmp_path / "home" / ".boba" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(SettingsError, match="not valid JSON"):
            settings(tmp_path).effective()

    def test_unknown_key_is_refused(self, tmp_path: Path) -> None:
        write(tmp_path / "home" / ".boba" / "settings.json", {"colour": "red"})

        with pytest.raises(SettingsError, match="settings schema"):
            settings(tmp_path).effective()

    def test_models_merge_by_name_and_model_flag_selects(self, tmp_path: Path) -> None:
        write(
            tmp_path / "home" / ".boba" / "settings.json",
            {
                "models": {
                    "big": {
                        "provider": {"kind": "ollama", "baseUrl": "http://o:11434"},
                        "model": "qwen",
                        "contextWindow": 16000,
                        "maxOutputTokens": 1024,
                    }
                }
            },
        )
        write(
            tmp_path / "project" / ".boba" / "settings.json",
            {"models": {"big": {"contextWindow": 32000}}},
        )

        effective = settings(tmp_path, Arguments(model="big")).effective()
        chosen = effective.selected_model()

        assert chosen.context_window == 32000
        assert chosen.max_output_tokens == 1024
        assert effective.public()["model"] == "big"

    def test_unknown_profile_is_named(self, tmp_path: Path) -> None:
        effective = settings(tmp_path, Arguments(model="nope")).effective()

        with pytest.raises(SettingsError, match="'nope' is not defined"):
            effective.selected_model()


class TestProfile:
    def test_api_key_comes_from_the_named_variable(self, tmp_path: Path) -> None:
        env = {**ENV, EnvName.LLM_API_KEY.value: "s3cret"}
        effective = settings(tmp_path, env=env).effective()

        profile = ProfileBuilder(env).profile("default", effective.selected_model())

        assert profile.model_id == "fake-model"
        dumped = profile.chat.model_dump()
        assert isinstance(dumped["provider"]["connection"]["auth"]["token"], SecretStr)
        assert "s3cret" not in json.dumps(effective.public())

    def test_missing_key_variable_is_an_error(self, tmp_path: Path) -> None:
        env = {**ENV, EnvName.LLM_API_KEY.value: "s3cret"}
        effective = settings(tmp_path, env=env).effective()

        with pytest.raises(SettingsError, match="BOBA_LLM_API_KEY"):
            ProfileBuilder(ENV).profile("default", effective.selected_model())

    def test_onnx_needs_a_model_dir(self, tmp_path: Path) -> None:
        write(
            tmp_path / "home" / ".boba" / "settings.json",
            {
                "model": "local",
                "models": {
                    "local": {
                        "provider": {"kind": "onnx"},
                        "model": "qwen3",
                        "contextWindow": 8000,
                        "maxOutputTokens": 256,
                    }
                },
            },
        )
        effective = settings(tmp_path).effective()

        with pytest.raises(SettingsError, match="modelDir"):
            ProfileBuilder(ENV).profile("local", effective.selected_model())


class TestMcpJson:
    def test_project_file_becomes_server_configs(self, tmp_path: Path) -> None:
        write(
            tmp_path / "project" / ".mcp.json",
            {
                "mcpServers": {
                    "std": {
                        "command": "python",
                        "args": ["server.py"],
                        "env": {"A": "1"},
                    },
                    "web": {
                        "type": "http",
                        "url": "https://mcp.example.org:8443/mcp",
                        "headers": {"Authorization": "Bearer t0k"},
                    },
                    "off": {"command": "python"},
                }
            },
        )
        arguments = Arguments(
            settings=(
                json.dumps({"mcp": {"disabledServers": ["off"]}, "env": {"B": "2"}}),
            ),
        )
        effective = settings(tmp_path, arguments).effective()
        builder = McpServerBuilder(effective.mcp, effective.env)

        assert sorted(effective.mcp_servers) == ["std", "web"]
        std = builder.config("std", effective.mcp_servers["std"]).endpoint
        assert isinstance(std, StdioCommand)
        assert std.command == "python"
        assert std.env == {"B": "2", "A": "1"}
        web = builder.config("web", effective.mcp_servers["web"]).endpoint
        assert isinstance(web, HttpEndpoint)
        assert web.url() == "https://mcp.example.org:8443/mcp"
        assert isinstance(web.auth, BearerAuth)
        assert web.auth.token.get_secret_value() == "t0k"

    def test_server_name_that_breaks_tool_names_is_refused(
        self, tmp_path: Path
    ) -> None:
        write(
            tmp_path / "project" / ".mcp.json",
            {"mcpServers": {"my__srv": {"command": "x"}}},
        )

        with pytest.raises(
            SettingsError, match=r"\.mcp\.json.*'my__srv'.*contains '__'"
        ):
            settings(tmp_path, Arguments()).effective()

    def test_only_enabled_servers_are_kept(self, tmp_path: Path) -> None:
        write(
            tmp_path / "project" / ".mcp.json",
            {"mcpServers": {"std": {"command": "x"}, "other": {"command": "y"}}},
        )
        arguments = Arguments(
            settings=(json.dumps({"mcp": {"enabledServers": ["other"]}}),)
        )

        effective = settings(tmp_path, arguments).effective()

        assert sorted(effective.mcp_servers) == ["other"]

    def test_scope_of_tool_calls_is_read_and_absent_by_default(
        self, tmp_path: Path
    ) -> None:
        arguments = Arguments(settings=(json.dumps({"mcp": {"scope": "thread-7"}}),))

        assert settings(tmp_path, arguments).effective().mcp.scope == "thread-7"
        assert settings(tmp_path).effective().mcp.scope is None

    def test_sse_server_is_refused(self, tmp_path: Path) -> None:
        write(
            tmp_path / "project" / ".mcp.json",
            {"mcpServers": {"old": {"type": "sse", "url": "http://x/sse"}}},
        )
        effective = settings(tmp_path).effective()

        with pytest.raises(SettingsError, match="sse"):
            McpServerBuilder(effective.mcp, effective.env).config(
                "old", effective.mcp_servers["old"]
            )


class TestPostgresHistory:
    def test_dsn_from_the_named_variable_becomes_a_config(self) -> None:
        history = PostgresHistorySettings(kind=HistoryKind.POSTGRES, dsn_env="PG")
        env = {
            "PG": "postgresql://agent:s3cret@db.example.org:5433/boba?sslmode=require"
        }

        config = PostgresBuilder(env).config(history)

        assert config.host == "db.example.org"
        assert config.port == 5433
        assert config.dbname == "boba"
        assert config.sslmode == "require"
        assert config.auth.method == "password"
        assert config.auth.user == "agent"
        assert "s3cret" not in config.model_dump_json()
        assert history.db_schema == "agent_history"

    def test_missing_variable_and_missing_user_are_errors(self) -> None:
        history = PostgresHistorySettings(kind=HistoryKind.POSTGRES, dsn_env="PG")

        with pytest.raises(SettingsError, match="PG"):
            PostgresBuilder({}).config(history)

        with pytest.raises(SettingsError, match="names no user"):
            PostgresBuilder({"PG": "postgresql://db/boba"}).config(history)


def write_private(path: Path, document: dict[str, object]) -> None:
    write(path, document)
    path.chmod(OWNER_ONLY)


def secret_document() -> dict[str, object]:
    return {
        "model": "user-main",
        "models": {
            "user-main": {
                "provider": {
                    "kind": "openai",
                    "baseUrl": "https://llm.local/v1",
                    "apiKey": API_KEY,
                },
                "model": "qwen3",
                "contextWindow": 32000,
                "maxOutputTokens": 4096,
            }
        },
        "history": {"kind": "postgres", "dsn": DSN, "schema": "agent"},
    }


class TestSecretsInFile:
    def test_private_file_with_secrets_is_parsed(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        write_private(path, secret_document())

        effective = settings(tmp_path, Arguments(settings=(str(path),))).effective()

        profile = ProfileBuilder({}).profile("user-main", effective.selected_model())
        token = profile.chat.model_dump()["provider"]["connection"]["auth"]["token"]
        assert isinstance(token, SecretStr)
        assert token.get_secret_value() == API_KEY
        assert isinstance(effective.history, PostgresHistorySettings)
        config = PostgresBuilder({}).config(effective.history)
        assert config.host == "db.example.org"
        assert config.auth.method == "password"
        assert effective.history.db_schema == "agent"
        public = json.dumps(effective.public())
        assert API_KEY not in public
        assert "pg-file-secret" not in public

    def test_world_readable_file_with_secrets_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        write(path, secret_document())
        path.chmod(WORLD_READABLE)

        with pytest.raises(SettingsError) as failure:
            settings(tmp_path, Arguments(settings=(str(path),))).effective()

        text = str(failure.value)
        assert str(path) in text
        assert "0644" in text
        assert "chmod 600" in text
        assert "history.dsn" in text
        assert "models.user-main.provider.apiKey" in text

    def test_user_file_with_secrets_is_checked_too(self, tmp_path: Path) -> None:
        path = tmp_path / "home" / ".boba" / "settings.json"
        write(path, secret_document())
        path.chmod(WORLD_READABLE)

        with pytest.raises(SettingsError, match="user settings"):
            settings(tmp_path).effective()

        path.chmod(OWNER_ONLY)
        effective = settings(tmp_path).effective()
        assert effective.model == "user-main"

    def test_world_readable_file_without_secrets_is_fine(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        write(path, {"agentName": "Open"})
        path.chmod(WORLD_READABLE)

        effective = settings(tmp_path, Arguments(settings=(str(path),))).effective()

        assert effective.agent_name == "Open"

    def test_secrets_on_the_command_line_are_refused(self, tmp_path: Path) -> None:
        arguments = Arguments(settings=(json.dumps(secret_document()),))

        with pytest.raises(SettingsError, match="command line"):
            settings(tmp_path, arguments).effective()

    def test_key_value_and_key_variable_exclude_each_other(
        self, tmp_path: Path
    ) -> None:
        document = secret_document()
        models = document["models"]
        assert isinstance(models, dict)
        models["user-main"]["provider"]["apiKeyEnv"] = "KEY"
        path = tmp_path / "settings.json"
        write_private(path, document)

        with pytest.raises(SettingsError, match="'apiKey' and 'apiKeyEnv'"):
            settings(tmp_path, Arguments(settings=(str(path),))).effective()

    def test_postgres_history_needs_dsn_or_dsn_env(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        write_private(path, {"history": {"kind": "postgres"}})

        with pytest.raises(SettingsError, match="'dsn'"):
            settings(tmp_path, Arguments(settings=(str(path),))).effective()

    def test_secret_survives_the_merge_of_layers(self, tmp_path: Path) -> None:
        write_private(tmp_path / "home" / ".boba" / "settings.json", secret_document())
        write(
            tmp_path / "project" / ".boba" / "settings.json",
            {"agentName": "Project", "limits": {"maxTurns": 3}},
        )

        effective = settings(tmp_path).effective()

        assert isinstance(effective.history, PostgresHistorySettings)
        assert effective.history.dsn is not None
        assert effective.history.dsn.get_secret_value() == DSN
        assert effective.agent_name == "Project"


@pytest.fixture
async def server() -> AsyncIterator[FakeServer]:
    fake = FakeServer()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


class TestMemoryHistory:
    def test_kind_memory_is_a_history_without_secrets(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        write(path, {"history": {"kind": "memory"}})
        path.chmod(WORLD_READABLE)

        effective = settings(tmp_path, Arguments(settings=(str(path),))).effective()

        assert isinstance(effective.history, MemoryHistorySettings)
        assert effective.history.kind is HistoryKind.MEMORY
        assert effective.public()["history"] == {"kind": "memory"}

    async def test_reopened_session_has_no_history(
        self, server: FakeServer, tmp_path: Path
    ) -> None:
        agents = StandAgents(server, tmp_path / "unused")
        store = MemoryHistoryStore()
        profile = agents.profile(Protocol.OPENAI)
        try:
            served = Served(agents.agent(profile, Collected(), store=store))
            session = await served.open(SESSION, resume=False)
            served.agent.queue.enqueue(
                QueueEntry(kind=QueueKind.PROMPT, text=ScenarioName.ANSWER.value)
            )
            await served.agent.settled()
            await served.close()

            spoken = [r for r in session.records() if isinstance(r, UserRecord)]
            assert len(spoken) == 1
            assert [line async for line in store.read(SESSION)] == []
            assert not (tmp_path / "unused").exists()

            again = agents.agent(profile, Collected(), store=store)
            with pytest.raises(HistoryError, match="no journal to resume"):
                await again.open(SESSION, resume=True)

            fresh = Served(agents.agent(profile, Collected(), store=store))
            reopened = await fresh.open(SESSION, resume=False)
            await fresh.close()
            assert [r for r in reopened.records() if isinstance(r, UserRecord)] == []
        finally:
            await agents.aclose()
