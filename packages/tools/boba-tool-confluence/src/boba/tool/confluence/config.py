"""Конфиг плагина confluence (v2).

Connection-поля на корне (`base_url`, `auth_method`, `auth_user`,
`auth_token`, `timeout_sec`, `body_format`) — общие для всех 5 tools'ов.
Каждый tool получает целиком `ConfluencePluginConfig` через FromConfig
и забирает оттуда поля, нужные конкретно ему.

`ConfluenceConnection.make_auth(cfg)` / `make_transport(cfg)` принимают
любой объект, удовлетворяющий `ConfluenceConnectionConfig`-Protocol —
этот класс ему удовлетворяет (duck typing).
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, StringList

__all__ = ["ConfluencePluginConfig"]


class ConfluencePluginConfig(BobaFlatSettings):
    """Confluence multi-tool plugin: 5 tools, общий connection."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        config_path="tool.confluence",
    )

    enable: bool = Field(
        default=False,
        description="Регистрировать ли confluence-tools в DI/каталоге LLM.",
    )
    tools: StringList | None = Field(
        default=None,
        description=(
            "Allowlist tool-имён: None — все включены; иначе только "
            "перечисленные. Имена: 'confluence_search', "
            "'confluence_page_outline', 'confluence_page_section', "
            "'confluence_page_download', 'confluence_page_download_markdown'."
        ),
    )
    base_url: str = Field(
        default="",
        description="URL Confluence (обязателен при enable=True).",
    )
    auth_token: str = Field(
        default="",
        description="PAT или пароль (обязателен при enable=True).",
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
    def _check_invariants(self) -> Self:
        if not self.enable:
            return self
        if not self.base_url:
            msg = "base_url обязателен при enable=True"
            raise ValueError(msg)
        if not self.auth_token:
            msg = "auth_token обязателен при enable=True"
            raise ValueError(msg)
        if self.auth_method == "basic" and not self.auth_user:
            msg = "auth_user обязателен при auth_method='basic'"
            raise ValueError(msg)
        return self
