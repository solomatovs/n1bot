"""HTTP-профиль соединения: адрес сервера частями httpx.URL,
timeout/ssl/retry и способ auth.

Хост может быть шаблоном `*.domain`: профиль покрывает поддомены любой
глубины, но не сам domain; перед запросом профиль привязывается к
конкретному хосту URL. Аутентификатор httpx по профилю строит
boba.transport.http.HttpxAuth.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal
from urllib.parse import urlparse

import httpx
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
    "UrlPart",
    "UrlScheme",
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
            "Хост SPN (HTTP/<service_host>), если он отличается от host профиля: "
            "адрес по IP, reverse proxy. None — host профиля."
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


class UrlScheme(StrEnum):
    """Схема адреса web-профиля."""

    HTTP = "http"
    HTTPS = "https"


class UrlPart(StrEnum):
    """Части адреса, которые профиль передаёт в httpx.URL под этими же
    именами. Части, которые httpx принимает байтами, перечислены в
    raw_parts()."""

    SCHEME = "scheme"
    USERNAME = "username"
    PASSWORD = "password"  # noqa: S105 — имя аргумента httpx.URL, не секрет
    HOST = "host"
    PORT = "port"
    PATH = "path"
    QUERY = "query"
    FRAGMENT = "fragment"
    USERINFO = "userinfo"
    NETLOC = "netloc"
    RAW_PATH = "raw_path"

    @classmethod
    def raw_parts(cls) -> frozenset[UrlPart]:
        return frozenset({cls.QUERY, cls.USERINFO, cls.NETLOC, cls.RAW_PATH})


class HttpConnection(ConnectionProfileBase):
    """Транспортный профиль web-соединения: адрес сервера, timeout/ssl/retry
    и auth. Адрес хранится частями с именами аргументов httpx.URL; заданная
    часть подставляется в URL, незаданная — нет. URL собирают только
    root_url() и url_of() средствами httpx.URL; хост запроса обязан
    попадать под host профиля (точный или шаблон `*.domain`).
    """

    model_config = ConfigDict(extra="ignore")

    kind: Literal["web"] = Field(
        default="web",
        description="Дискриминатор соединения при хранении в базе.",
    )
    scheme: UrlScheme = Field(
        default=UrlScheme.HTTPS,
        description="Схема запросов: http или https.",
    )
    username: str | None = Field(
        default=None,
        description="Имя пользователя в адресе (userinfo URL).",
    )
    password: SecretStr | None = Field(
        default=None,
        description="Пароль в адресе (userinfo URL).",
    )
    host: str | None = Field(
        default=None,
        description="Хост сервера или шаблон `*.example.com` для поддоменов.",
    )
    port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        description="Порт сервера; пусто — порт схемы.",
    )
    path: str | None = Field(
        default=None,
        description="Корневой путь сервиса, например `/wiki`; пусто — корень сервера.",
    )
    query: str | None = Field(
        default=None,
        description="Query-строка корня без `?`, например `tenant=a`.",
    )
    fragment: str | None = Field(
        default=None,
        description="Фрагмент адреса без `#`.",
    )
    userinfo: SecretStr | None = Field(
        default=None,
        description=(
            "Готовая часть `user:password` "
            "адреса; перекрывает username/password."
        ),
    )
    netloc: str | None = Field(
        default=None,
        description="Готовая часть `host:port` адреса; перекрывает host/port.",
    )
    raw_path: str | None = Field(
        default=None,
        description="Готовый путь с query в percent-кодировке; перекрывает path/query.",
    )
    auth: WebAuth = Field(
        default=NoneAuth(method="none"),
        description=(
            "Auth-метод inline: `{ method = 'none'|'basic'|'bearer'|'digest'"
            "|'negotiate', ... }`. По умолчанию anonymous (`method='none'`)."
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

    RAW_ENCODING: ClassVar[str] = "ascii"
    """Кодировка байтовых частей httpx.URL: они уже percent-кодированы."""

    @field_validator("host")
    @classmethod
    def _lowercase_host(cls, value: str | None) -> str | None:
        """Имена хостов регистронезависимы; шаблон и SPN сравниваются в нижнем."""
        if value is None:
            return None

        return value.lower()

    @field_validator("path")
    @classmethod
    def _normalize_path(cls, value: str | None) -> str | None:
        """Путь с ведущим слэшем и без хвостового; корень сервера — пустая строка."""
        if value is None:
            return None

        stripped = value.strip("/")
        if not stripped:
            return ""

        return posixpath.join("/", stripped)

    @model_validator(mode="after")
    def _address_names_a_host(self) -> HttpConnection:
        """Части обязаны собираться в адрес с хостом: без него ни запрос,
        ни проверка покрытия невозможны."""
        try:
            root = self.root_url()
        except (httpx.InvalidURL, TypeError, UnicodeEncodeError) as exc:
            msg = f"web profile: address parts do not form a URL: {exc}"
            raise ValueError(msg) from exc

        if not root.host:
            msg = "web profile: address needs a host, set host or netloc"
            raise ValueError(msg)

        return self

    def _url_parts(self) -> Iterator[tuple[UrlPart, object]]:
        """Заданные части адреса в виде аргументов httpx.URL."""
        yield UrlPart.SCHEME, self.scheme.value
        yield from self._authority_parts()
        yield from self._resource_parts()

    def _authority_parts(self) -> Iterator[tuple[UrlPart, object]]:
        """Учётные данные и сервер: userinfo/netloc перекрывают раздельные части."""
        if self.username is not None:
            yield UrlPart.USERNAME, self.username

        if self.password is not None:
            yield UrlPart.PASSWORD, self.password.get_secret_value()

        if self.userinfo is not None:
            yield UrlPart.USERINFO, self._raw(self.userinfo.get_secret_value())

        if self.host is not None:
            yield UrlPart.HOST, self.host

        if self.port is not None:
            yield UrlPart.PORT, self.port

        if self.netloc is not None:
            yield UrlPart.NETLOC, self._raw(self.netloc)

    def _resource_parts(self) -> Iterator[tuple[UrlPart, object]]:
        """Путь, query и фрагмент: raw_path перекрывает path и query."""
        if self.path is not None:
            yield UrlPart.PATH, self.path

        if self.query is not None:
            yield UrlPart.QUERY, self._raw(self.query)

        if self.raw_path is not None:
            yield UrlPart.RAW_PATH, self._raw(self.raw_path)

        if self.fragment is not None:
            yield UrlPart.FRAGMENT, self.fragment

    @classmethod
    def _raw(cls, value: str) -> bytes:
        return value.encode(cls.RAW_ENCODING)

    def root_url(self) -> httpx.URL:
        """Адрес сервиса из заданных частей; порт по умолчанию схемы httpx
        опускает, учётные данные адреса остаются в URL."""
        kwargs: dict[str, Any] = {}
        for part, value in self._url_parts():
            kwargs[part.value] = value

        return httpx.URL(**kwargs)

    def public_url(self) -> httpx.URL:
        """Адрес сервиса без учётных данных: для журнала и сообщений."""
        return self.root_url().copy_with(userinfo=b"")

    def url_of(self, relative: str) -> httpx.URL:
        """URL запроса под корнем сервиса: относительный путь дописывается
        так же, как httpx.Client(base_url=...) дописывает запрос."""
        root = self.root_url()
        folder = root.copy_with(path=posixpath.join(root.path, ""))
        return folder.join(relative.lstrip("/"))

    def address_host(self) -> str:
        """Хост собранного адреса в нижнем регистре: точный или шаблон."""
        return self.root_url().host

    def covers(self, host: str) -> bool:
        """Попадает ли хост под host профиля (точный или шаблон)."""
        return HostPattern(value=self.address_host()).matches(host)

    def bound_to(self, host: str) -> HttpConnection:
        """Профиль с конкретным хостом вместо шаблона; порт сохраняется."""
        root = self.root_url()
        if not HostPattern(value=root.host).wildcard:
            return self

        return self.model_copy(
            update={"host": host.lower(), "port": root.port, "netloc": None}
        )

    def service_name(self) -> str:
        """SPN сервера в форме hostbased: HTTP@<service_host или host профиля>."""
        host = self.address_host()
        if isinstance(self.auth, NegotiateAuth) and self.auth.service_host:
            host = self.auth.service_host.lower()

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
        return f"{self.auth.trace()} url={self.public_url()}"

    def login_url(self) -> str | None:
        """URL login-сервлета negotiate-профиля; None — сервлета нет."""
        if not isinstance(self.auth, NegotiateAuth):
            return None

        if self.auth.login_path is None:
            return None

        return str(self.url_of(self.auth.login_path))
