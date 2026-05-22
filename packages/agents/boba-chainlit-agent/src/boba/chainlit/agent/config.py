"""Конфиги chainlit-приложения.

`ChainlitConfig` — параметры самого chainlit-сервера (host/port, auth,
UI-overrides). Источник: env `BOBA_CHAINLIT__*` / TOML `[chainlit]`.

`AppConfig` (+ `AppCoreConfig`) — кросс-слойный конфиг агента
(workspaces, openai, system prompt, логирование). Источник:
env `BOBA_AGENT__*` / TOML `[agent]`. Идентичен boba-cli-agent.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boba.agent.workspace_fs import WorkspaceLayout
from boba.provider.openai import OpenAIConfig
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["AppConfig", "AppCoreConfig", "ChainlitConfig"]


class ChainlitConfig(BobaFlatSettings):
    """Параметры chainlit-приложения: server, runtime-root, LLM-модель, auth.

    Config-секция: `[chainlit]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        config_path="chainlit",
    )

    model: str = Field(
        description="Конкретная LLM-модель для всех запросов из UI.",
    )
    host: str = Field(
        default="127.0.0.1",
        description="Адрес, на котором слушает chainlit-сервер.",
    )
    port: int = Field(
        default=8501,
        description="Порт chainlit-сервера.",
    )
    root_path: str = Field(
        default="",
        description="HTTP root path под reverse-proxy. Пусто — chainlit на корне.",
    )
    auth_secret: str = Field(
        description=(
            "Секрет для подписи user-session cookie. Обязателен. "
            "Сгенерировать: `openssl rand -hex 32`. Задаётся через "
            "`BOBA_CHAINLIT__AUTH_SECRET` или TOML `[chainlit] auth_secret`."
        ),
        min_length=1,
    )
    headless: bool = Field(
        default=True,
        description="true — не пытаться открыть браузер при старте.",
    )
    app_root: str = Field(
        default="./local/chainlit",
        description=(
            "Директория chainlit runtime-state: .chainlit/config.toml, "
            "chainlit.md, public/, translations/. Не лежит в исходниках — "
            "вынесена в local/ (gitignored). Bridge ставит её в CHAINLIT_APP_ROOT."
        ),
    )
    chat_session_pool_capacity: int = Field(
        default=32,
        description="Сколько ChatSession держать в RAM одновременно (LRU eviction).",
    )


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
    """Конфиг агента: core/workspaces/openai/prompts.

    Config-секция: `[agent]`.
    """

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
