"""DTO cli-приложения: AppCoreConfig + composite AppConfig.

Источники (priority high → low):
  1. init-kwargs (`AppConfig(core=AppCoreConfig(...))`).
  2. env:  плоский namespace `BOBA_AGENT__<FIELD>` — имена sub-моделей
            не нужны, поля распределяются по sub-моделям через model-validator
            (`BobaFlatSettings._redistribute_flat_keys`):
              BOBA_AGENT__LOG_LEVEL=INFO       # → core.log_level
              BOBA_AGENT__BASE_URL=...         # → openai.base_url
              BOBA_AGENT__MAX_ITERATIONS=20    # → runtime.max_iterations
            Nested-форма тоже работает:
              BOBA_AGENT__OPENAI__BASE_URL=... # → openai.base_url
  3. TOML: плоская секция [agent] в файле $BOBA_CONFIG_PATH.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

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
        config_path="agent",
    )

    core: AppCoreConfig = Field(default_factory=AppCoreConfig)
    workspaces: WorkspaceLayout = Field(default_factory=WorkspaceLayout)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    system_prompt_dir: str = Field(
        description="Корневая директория .md/.txt-файлов с system-prompt",
    )
