"""Confluence connection-config внутри секции `[tool.kb.confluence]`.

Connection-поля (`base_url`, `auth_method`, `auth_user`, `auth_token`,
`timeout_sec`, `ssl_verify`, `body_format`) — общие для всех
confluence-tools'ов. Каждый tool получает этот класс через FromConfig
и берёт нужные ему поля. Фабрики httpx.Auth / HttpTransport — в
`connection.py` (`ConfluenceConnection.make_auth`/`make_transport`).
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["ConfluenceConnectionConfig"]


class ConfluenceConnectionConfig(BobaFlatSettings):
    """Confluence connection-config: общий для всех confluence-tools'ов.

    Config-секция: `[tool.kb.confluence]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence",
    )

    base_url: str = Field(
        default="",
        description="URL Confluence. Обязателен.",
    )
    auth_token: str = Field(
        default="",
        description=(
            "PAT или пароль. Обязателен при auth_method=pat|basic; при "
            "auth_method=none игнорируется."
        ),
    )
    auth_method: Literal["pat", "basic", "none"] = Field(
        default="pat",
        description=(
            "`pat` — Bearer-токен; `basic` — login+password; "
            "`none` — anonymous (public Confluence без авторизации, "
            "например cwiki.apache.org)."
        ),
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

        Плагин-уровневое включение управляется `[tool.kb]`. Если
        оператор использует confluence-tools, секция `[tool.kb.confluence]`
        должна быть заполнена; иначе load-time валидация падает fail-fast.

        При `auth_method='none'` поля `auth_token` / `auth_user` не
        требуются — рассчитано на публичные Confluence (anonymous read).
        """
        if not self.base_url:
            msg = "kb.confluence.base_url обязателен"
            raise ValueError(msg)
        if self.auth_method != "none" and not self.auth_token:
            msg = (
                "kb.confluence.auth_token обязателен "
                "(или задайте auth_method='none' для anonymous-доступа)"
            )
            raise ValueError(msg)
        if self.auth_method == "basic" and not self.auth_user:
            msg = (
                "kb.confluence.auth_user обязателен при auth_method='basic'"
            )
            raise ValueError(msg)
        return self
