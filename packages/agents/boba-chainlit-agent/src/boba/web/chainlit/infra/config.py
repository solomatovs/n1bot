"""DTO chainlit-приложения: AppCoreConfig + composite AppConfig.

Идентичен boba-cli-agent/.../infra/config.py: оба читают одну и ту же
плоскую секцию `[agent]` под одним env-префиксом `BOBA_AGENT__`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boba.agent.prompt_providers import PromptsConfig
from boba.agent.workspace_fs import WorkspaceLayout
from boba.provider.openai import OpenAIConfig
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["AppConfig", "AppCoreConfig"]


class AppCoreConfig(BaseModel):
    """Кросс-слойные настройки приложения: SSL/логирование."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ssl_verify: bool = Field(
        default=False,
        description="Проверять ли TLS-сертификат у HTTPS-запросов.",
    )
    log_level: str = Field(
        default="INFO",
        description="Уровень корневого логгера.",
    )
    log_file: str | None = Field(
        default=None,
        description="Путь к log-файлу. Пусто — логи в stderr.",
    )


class AppConfig(BobaFlatSettings):
    """Конфиг агента: core/workspaces/openai/prompts."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        boba_env_prefix="BOBA_AGENT__",
        boba_toml_section="agent",
    )

    core: AppCoreConfig = Field(default_factory=AppCoreConfig)
    workspaces: WorkspaceLayout = Field(default_factory=WorkspaceLayout)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    prompts: PromptsConfig
