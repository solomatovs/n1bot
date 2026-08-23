"""Способы аутентификации соединения postgres: одно поле auth, дискриминант method.

Вариант несёт ровно свои поля и сам переводит их в ключи libpq. Производные
ключи (gssencmode, require_auth, krbsrvname, user у kerberos) задаёт вариант,
а не администратор: сервер не сможет предложить метод слабее выбранного.

Ошибки:
PostgresAuthError — вариант не может дать параметры соединения: делегирование
    разрешается приложением, у билета нет имени сервиса.
"""

from __future__ import annotations

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
    "PasswordAuth",
    "PostgresAuth",
    "PostgresAuthError",
    "PostgresAuthMethod",
    "TrustAuth",
]


class PostgresAuthError(Exception):
    """Из варианта авторизации нельзя собрать параметры соединения."""


class PostgresAuthMethod(StrEnum):
    """Не-kerberos способы; kerberos-варианты приходят из boba-krb."""

    TRUST = "trust"
    PASSWORD = "password"  # noqa: S105 — это имя метода, не секрет
    CERTIFICATE = "certificate"


class GssMode(StrEnum):
    """gssencmode соединения; выбирается вариантом, а не конфигом."""

    OFF = "disable"
    REQUIRED = "require"


class RequireAuth(StrEnum):
    """require_auth соединения: сервер не может предложить метод слабее."""

    SCRAM = "scram-sha-256"
    CERT = "cert"
    GSS = "gss"


class PostgresAuthBase(BaseModel):
    """Общее у не-kerberos вариантов: запрет лишних полей и роль сервера."""

    model_config = ConfigDict(extra="forbid")

    REVEAL_SECRETS: ClassVar[str] = SecretRevealing.REVEAL_CONTEXT

    user: str = Field(min_length=1, description="Роль postgres, под которой входим.")

    def libpq(self) -> dict[str, Any]:
        """Ключи connect() этого варианта; реализация обязана их перечислить."""
        raise NotImplementedError

    @classmethod
    def _reveal(cls, value: SecretStr, info: SerializationInfo) -> str | None:
        """Секрет уходит в дамп только с REVEAL_SECRETS: он нужен телу."""
        context = info.context
        if not isinstance(context, Mapping):
            return None

        if not context.get(cls.REVEAL_SECRETS):
            return None

        return value.get_secret_value()


class TrustAuth(PostgresAuthBase):
    """Сервер доверяет клиенту без секрета: trust или peer в pg_hba."""

    method: Literal["trust"]

    def libpq(self) -> dict[str, Any]:
        return {"user": self.user, "gssencmode": GssMode.OFF.value}


class PasswordAuth(PostgresAuthBase):
    """Пароль роли: scram-sha-256."""

    method: Literal["password"]

    password: SecretStr = Field(min_length=1, description="Пароль роли (секрет).")

    def libpq(self) -> dict[str, Any]:
        return {
            "user": self.user,
            "password": self.password.get_secret_value(),
            "gssencmode": GssMode.OFF.value,
            "require_auth": RequireAuth.SCRAM.value,
        }

    @field_serializer("password", when_used="json")
    def _dump_password(self, value: SecretStr, info: SerializationInfo) -> str | None:
        return self._reveal(value, info)


class CertificateAuth(PostgresAuthBase):
    """Клиентский сертификат: cert в pg_hba, роль сверяется с CN."""

    method: Literal["certificate"]

    sslcert: str = Field(min_length=1, description="Клиентский сертификат.")
    sslkey: str = Field(min_length=1, description="Клиентский приватный ключ.")
    sslkey_password: SecretStr | None = Field(
        default=None, description="Пароль приватного ключа (секрет)."
    )

    def libpq(self) -> dict[str, Any]:
        conn: dict[str, Any] = {
            "user": self.user,
            "sslcert": self.sslcert,
            "sslkey": self.sslkey,
            "gssencmode": GssMode.OFF.value,
            "require_auth": RequireAuth.CERT.value,
        }

        if self.sslkey_password is not None:
            conn["sslpassword"] = self.sslkey_password.get_secret_value()

        return conn

    @field_serializer("sslkey_password", when_used="json")
    def _dump_key_password(
        self, value: SecretStr | None, info: SerializationInfo
    ) -> str | None:
        if value is None:
            return None

        return self._reveal(value, info)


class PostgresKerberos:
    """Перевод kerberos-варианта в ключи libpq: имя сервиса и роль из принципала."""

    DEFAULT_SERVICE: ClassVar[str] = "postgres"
    """krbsrvname по умолчанию: так называется сервис postgres в KDC."""

    @classmethod
    def libpq(cls, auth: KerberosAuthBase) -> dict[str, Any]:
        if isinstance(auth, DelegatedAuth):
            msg = (
                "delegated postgres auth is resolved by the application: "
                "the connection body expects a call ticket"
            )
            raise PostgresAuthError(msg)

        return {
            "user": cls.role_of(auth.source_principal()),
            "gssencmode": GssMode.REQUIRED.value,
            "require_auth": RequireAuth.GSS.value,
            "krbsrvname": cls.service_of(auth),
        }

    @classmethod
    def service_of(cls, auth: KerberosAuthBase) -> str:
        """Имя kerberos-сервиса: своё у строки либо стандартное для postgres."""
        if auth.service is None:
            return cls.DEFAULT_SERVICE

        if isinstance(auth, TicketAuth):
            name, _, _ = auth.service_name().partition("@")
            return name

        return auth.service

    @staticmethod
    def role_of(principal: str) -> str:
        """Роль сервера: короткое имя принципала, как её видит include_realm=0."""
        name, _, _ = principal.partition("@")
        return name


class PostgresLibpq:
    """Ключи connect() варианта авторизации, каким бы он ни был."""

    @staticmethod
    def of(auth: PostgresAuth) -> dict[str, Any]:
        if isinstance(auth, KerberosAuthBase):
            return PostgresKerberos.libpq(auth)

        return auth.libpq()


PostgresAuth: TypeAlias = Annotated[
    TrustAuth
    | PasswordAuth
    | CertificateAuth
    | KeytabAuth
    | KerberosPasswordAuth
    | DelegatedAuth
    | TicketAuth,
    Field(discriminator="method"),
]
"""Способ аутентификации соединения postgres; различается полем method."""
