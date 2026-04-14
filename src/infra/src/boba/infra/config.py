"""Загрузка AppConfig из TOML-файла и переменных окружения.

Приоритет (от высшего к низшему):
    1. Env var <KEY>_FILE — путь к файлу с секретом
    2. Env var <KEY> — переменная окружения
    3. TOML-файл (секция [app]) — путь задаётся через BOBA_CONFIG
    4. Значение по умолчанию
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import tomli

from boba.domain.agent.models import AgentConfig
from boba.domain.config import AppConfig, LLMConfig


class ConfigLoader:
    """Загружает AppConfig, разрешая значения из env / TOML / секретов."""

    def _subsection(self, name: str) -> dict[str, Any]:
        val = self._app.get(name)
        return val if isinstance(val, dict) else {}

    def __init__(self) -> None:
        self._app = self._load_section("app")
        self._llm = self._subsection("llm")
        self._agent = self._subsection("agent")

    def load(self) -> AppConfig:
        return AppConfig(
            workspace_base_dir=self._resolve(
                "WORKSPACE_BASE_DIR", "workspace_base_dir", "./workspaces",
                section=self._app,
            ),
            ssl_verify=self._resolve(
                "SSL_VERIFY", "ssl_verify", "false",
                section=self._app,
            ).lower() in ("true", "1", "yes"),
            log_level=self._resolve(
                "LOG_LEVEL", "log_level", "INFO",
                section=self._app,
            ),
            llm=LLMConfig(
                base_url=self._resolve(
                    "LLM_BASE_URL", "base_url", "http://localhost:11434/v1",
                    section=self._llm,
                ),
                api_key=self._resolve(
                    "LITELLM_API_KEY", "api_key", "ollama",
                    section=self._llm,
                ),
                model=self._resolve(
                    "LLM_MODEL", "model", "qwen3:8b",
                    section=self._llm,
                ),
            ),
            agent=AgentConfig(
                max_iterations=int(self._resolve(
                    "AGENT_MAX_ITERATIONS", "max_iterations", "20",
                    section=self._agent,
                )),
            ),
        )

    def _resolve(
        self,
        key: str,
        toml_key: str,
        default: str = "",
        section: dict[str, Any] | None = None,
    ) -> str:
        file_path = os.environ.get(f"{key}_FILE")
        if file_path:
            p = Path(file_path)
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()

        env_val = os.environ.get(key)
        if env_val is not None:
            return env_val

        src = section if section is not None else self._app
        toml_val = src.get(toml_key)
        if toml_val is not None:
            return str(toml_val)

        return default

    @staticmethod
    def _load_section(section: str) -> dict[str, Any]:
        config_path = os.environ.get("BOBA_CONFIG", "")
        if not config_path:
            return {}
        path = Path(config_path)
        if not path.is_file():
            return {}
        try:
            with open(path, "rb") as f:
                data = tomli.load(f)
            return data.get(section, {})
        except Exception:
            return {}
