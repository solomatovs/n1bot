"""Договор запуска агента: файл настроек, который чат пишет своей моделью
(SettingsFile), валиден по JSON Schema, которую агент публикует командой
`boba settings-schema` (запуск модулем, как агент стартует в отладке и
тестах). Стендов не нужно: подключение истории только рендерится в строку
libpq, агент только печатает схему."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from acp_stand import AGENT_MODULE, AgentStand
from jsonschema import Draft202012Validator
from pydantic import SecretStr

from boba.aid.agent.config import (
    AgentConfig,
    AgentHistoryKind,
    MemoryAgentHistory,
    PostgresAgentHistory,
)
from boba.aid.agent.settings import (
    AgentProviderKind,
    AgentWorkdirs,
    McpFile,
    ModelFile,
    ProviderFile,
    SettingsFileBuilder,
)
from boba.chat.profiles import ReasoningReturn
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.connection.auth import PasswordAuth
from boba.mcp_client.client import McpServersConfig
from boba.transport.http.connection import BearerAuth

PROFILE = "general"
POSTGRES = PostgresConfig.model_validate(
    {
        "host": "db.example.test",
        "port": 5432,
        "dbname": "boba",
        "connect_timeout": 10,
        "pool": {"min_size": 2, "max_size": 5, "timeout": 7.0},
        "auth": PasswordAuth(
            method="password", user="boba", password=SecretStr("pw-secret")
        ),
    }
)
HISTORY = PostgresAgentHistory.model_validate(
    {"kind": "postgres", "schema": "boba", "postgres": POSTGRES}
)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Контекст chainlit этим тестам не нужен: они живут ниже обработчиков."""


@pytest.fixture(scope="module")
def published_schema() -> Draft202012Validator:
    """Схема файла настроек из самого агента: `python -m boba.cli.app
    settings-schema`."""
    printed = subprocess.run(
        [sys.executable, "-m", AGENT_MODULE, "settings-schema"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert printed.returncode == 0, printed.stderr[-2000:]
    document = json.loads(printed.stdout)
    Draft202012Validator.check_schema(document)

    return Draft202012Validator(document)


def _model() -> ModelFile:
    """Профиль модели с удалённым провайдером и всеми полями, которые
    ProfileModelSpecs берёт из соединения профиля."""
    provider = ProviderFile(
        kind=AgentProviderKind.OPENAI,
        base_url="https://llm.example.test/v1",
        auth=BearerAuth(method="bearer", token=SecretStr("sk-contract-secret")),
        timeout_sec=90.0,
        ssl_verify=False,
        retry_attempts=3,
        retry_backoff_sec=0.5,
        retry_statuses={429: 5, 503: 2},
        retry_after_max_sec=30.0,
        transport={"dump": {"enabled": False}},
    )
    return ModelFile(
        provider=provider,
        model="fake-model-general",
        sampling={"temperature": 0.2},
        context_window=32000,
        max_output_tokens=4096,
        reasoning_return=ReasoningReturn.TURN,
    )


def _config(
    tmp_path: Path, history: PostgresAgentHistory | MemoryAgentHistory
) -> AgentConfig:
    base = AgentStand(tmp_path / "agents", HISTORY).config()

    return base.model_copy(update={"history": history})


def _written(
    tmp_path: Path, config: AgentConfig, mcp: McpFile | None
) -> dict[str, Any]:
    """Документ файла настроек, как его записал бы запуск агента треда."""
    builder = SettingsFileBuilder(config, McpServersConfig(servers={}))
    env = {"KRB5CCNAME": "FILE:/run/boba/krb5cc", "KRB5_CONFIG": "/etc/boba/krb5.conf"}
    workdir = AgentWorkdirs(tmp_path / "agents").create("thread-contract")
    workdir.write(builder.build(PROFILE, _model(), mcp, env), "Answer briefly")
    try:
        return json.loads(workdir.settings_path.read_text(encoding="utf-8"))
    finally:
        workdir.remove()


def _errors(validator: Draft202012Validator, document: dict[str, Any]) -> Iterator[str]:
    for error in validator.iter_errors(document):
        path = "/".join(str(part) for part in error.absolute_path)
        yield f"{path}: {error.message}"


class TestSettingsContract:
    """Файл чата против схемы агента."""

    def test_postgres_history_with_servers_is_valid(
        self, tmp_path: Path, published_schema: Draft202012Validator
    ) -> None:
        document = _written(
            tmp_path, _config(tmp_path, HISTORY), McpFile(call_timeout_sec=120.0)
        )

        assert list(_errors(published_schema, document)) == []
        assert document["history"] == {
            "kind": AgentHistoryKind.POSTGRES.value,
            "dsn": document["history"]["dsn"],
            "schema": "boba",
            "pool": {"minSize": 2, "maxSize": 5, "timeoutSec": 7.0},
        }
        assert "password=pw-secret" in document["history"]["dsn"]
        assert document["permissions"] == {"allowBypass": True}
        auth = document["models"][PROFILE]["provider"]["auth"]
        assert auth == {"method": "bearer", "token": "sk-contract-secret"}
        assert document["models"][PROFILE]["reasoningReturn"] == "turn"
        assert document["mcp"] == {"callTimeoutSec": 120.0}

    def test_memory_history_without_servers_is_valid(
        self, tmp_path: Path, published_schema: Draft202012Validator
    ) -> None:
        memory = MemoryAgentHistory.model_validate({"kind": "memory"})

        document = _written(tmp_path, _config(tmp_path, memory), None)

        assert list(_errors(published_schema, document)) == []
        assert document["history"] == {"kind": AgentHistoryKind.MEMORY.value}
        assert document["mcp"] is None

    def test_schema_rejects_the_old_snake_case_form(
        self, tmp_path: Path, published_schema: Draft202012Validator
    ) -> None:
        """Схема агента строгая: ключ не по договору — ошибка, значит
        проверка выше не пустая."""
        document = _written(tmp_path, _config(tmp_path, HISTORY), None)
        document["permissions"] = {"allow_bypass": True}

        errors = list(_errors(published_schema, document))

        assert errors, "the schema must forbid keys outside the contract"
        assert any("allow_bypass" in error for error in errors), errors
