"""Способы аутентификации соединения clickhouse: одно поле auth, дискриминант method.

Вариант несёт свои поля и сам переводит их в аргументы клиента. Kerberos
уезжает не аргументами, а заголовком Negotiate на каждый запрос, поэтому
клиенту он отдаёт только имя пользователя, если оно вообще нужно.

Ошибки:
ClickHouseAuthError — вариант не может дать параметры клиента: делегирование
    разрешается приложением.
ClickHouseError — kerberos-варианту не выданы кредитивы на время работы
    клиента, или заголовок Negotiate для запроса не собрался: нет билета или
    он не подходит службе.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializationInfo,
    field_serializer,
)

from boba.db.clickhouse.errors import ClickHouseError
from boba.kerberos import (
    DelegatedAuth,
    KerberosAuthBase,
    KerberosError,
    KerberosPasswordAuth,
    KeytabAuth,
    TicketAuth,
)
from boba.krb import ClientCredentials, KerberosCredentials, SpnegoNegotiate
from boba.toolkit.types import SecretRevealing

__all__ = [
    "CertificateAuth",
    "ClickHouseAuth",
    "ClickHouseAuthError",
    "ClickHouseAuthMethod",
    "ClickHouseAuthSession",
    "ClickHouseKerberos",
    "ClickHouseLibch",
    "NoPasswordAuth",
    "PasswordAuth",
    "SpnegoHeaders",
]


class SpnegoHeaders(dict[str, str]):
    """Заголовки клиента со свежим Negotiate на каждый HTTP-запрос.

    ClickHouse отвергает повторно присланный AP-REQ как replay, поэтому один
    заголовок на весь клиент не работает: со второго запроса сервер перестаёт
    видеть принципала. clickhouse-connect снимает copy() заголовков перед
    каждым запросом — токен и выпускается здесь, в copy().

    Токен строится по кредам из окружения процесса (KRB5CCNAME/KRB5_CONFIG),
    поэтому клиент живёт внутри KerberosCredentials.applied_async().
    """

    HEADER: ClassVar[str] = SpnegoNegotiate.HEADER

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    @property
    def service_name(self) -> str:
        return self._service_name

    def copy(self) -> dict[str, str]:
        headers = dict(self)
        headers[self.HEADER] = self._negotiate()
        return headers

    def _negotiate(self) -> str:
        try:
            return SpnegoNegotiate.header(self._service_name)
        except KerberosError as exc:
            msg = (
                f"building Negotiate header for clickhouse service "
                f"{self._service_name} failed: {exc}"
            )
            raise ClickHouseError(msg) from exc


class ClickHouseAuthSession:
    """Окружение авторизации на время работы клиента. У kerberos-варианта
    держит кредитивы процесса и отдаёт SpnegoHeaders с именем службы, чтобы
    клиент выпускал Negotiate на каждый запрос; у остальных вариантов не
    делает ничего и отдаёт None. Строит ClickHouseConfig.auth_session(),
    зовёт PayloadClickHouse."""

    def __init__(
        self, auth: ClickHouseAuth, service_name: str | None, where: str
    ) -> None:
        self._auth = auth
        self._where = where
        self._credentials: KerberosCredentials | None = None
        self._headers: SpnegoHeaders | None = None
        if not isinstance(auth, KerberosAuthBase):
            return

        if service_name is None:
            raise ClickHouseAuthError(
                f"clickhouse {where}: auth {auth.method} expects a kerberos "
                "service name for the Negotiate header, got none"
            )

        try:
            self._credentials = ClientCredentials.of(auth)
        except KerberosError as exc:
            raise ClickHouseError(
                f"clickhouse {where}: auth {auth.method} gives no client "
                f"credentials: {exc}"
            ) from exc

        self._headers = SpnegoHeaders(service_name)

    def describe(self) -> str:
        """Кем входим: принципал kerberos либо способ авторизации."""
        if self._credentials is None:
            return self._auth.method

        return f"{self._auth.method} {self._credentials.principal}"

    @asynccontextmanager
    async def applied(self) -> AsyncGenerator[SpnegoHeaders | None, None]:
        if self._credentials is None or self._headers is None:
            yield None
            return

        try:
            async with self._credentials.applied_async():
                yield self._headers
        except KerberosError as exc:
            msg = (
                f"clickhouse {self._where}: kerberos credentials of "
                f"{self._credentials.principal} for service "
                f"{self._headers.service_name} failed: {type(exc).__name__}: {exc}"
            )
            raise ClickHouseError(msg) from exc


class ClickHouseAuthError(Exception):
    """Из варианта авторизации нельзя собрать параметры клиента."""


class ClickHouseAuthMethod(StrEnum):
    """Не-kerberos способы; kerberos-варианты приходят из boba-krb."""

    NO_PASSWORD = "no_password"  # noqa: S105 — это имя метода, не секрет
    PASSWORD = "password"  # noqa: S105 — это имя метода, не секрет
    CERTIFICATE = "certificate"


class ClickHouseAuthBase(BaseModel, ABC):
    """Общее у не-kerberos вариантов: запрет лишних полей и имя пользователя."""

    model_config = ConfigDict(extra="forbid")

    REVEAL_SECRETS: ClassVar[str] = SecretRevealing.REVEAL_CONTEXT

    method: str = Field(description="Способ; вариант сужает его до литерала.")
    user: str = Field(min_length=1, description="Пользователь ClickHouse.")

    @abstractmethod
    def client(self) -> dict[str, Any]:
        """Аргументы конструктора клиента; реализация обязана их перечислить."""

    def trace(self) -> str:
        """Строка журнала: способ и пользователь, под которым входим."""
        return f"auth={self.method} user={self.user}"


class NoPasswordAuth(ClickHouseAuthBase):
    """Пользователь без пароля: IDENTIFIED WITH no_password."""

    method: Literal["no_password"]

    def client(self) -> dict[str, Any]:
        return {"username": self.user}


class PasswordAuth(ClickHouseAuthBase):
    """Пароль пользователя ClickHouse."""

    method: Literal["password"]

    password: SecretStr = Field(min_length=1, description="Пароль (секрет).")

    def client(self) -> dict[str, Any]:
        return {"username": self.user, "password": self.password.get_secret_value()}

    @field_serializer("password", when_used="json")
    def _dump_password(self, value: SecretStr, info: SerializationInfo) -> str | None:
        """Пароль уходит в дамп только с REVEAL_SECRETS: он нужен телу."""
        context = info.context
        if not isinstance(context, Mapping):
            return None

        if not context.get(self.REVEAL_SECRETS):
            return None

        return value.get_secret_value()


class CertificateAuth(ClickHouseAuthBase):
    """Клиентский сертификат: сервер сверяет CN с пользователем."""

    method: Literal["certificate"]

    client_cert: str = Field(min_length=1, description="Файл сертификата клиента.")
    client_cert_key: str = Field(min_length=1, description="Файл ключа клиента.")

    def client(self) -> dict[str, Any]:
        return {
            "username": self.user,
            "client_cert": self.client_cert,
            "client_cert_key": self.client_cert_key,
        }


class ClickHouseKerberos:
    """Kerberos-вариант глазами клиента: имя пользователя даёт сам билет."""

    DEFAULT_SERVICE: ClassVar[str] = "HTTP"
    """krbsrvname по умолчанию: HTTP-интерфейс ClickHouse принимает SPNEGO."""

    @classmethod
    def client(cls, auth: KerberosAuthBase) -> dict[str, Any]:
        if isinstance(auth, DelegatedAuth):
            msg = (
                f"delegated clickhouse auth ({auth.method}) is resolved by the "
                "application: the connection body expects a call ticket, "
                "not the delegated section"
            )
            raise ClickHouseAuthError(msg)

        # username серверу не шлём: он берёт принципал из заголовка Negotiate
        return {}

    @classmethod
    def service_of(cls, auth: KerberosAuthBase) -> str:
        """Имя kerberos-сервиса: своё у строки либо стандартное для ClickHouse."""
        if auth.service is None:
            return cls.DEFAULT_SERVICE

        if isinstance(auth, TicketAuth):
            name, _, _ = auth.service_name().partition("@")
            return name

        return auth.service


class ClickHouseLibch:
    """Аргументы клиента для варианта авторизации, каким бы он ни был."""

    @staticmethod
    def of(auth: ClickHouseAuth) -> dict[str, Any]:
        if isinstance(auth, KerberosAuthBase):
            return ClickHouseKerberos.client(auth)

        return auth.client()


ClickHouseAuth: TypeAlias = Annotated[
    NoPasswordAuth
    | PasswordAuth
    | CertificateAuth
    | KeytabAuth
    | KerberosPasswordAuth
    | DelegatedAuth
    | TicketAuth,
    Field(discriminator="method"),
]
"""Способ аутентификации соединения clickhouse; различается полем method."""
