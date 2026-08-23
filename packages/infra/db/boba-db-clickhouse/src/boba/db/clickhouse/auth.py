"""Способы аутентификации соединения clickhouse: одно поле auth, дискриминант method.

Вариант несёт свои поля и сам переводит их в аргументы клиента. Kerberos
уезжает не аргументами, а заголовком Negotiate на каждый запрос, поэтому
клиенту он отдаёт только имя пользователя, если оно вообще нужно.

Ошибки:
ClickHouseAuthError — вариант не может дать параметры клиента: делегирование
    разрешается приложением.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
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

from boba.krb import (
    DelegatedAuth,
    KerberosAuthBase,
    KerberosPasswordAuth,
    KeytabAuth,
    TicketAuth,
)
from boba.toolkit.types import SecretRevealing

__all__ = [
    "CertificateAuth",
    "ClickHouseAuth",
    "ClickHouseAuthError",
    "ClickHouseAuthMethod",
    "ClickHouseKerberos",
    "ClickHouseLibch",
    "NoPasswordAuth",
    "PasswordAuth",
]


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

    user: str = Field(min_length=1, description="Пользователь ClickHouse.")

    @abstractmethod
    def client(self) -> dict[str, Any]:
        """Аргументы конструктора клиента; реализация обязана их перечислить."""


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
                "delegated clickhouse auth is resolved by the application: "
                "the connection body expects a call ticket"
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
