"""Конфиг chainlit-приложения: транспорт (chainlit-сервер) + runtime.

`ChainlitConfig` — единый конфиг chainlit-агента:
- chainlit-specific поля (host, port, auth_secret, app_root, …);
- nested `runtime: AgentRuntimeConfig` — общие настройки агента
  (логи, workspace, LLM, system prompt, model).

Источник: env `BOBA_CHAINLIT__*` / TOML `[chainlit]`. Оператор задаёт
runtime-поля плоско в той же секции — `BobaFlatSettings`-валидатор
распределит их по дереву.
"""

from __future__ import annotations

from pydantic import Field

from boba.agent import AgentRuntimeConfig
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["ChainlitConfig"]


class ChainlitConfig(BobaFlatSettings):
    """Параметры chainlit-приложения: server + runtime агента.

    Config-секция: `[chainlit]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        config_path="chainlit",
    )

    runtime: AgentRuntimeConfig = Field(default_factory=AgentRuntimeConfig)  # pyright: ignore[reportArgumentType]

    host: str = Field(
        default="127.0.0.1",
        description="Адрес, на котором слушает chainlit-сервер.",
    )
    port: int = Field(
        default=8501,
        description="Порт chainlit-сервера.",
    )
    url_prefix: str = Field(
        default="",
        description=(
            "URL-prefix для HTTP-роутинга под reverse-proxy"
        ),
    )
    auth_secret: str = Field(
        description=(
            "Секрет для подписи user-session cookie. Обязателен. "
            "Сгенерировать: `openssl rand -hex 32`"
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
