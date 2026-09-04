"""HTTP-профиль соединения: base_url, timeout/ssl/retry и способ auth.

Хост base_url может быть шаблоном `*.domain`: профиль покрывает поддомены
любой глубины, но не сам domain; перед запросом профиль привязывается к
конкретному хосту URL. Аутентификатор httpx по профилю строит
boba.transport.http.HttpxAuth.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, ClassVar, Literal, Self
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from boba.connections.base import ConnectionProfileBase
from boba.kerberos import KerberosAuth, KerberosAuthBase, KerberosDump, TicketAuth

__all__ = [
    "BasicAuth",
    "BearerAuth",
    "DigestAuth",
    "HostPattern",
    "HttpConnection",
    "NegotiateAuth",
    "NoneAuth",
    "WebAuth",
]


class _AuthBase(BaseModel):
    """Общая база: запрет лишних полей; аутентификатор строит транспорт."""

    model_config = ConfigDict(extra="forbid")

    REVEAL_CONTEXT: ClassVar[str] = "reveal_secrets"
    """Ключ обязан совпадать с SecretRevealing.REVEAL_CONTEXT из toolkit."""

    method: str = Field(description="Способ; вариант сужает его до литерала.")

    def trace(self) -> str:
        """Строка журнала: способ, а у kerberos — ещё и чей билет."""
        return f"auth={self.method}"

    @classmethod
    def _reveal(cls, value: SecretStr, info: SerializationInfo) -> str | None:
        """Секрет уходит в дамп только с REVEAL_CONTEXT в контексте."""
        context = info.context
        if not isinstance(context, Mapping):
            return None

        if not context.get(cls.REVEAL_CONTEXT):
            return None

        return value.get_secret_value()


class NoneAuth(_AuthBase):
    """Anonymous-доступ. method='none' обязан быть прописан явно."""

    method: Literal["none"]


class BasicAuth(_AuthBase):
    """HTTP Basic: user и password."""

    method: Literal["basic"]
    user: str = Field(min_length=1)
    password: SecretStr = Field(min_length=1)

    @field_serializer("password", when_used="json")
    def _dump_password(self, value: SecretStr, info: SerializationInfo) -> str | None:
        return self._reveal(value, info)


class BearerAuth(_AuthBase):
    """Authorization: Bearer <token>."""

    method: Literal["bearer"]
    token: SecretStr = Field(min_length=1)

    @field_serializer("token", when_used="json")
    def _dump_token(self, value: SecretStr, info: SerializationInfo) -> str | None:
        return self._reveal(value, info)


class DigestAuth(_AuthBase):
    """HTTP Digest: user и password."""

    method: Literal["digest"]
    user: str = Field(min_length=1)
    password: SecretStr = Field(min_length=1)

    @field_serializer("password", when_used="json")
    def _dump_password(self, value: SecretStr, info: SerializationInfo) -> str | None:
        return self._reveal(value, info)


class NegotiateAuth(_AuthBase):
    """Kerberos/SPNEGO: Authorization: Negotiate по kerberos-секции профиля.

    В конфиге — keytab сервиса либо delegated (идёт сам пользователь);
    в песочницу уезжает билет одного вызова к HTTP@host профиля.
    """

    method: Literal["negotiate"]
    kerberos: KerberosAuth = Field(
        description="Креды: keytab, delegated или билет вызова.",
    )
    service_host: str | None = Field(
        default=None,
        description=(
            "Хост SPN (HTTP/<service_host>), если он отличается от хоста "
            "base_url: адрес по IP, reverse proxy. None — хост base_url."
        ),
    )
    login_path: str | None = Field(
        default=None,
        description=(
            "Путь login-сервлета, если сервис принимает Negotiate только там "
            "(Confluence Kerberos SSO: /plugins/servlet/kerberos/ntlm/login); "
            "сессионная cookie оттуда едет в остальные запросы. None — Negotiate "
            "на каждом запросе."
        ),
    )

    def trace(self) -> str:
        """Строка журнала: negotiate плюс описание kerberos-кредов."""
        return f"auth=negotiate {self.kerberos.trace()}"

    @field_serializer("kerberos", when_used="json")
    def _dump_kerberos(
        self, value: KerberosAuth, info: SerializationInfo
    ) -> dict[str, Any] | None:
        return KerberosDump.json(value, info.context, "web connection")


WebAuth = Annotated[
    NoneAuth | BasicAuth | BearerAuth | DigestAuth | NegotiateAuth,
    Field(discriminator="method"),
]
"""Discriminated union по method — точная диагностика ошибок валидации."""


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


class HttpConnection(ConnectionProfileBase):
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
            msg = (
                "web profile: negotiate auth needs base_url to name the SPN, "
                "got no base_url"
            )
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

    def bound_to(self, host: str) -> HttpConnection:
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
            msg = (
                f"web profile: SPN needs a host from base_url {self.base_url!r} "
                "or auth.service_host, both are empty"
            )
            raise ValueError(msg)

        if HostPattern(value=host).wildcard:
            msg = f"web profile: SPN needs a concrete host, got pattern {host!r}"
            raise ValueError(msg)

        return f"{self.HTTP_SERVICE}@{host}"

    def kerberos_section(self) -> KerberosAuthBase | None:
        if isinstance(self.auth, NegotiateAuth):
            return self.auth.kerberos

        return None

    def with_call_ticket(self, ticket: TicketAuth) -> HttpConnection:
        if not isinstance(self.auth, NegotiateAuth):
            return self

        auth = self.auth.model_copy(update={"kerberos": ticket})
        return self.model_copy(update={"auth": auth})

    def trace(self) -> str:
        return f"{self.auth.trace()} url={self.base_url}"

    def login_url(self) -> str | None:
        """URL login-сервлета negotiate-профиля; None — сервлета нет."""
        if not isinstance(self.auth, NegotiateAuth):
            return None

        if self.auth.login_path is None:
            return None

        if self.base_url is None:
            return None

        return self.base_url.rstrip("/") + "/" + self.auth.login_path.lstrip("/")
