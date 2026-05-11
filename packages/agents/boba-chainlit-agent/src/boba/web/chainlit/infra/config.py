"""DTO chainlit-приложения: AppCoreConfig + плоский AppConfig."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from boba.agent.orchestrator import AgentConfig
from boba.agent.prompt_providers import PromptsConfig
from boba.agent.workspace_fs import WorkspaceLayout
from boba.provider.openai import OpenAIConfig
from boba.schema import Inline
from boba.schema.coercion import ParseBool, ParseString

__all__ = ["AppConfig", "AppCoreConfig"]


@dataclass(frozen=True)
class AppCoreConfig:
    """Кросс-слойные настройки приложения: SSL/логирование."""

    ssl_verify: Annotated[
        bool, "Проверять ли TLS-сертификат у HTTPS-запросов.", ParseBool(),
    ] = False
    log_level: Annotated[
        str, "Уровень корневого логгера.", ParseString(),
    ] = "INFO"
    log_file: Annotated[
        str | None, "Путь к log-файлу. Пусто — логи в stderr.", ParseString(),
    ] = None


@dataclass(frozen=True)
class AppConfig:
    """Плоский agent-конфиг: core/workspaces/openai/prompts/runtime."""

    core:       Annotated[AppCoreConfig,   Inline()]
    workspaces: Annotated[WorkspaceLayout, Inline()]
    openai:     Annotated[OpenAIConfig,    Inline()]
    prompts:    Annotated[PromptsConfig,   Inline()]
    runtime:    Annotated[AgentConfig,     Inline()]
