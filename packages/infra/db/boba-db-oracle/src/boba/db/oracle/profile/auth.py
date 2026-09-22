"""Способы аутентификации соединения Oracle: одно поле auth, дискриминант method.

Вариант несёт свои поля и сам переводит их в аргументы connect() драйвера.
Thin-режим python-oracledb умеет только пароль: Kerberos и внешняя аутентификация
живут в клиентских библиотеках Oracle, которых в проекте нет.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializationInfo,
    field_serializer,
)

from boba.toolkit.types import SecretRevealing

__all__ = ["OracleAuth", "OracleAuthBase", "PasswordAuth"]


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


class PasswordAuth(OracleAuthBase):
    """Пароль пользователя Oracle."""

    method: Literal["password"]

    password: SecretStr = Field(min_length=1, description="Пароль (секрет).")

    def connect(self) -> dict[str, Any]:
        return {"user": self.user, "password": self.password.get_secret_value()}

    @field_serializer("password", when_used="json")
    def _dump_password(self, value: SecretStr, info: SerializationInfo) -> str | None:
        """Пароль уходит в дамп только с REVEAL_SECRETS: он нужен телу."""
        context = info.context
        if not isinstance(context, Mapping):
            return None

        if not context.get(self.REVEAL_SECRETS):
            return None

        return value.get_secret_value()


OracleAuth: TypeAlias = PasswordAuth
"""Способ аутентификации соединения Oracle; различается полем method."""
