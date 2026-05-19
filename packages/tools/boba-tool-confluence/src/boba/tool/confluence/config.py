"""Конфиг плагина confluence.

Connection-поля на корне (`base_url`, `auth_method`, `auth_user`,
`auth_token`, `timeout_sec`, `body_format`) — общие для всех 5 tools'ов.
Каждый tool получает целиком `ConfluencePluginConfig` через FromConfig
и забирает оттуда поля, нужные конкретно ему.

Плагин-уровневое включение/allowlist (`enable`, `tools`) — забота
framework'а (`AgentBuilder.discover_plugins`); конфиг плагина их не
объявляет (`extra="ignore"`).

`ConfluenceConnection.make_auth(cfg)` / `make_transport(cfg)` принимают
любой объект, удовлетворяющий `ConfluenceConnectionConfig`-Protocol —
этот класс ему удовлетворяет (duck typing).
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["ConfluencePluginConfig"]


class ConfluencePluginConfig(BobaFlatSettings):
    """Confluence multi-tool plugin: 5 tools, общий connection."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.confluence",
    )

    base_url: str = Field(
        default="",
        description="URL Confluence. Обязателен.",
    )
    auth_token: str = Field(
        default="",
        description="PAT или пароль. Обязателен.",
    )
    auth_method: Literal["pat", "basic"] = Field(
        default="pat",
        description="`pat` — Bearer-токен; `basic` — login+password.",
    )
    auth_user: str = Field(
        default="",
        description=(
            "Логин для basic-auth; обязателен при auth_method=basic. Для PAT — пусто."
        ),
    )
    timeout_sec: float = Field(
        default=30.0,
        description="HTTP-таймаут (сек).",
    )
    ssl_verify: bool = Field(
        default=False,
        description="Проверять ли TLS-сертификат.",
    )
    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description=(
            "`view` — clean HTML (рекомендуется); `export_view` — с макросами; "
            "`storage` — raw storage XML."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Проверка консистентности — срабатывает при загрузке конфига.

        Под discover-flow плагин загружается только если оператор явно
        включил его (`[tool.confluence] enable=true`). Значит при load-time
        connection-поля должны быть валидно заполнены.
        """
        if not self.base_url:
            msg = "confluence.base_url обязателен"
            raise ValueError(msg)
        if not self.auth_token:
            msg = "confluence.auth_token обязателен"
            raise ValueError(msg)
        if self.auth_method == "basic" and not self.auth_user:
            msg = "confluence.auth_user обязателен при auth_method='basic'"
            raise ValueError(msg)
        return self
