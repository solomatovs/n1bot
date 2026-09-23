"""Способы аутентификации соединения Oracle: одно поле auth, дискриминант method.

Вариант несёт свои поля и сам переводит их в аргументы connect() драйвера.
Производные ключи задаёт вариант, а не администратор: wallet всегда включает
протокол tcps. Thin-режим python-oracledb умеет пароль и wallet (TLS и mTLS);
Kerberos и внешняя аутентификация живут в клиентских библиотеках Oracle, которых
в проекте нет.
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

from boba.toolkit.types import SecretRevealing

__all__ = [
    "NetProtocol",
    "OracleAuth",
    "OracleAuthBase",
    "OracleAuthMethod",
    "PasswordAuth",
    "WalletAuth",
]


class OracleAuthMethod(StrEnum):
    """Способы аутентификации thin-режима; значение — поле method секции auth."""

    PASSWORD = "password"  # noqa: S105 — это имя метода, не секрет
    WALLET = "wallet"


class NetProtocol(StrEnum):
    """Протокол listener'а: wallet требует tcps, пароль ходит по tcp."""

    TCP = "tcp"
    TCPS = "tcps"


class OracleAuthBase(BaseModel):
    """Общее у вариантов: запрет лишних полей, имя пользователя, аргументы драйвера."""

    model_config = ConfigDict(extra="forbid")

    REVEAL_SECRETS: ClassVar[str] = SecretRevealing.REVEAL_CONTEXT

    method: str = Field(description="Способ; вариант сужает его до литерала.")
    user: str = Field(min_length=1, description="Пользователь Oracle.")

    def connect(self) -> dict[str, Any]:
        """Аргументы connect() этого варианта; реализация обязана их перечислить."""
        raise NotImplementedError

    def trace(self) -> str:
        """Строка журнала: способ и пользователь, под которым входим."""
        return f"auth={self.method} user={self.user}"

    @classmethod
    def _reveal(cls, value: SecretStr, info: SerializationInfo) -> str | None:
        """Секрет уходит в дамп только с REVEAL_SECRETS: он нужен телу."""
        context = info.context
        if not isinstance(context, Mapping):
            return None

        if not context.get(cls.REVEAL_SECRETS):
            return None

        return value.get_secret_value()


class PasswordAuth(OracleAuthBase):
    """Пароль пользователя Oracle по tcp."""

    method: Literal["password"]

    password: SecretStr = Field(min_length=1, description="Пароль (секрет).")

    def connect(self) -> dict[str, Any]:
        return {
            "user": self.user,
            "password": self.password.get_secret_value(),
            "protocol": NetProtocol.TCP.value,
        }

    @field_serializer("password", when_used="json")
    def _dump_password(self, value: SecretStr, info: SerializationInfo) -> str | None:
        return self._reveal(value, info)


class WalletAuth(OracleAuthBase):
    """Пароль пользователя поверх TLS с wallet: каталог с ewallet.pem даёт
    корневой сертификат сервера и, для mTLS, сертификат клиента. Протокол
    всегда tcps, порт listener'а берётся из профиля."""

    method: Literal["wallet"]

    password: SecretStr = Field(min_length=1, description="Пароль (секрет).")
    wallet_location: str = Field(
        min_length=1,
        description="Каталог с ewallet.pem (thin-режим читает только PEM).",
    )
    wallet_password: SecretStr = Field(
        min_length=1, description="Пароль wallet, которым зашифрован ewallet.pem."
    )
    ssl_server_dn_match: bool = Field(
        description="Сверять DN сертификата сервера с именем сервиса."
    )

    def connect(self) -> dict[str, Any]:
        return {
            "user": self.user,
            "password": self.password.get_secret_value(),
            "wallet_location": self.wallet_location,
            "wallet_password": self.wallet_password.get_secret_value(),
            "ssl_server_dn_match": self.ssl_server_dn_match,
            "protocol": NetProtocol.TCPS.value,
        }

    @field_serializer("password", "wallet_password", when_used="json")
    def _dump_secret(self, value: SecretStr, info: SerializationInfo) -> str | None:
        return self._reveal(value, info)


OracleAuth: TypeAlias = Annotated[
    PasswordAuth | WalletAuth,
    Field(discriminator="method"),
]
"""Способ аутентификации соединения Oracle; различается полем method."""
