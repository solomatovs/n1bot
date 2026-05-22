"""`ConfluenceConnection` — переиспользуемая базовая модель Confluence-подключения.

`BaseModel` (не settings), без `config_path` — встраивается как nested-поле
в tool-конфиги, которым нужно ходить в Confluence (ingest, search, download).
Рекурсивный flatten в `BobaFlatSettings` поднимает поля на уровень корневой
TOML-секции tool'а.

Методы `make_auth()` / `make_transport()` — фабрики httpx.Auth / HttpTransport.
httpx-клиенты живут глубже в pipeline (`HttpTransport`, RequestSources);
здесь только factory-helpers.
"""

from __future__ import annotations

from typing import Literal, Self

import httpx
from pydantic import BaseModel, Field, model_validator

from boba.tool.kb.confluence.auth import PatAuth
from boba.transport.http import HttpTransport

__all__ = ["ConfluenceConnection"]


class ConfluenceConnection(BaseModel):
    """Confluence connection: URL + auth + http-params + body_format."""

    base_url: str = Field(
        default="",
        description="URL Confluence. Обязателен.",
    )
    auth_method: Literal["pat", "basic", "none"] = Field(
        default="pat",
        description=(
            "`pat` — Bearer-токен; `basic` — login+password; "
            "`none` — anonymous (public Confluence без авторизации, "
            "например cwiki.apache.org)."
        ),
    )
    auth_token: str = Field(
        default="",
        description=(
            "PAT или пароль. Обязателен при auth_method=pat|basic; при "
            "auth_method=none игнорируется."
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
        """Проверка консистентности — срабатывает при загрузке tool-конфига.

        При `auth_method='none'` поля `auth_token` / `auth_user` не
        требуются — рассчитано на публичные Confluence (anonymous read).
        """
        if not self.base_url:
            msg = "confluence: base_url обязателен"
            raise ValueError(msg)
        if self.auth_method != "none" and not self.auth_token:
            msg = (
                "confluence: auth_token обязателен "
                "(или задайте auth_method='none' для anonymous-доступа)"
            )
            raise ValueError(msg)
        if self.auth_method == "basic" and not self.auth_user:
            msg = "confluence: auth_user обязателен при auth_method='basic'"
            raise ValueError(msg)
        return self

    def make_auth(self) -> httpx.Auth | None:
        """`auth_method=none` → `None` (anonymous для публичного Confluence).

        Downstream (`HttpRequest.auth`, `httpx.Client(auth=...)`, RequestSources)
        принимают `httpx.Auth | None`, поэтому None прокидывается до конца pipeline.
        """
        match self.auth_method:
            case "none":
                return None
            case "basic":
                return httpx.BasicAuth(
                    username=self.auth_user,
                    password=self.auth_token,
                )
            case "pat":
                return PatAuth(token=self.auth_token)
            case _:
                msg = f"Unsupported auth_method: {self.auth_method!r}"
                raise ValueError(msg)

    def make_transport(self) -> HttpTransport:
        return HttpTransport(
            timeout_sec=self.timeout_sec,
            verify=self.ssl_verify,
        )
