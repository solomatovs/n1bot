"""Переиспользуемый транспортный профиль: timeout/ssl/retry/auth, url даёт consumer.

Хост base_url может быть шаблоном `*.domain`: профиль покрывает поддомены
любой глубины, но не сам domain; перед запросом профиль привязывается к
конкретному хосту URL.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from typing import ClassVar, Literal, Self
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from boba.transport.http.auth import NegotiateAuth, NoneAuth, WebAuth

__all__ = ["HostPattern", "HttpProfile"]


class HostPattern(BaseModel):
    """Хост профиля: точное имя либо шаблон `*.domain`."""

    model_config = ConfigDict(frozen=True)

    WILDCARD: ClassVar[str] = "*."

    value: str

    @field_validator("value")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        """Имена хостов регистронезависимы: конфиг приводится к виду host_of."""
        return value.lower()

    @property
    def wildcard(self) -> bool:
        return self.value.startswith(self.WILDCARD)

    @property
    def suffix(self) -> str:
        """`.domain` для шаблона; пустая строка для точного хоста."""
        if not self.wildcard:
            return ""

        return self.value[len(self.WILDCARD) - 1 :]

    def matches(self, host: str) -> bool:
        lowered = host.lower()
        if not self.wildcard:
            return lowered == self.value

        if lowered == self.suffix[1:]:
            return False

        return lowered.endswith(self.suffix)

    @staticmethod
    def host_of(url: str) -> str:
        """Хост URL в нижнем регистре; пустая строка — хоста нет."""
        host = urlparse(url).hostname
        if host is None:
            return ""

        return host.lower()


class HttpProfile(BaseModel):
    """Транспортный профиль: timeout/ssl/retry + auth. Без url."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["web"] = Field(
        default="web",
        description="Дискриминатор соединения при хранении в базе.",
    )
    base_url: str | None = Field(
        default=None,
        description=(
            "Базовый URL для всех запросов с этим профилем (например, `https://api.example.com/v1/`)"
        ),
    )
    auth: WebAuth = Field(
        default=NoneAuth(method="none"),
        description=(
            "Auth-метод inline: `{ method = 'none'|'basic'|'bearer'|'digest', "
            "... }`. По умолчанию anonymous (`method='none'`)."
        ),
    )

    timeout_sec: float = Field(
        default=30.0,
        gt=0,
        description="HTTP-таймаут запроса (сек).",
    )
    ssl_verify: bool = Field(
        default=True,
        description="Проверять ли TLS-сертификат (false — для self-signed).",
    )
    retry_attempts: int = Field(
        default=1,
        ge=1,
        description=(
            "Сколько раз пытаться выполнить запрос. Ретраятся 5xx и "
            "transport-ошибки (timeout/connect); 4xx — нет. 1 — без retry."
        ),
    )
    retry_backoff_sec: float = Field(
        default=1.0,
        ge=0,
        description="Базовый линейный backoff между попытками (сек) × номер попытки.",
    )

    HTTP_SERVICE: ClassVar[str] = "HTTP"
    """Имя kerberos-сервиса веб-серверов; SPN вида HTTP/host."""

    @model_validator(mode="after")
    def _negotiate_needs_host(self) -> Self:
        if not isinstance(self.auth, NegotiateAuth):
            return self

        if self.base_url is None:
            msg = "web profile: negotiate auth needs base_url to name the SPN"
            raise ValueError(msg)

        if not self.host():
            msg = f"web profile: base_url {self.base_url!r} has no host for the SPN"
            raise ValueError(msg)

        return self

    def host(self) -> str:
        """Хост base_url в нижнем регистре (может быть шаблоном); пустая — нет."""
        if self.base_url is None:
            return ""

        return HostPattern.host_of(self.base_url)

    def covers(self, host: str) -> bool:
        """Попадает ли хост под base_url профиля (точный или шаблон)."""
        own = self.host()
        if not own:
            return False

        return HostPattern(value=own).matches(host)

    def bound_to(self, host: str) -> HttpProfile:
        """Профиль с конкретным хостом вместо шаблона в base_url."""
        if self.base_url is None:
            return self

        own = self.host()
        if not HostPattern(value=own).wildcard:
            return self

        parts = urlparse(self.base_url)
        netloc = host
        if parts.port is not None:
            netloc = f"{host}:{parts.port}"

        bound = parts._replace(netloc=netloc).geturl()
        return self.model_copy(update={"base_url": bound})

    def service_name(self) -> str:
        """SPN сервера в форме hostbased: HTTP@<service_host или host base_url>."""
        host = self.host()
        if isinstance(self.auth, NegotiateAuth) and self.auth.service_host:
            host = self.auth.service_host.lower()

        if not host:
            msg = "web profile: service name needs base_url with a host"
            raise ValueError(msg)

        if HostPattern(value=host).wildcard:
            msg = f"web profile: SPN needs a concrete host, got pattern {host!r}"
            raise ValueError(msg)

        return f"{self.HTTP_SERVICE}@{host}"

    def httpx_auth(self) -> httpx.Auth | None:
        """Аутентификатор httpx профиля; negotiate получает SPN и login-URL."""
        if isinstance(self.auth, NegotiateAuth):
            return self.auth.httpx_auth_at(self.service_name(), self.login_url())

        return self.auth.httpx_auth("")

    def login_url(self) -> str | None:
        """URL login-сервлета negotiate-профиля; None — сервлета нет."""
        if not isinstance(self.auth, NegotiateAuth):
            return None

        if self.auth.login_path is None:
            return None

        if self.base_url is None:
            return None

        return self.base_url.rstrip("/") + "/" + self.auth.login_path.lstrip("/")
