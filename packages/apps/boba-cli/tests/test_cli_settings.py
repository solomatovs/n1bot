"""Слои настроек: порядок приоритета, слияние таблиц и списков, профиль из
переменных окружения, `.mcp.json` формата Claude Code, понятные отказы."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from boba.agent.records import PermissionMode
from boba.cli.settings import (
    Arguments,
    EnvName,
    JsonlHistorySettings,
    McpServerBuilder,
    ProfileBuilder,
    Settings,
    SettingsError,
    SettingsSource,
)
from boba.mcp_client.client import BearerAuth, HttpEndpoint, StdioCommand

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
            settings=(json.dumps({"agentName": "Flag"}),),
            max_turns=9,
            permission_mode=PermissionMode.PLAN,
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
    def test_project_file_and_argument_become_server_configs(
        self, tmp_path: Path
    ) -> None:
        write(
            tmp_path / "project" / ".mcp.json",
            {
                "mcpServers": {
                    "std": {
                        "command": "python",
                        "args": ["server.py"],
                        "env": {"A": "1"},
                    },
                    "off": {"command": "python"},
                }
            },
        )
        arguments = Arguments(
            mcp_config=(
                json.dumps(
                    {
                        "mcpServers": {
                            "web": {
                                "type": "http",
                                "url": "https://mcp.example.org:8443/mcp",
                                "headers": {"Authorization": "Bearer t0k"},
                            }
                        }
                    }
                ),
            ),
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

    def test_strict_flag_ignores_the_project_file(self, tmp_path: Path) -> None:
        write(
            tmp_path / "project" / ".mcp.json",
            {"mcpServers": {"std": {"command": "x"}}},
        )

        effective = settings(tmp_path, Arguments(strict_mcp_config=True)).effective()

        assert effective.mcp_servers == {}

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
