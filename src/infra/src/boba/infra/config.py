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
from types import MappingProxyType
from typing import Any

import tomli

from boba.domain.agent.models import (
    AgentConfig,
    LLMRequestDefaults,
    SamplingParams,
)
from boba.domain.config import AppConfig, LLMConfig, WorkspaceLayout
from boba.domain.core.workspace import (
    SYSTEM_WORKSPACE_KIND,
    TMP_WORKSPACE_KIND,
    USER_WORKSPACE_KIND,
    WorkspaceKind,
)


class ConfigLoader:
    """Загружает AppConfig, разрешая значения из env / TOML / секретов."""

    def _subsection(self, name: str) -> dict[str, Any]:
        val = self._app.get(name)
        return val if isinstance(val, dict) else {}

    _WORKSPACE_KINDS: tuple[tuple[WorkspaceKind, str], ...] = (
        (USER_WORKSPACE_KIND, "WORKSPACE_USER_SUBDIR"),
        (SYSTEM_WORKSPACE_KIND, "WORKSPACE_SYSTEM_SUBDIR"),
        (TMP_WORKSPACE_KIND, "WORKSPACE_TMP_SUBDIR"),
    )

    def __init__(self) -> None:
        self._app = self._load_section("app")
        self._llm = self._subsection("llm")
        self._agent = self._subsection("agent")
        self._workspaces = self._subsection("workspaces")

    def load_app(self) -> AppConfig:
        """Кросс-слойные настройки приложения + :class:`LLMConfig`."""
        return AppConfig(
            workspaces=self._load_workspace_layout(),
            ssl_verify=self._resolve(
                "SSL_VERIFY",
                "ssl_verify",
                "false",
                section=self._app,
            ).lower()
            in ("true", "1", "yes"),
            log_level=self._resolve(
                "LOG_LEVEL",
                "log_level",
                "INFO",
                section=self._app,
            ),
            llm=LLMConfig(
                base_url=self._resolve(
                    "LLM_BASE_URL",
                    "base_url",
                    "http://localhost:11434/v1",
                    section=self._llm,
                ),
                api_key=self._resolve(
                    "LITELLM_API_KEY",
                    "api_key",
                    "ollama",
                    section=self._llm,
                ),
                model=self._resolve(
                    "LLM_MODEL",
                    "model",
                    "qwen3:8b",
                    section=self._llm,
                ),
            ),
        )

    def _load_workspace_layout(self) -> WorkspaceLayout:
        base_dir = self._resolve(
            "WORKSPACE_BASE_DIR",
            "base_dir",
            "./workspaces",
            section=self._workspaces,
        )
        subdirs = {
            kind: self._resolve(
                env_key,
                kind.name,
                kind.name,
                section=self._workspaces,
            )
            for kind, env_key in self._WORKSPACE_KINDS
        }
        return WorkspaceLayout(
            base_dir=base_dir,
            subdirs=MappingProxyType(subdirs),
        )

    def load_llm_defaults(self) -> LLMRequestDefaults:
        """Дефолты sampling и ``parallel_tool_calls`` для LLM-запроса.
        Живут в agent-слое (:class:`LLMRequestDefaults`), поэтому
        грузятся отдельным методом — :class:`LLMConfig` остаётся
        транспортным."""
        s = self._llm
        return LLMRequestDefaults(
            sampling=SamplingParams(
                temperature=self._float_opt("LLM_TEMPERATURE", "temperature", s),
                top_p=self._float_opt("LLM_TOP_P", "top_p", s),
                max_tokens=self._int_opt("LLM_MAX_TOKENS", "max_tokens", s),
                seed=self._int_opt("LLM_SEED", "seed", s),
                stop=self._list_opt("LLM_STOP", "stop", s),
                frequency_penalty=self._float_opt(
                    "LLM_FREQUENCY_PENALTY", "frequency_penalty", s
                ),
                presence_penalty=self._float_opt(
                    "LLM_PRESENCE_PENALTY", "presence_penalty", s
                ),
            ),
            parallel_tool_calls=self._bool_opt(
                "LLM_PARALLEL_TOOL_CALLS", "parallel_tool_calls", s
            ),
        )

    def load_agent(self) -> AgentConfig:
        """Настройки agent-loop'а. Отдельный метод — :class:`AppConfig`
        не тянет зависимость на agent-слой."""
        return AgentConfig(
            max_iterations=int(
                self._resolve(
                    "AGENT_MAX_ITERATIONS",
                    "max_iterations",
                    "20",
                    section=self._agent,
                )
            ),
            max_consecutive_tool_calls=int(
                self._resolve(
                    "AGENT_MAX_CONSECUTIVE_TOOL_CALLS",
                    "max_consecutive_tool_calls",
                    "3",
                    section=self._agent,
                )
            ),
            max_consecutive_format_failures=int(
                self._resolve(
                    "AGENT_MAX_CONSECUTIVE_FORMAT_FAILURES",
                    "max_consecutive_format_failures",
                    "3",
                    section=self._agent,
                )
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

    def _resolve_opt(
        self, key: str, toml_key: str, section: dict[str, Any]
    ) -> str | None:
        """Вариант ``_resolve`` без дефолта: возвращает ``None``, если
        значение нигде не задано. Списки/словари из TOML игнорирует
        (для них есть :meth:`_list_opt`)."""
        file_path = os.environ.get(f"{key}_FILE")
        if file_path:
            p = Path(file_path)
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()

        env_val = os.environ.get(key)
        if env_val is not None:
            return env_val

        toml_val = section.get(toml_key)
        if toml_val is None or isinstance(toml_val, (list, dict)):
            return None
        return str(toml_val)

    def _float_opt(
        self, key: str, toml_key: str, section: dict[str, Any]
    ) -> float | None:
        raw = self._resolve_opt(key, toml_key, section)
        return float(raw) if raw is not None else None

    def _int_opt(
        self, key: str, toml_key: str, section: dict[str, Any]
    ) -> int | None:
        raw = self._resolve_opt(key, toml_key, section)
        return int(raw) if raw is not None else None

    def _bool_opt(
        self, key: str, toml_key: str, section: dict[str, Any]
    ) -> bool | None:
        raw = self._resolve_opt(key, toml_key, section)
        if raw is None:
            return None
        return raw.strip().lower() in ("true", "1", "yes", "on")

    def _list_opt(
        self, key: str, toml_key: str, section: dict[str, Any]
    ) -> list[str] | None:
        """TOML-list имеет приоритет, env — CSV-строка."""
        toml_val = section.get(toml_key)
        if isinstance(toml_val, list):
            return [str(v) for v in toml_val]
        env_val = os.environ.get(key)
        if env_val is not None:
            return [s for s in env_val.split(",") if s]
        return None

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
