"""AgentRuntimeConfig — общие runtime-настройки агента для composition root."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boba.provider.openai import OpenAIConfig

__all__ = ["AgentRuntimeConfig"]


class AgentRuntimeConfig(BaseModel):
    """Общие runtime-настройки агента: логи + workspace'ы + LLM + промпт.

    Plain BaseModel, не settings: подключается как nested-поле в
    конкретный AppConfig каждого приложения (CLI/Chainlit). Оператор
    задаёт поля плоско в своей TOML-секции, `BobaFlatSettings`-валидатор
    распределяет их по дереву (`log_level` → `runtime.log_level`,
    `base_url` → `runtime.openai.base_url`, и т.д.).
    """

    model_config = ConfigDict(extra="forbid")

    log_level: str = Field(
        default="INFO",
        description="Уровень корневого логгера.",
    )
    log_file: str | None = Field(
        default=None,
        description="Путь к log-файлу. Пусто — логи в stderr.",
    )
    user_workspace_dir: str = Field(
        default="./workspaces/user",
        description=(
            "Корневая директория user-workspace'а: project-файлы, "
            "доступные tools (cat/grep/write)."
        ),
    )
    system_workspace_dir: str = Field(
        default="./workspaces/system",
        description=(
            "Корневая директория system-workspace'а: history, "
            "debug-артефакты, curl-трейсы LLM-вызовов."
        ),
    )
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    system_prompt_dir: str = Field(
        description="Корневая директория .md/.txt-файлов с system-prompt.",
    )
    model: str = Field(
        description="LLM-модель по умолчанию (напр. qwen3.5-35b). Обязательно.",
    )
