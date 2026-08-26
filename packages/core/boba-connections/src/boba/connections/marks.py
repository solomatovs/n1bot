"""Соединения субъекта на вызов: адресация секцией инструмента, отказы сборки,
метка клиента и строка журнала.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from boba.connections.clickhouse import ClickHouseConfig
from boba.connections.http import HttpProfile
from boba.connections.postgres import PostgresConfig
from boba.connections.profile import ConnectionKind, ConnectionProfile
from boba.connections.whitelist import ConnectionKeying

__all__ = [
    "ClientLabel",
    "ConnectionRefusal",
    "ConnectionTrace",
    "LoginMark",
    "UserConnectionsSpec",
]


class ConnectionRefusal(StrEnum):
    """Отказы сборки whitelist'а."""

    AMBIGUOUS = "ambiguous_connection"
    NO_DELEGATION = "no_delegated_credentials"
    HOST_NOT_ALLOWED = "host_not_allowed"


@dataclass(frozen=True)
class UserConnectionsSpec:
    """Как секция инструментов адресует соединения: вид и ключ вызова."""

    kind: ConnectionKind
    keying: ConnectionKeying


class ConnectionTrace:
    """Как соединение выглядит в журнале: способ авторизации и под кем идём.

    Пишется по профилю, который уже уехал бы в песочницу, поэтому у
    делегированных строк здесь виден выпущенный билет вызова, а не строка
    таблицы.
    """

    @staticmethod
    def of(profile: ConnectionProfile) -> str:
        if isinstance(profile, HttpProfile):
            return f"{profile.auth.trace()} url={profile.base_url}"

        return profile.auth.trace()


class LoginMark:
    """Метка входа в журнале: сама метка — ключ к тикету, целиком её не пишем."""

    KEEP: ClassVar[int] = 8

    @classmethod
    def of(cls, login: str) -> str:
        if len(login) <= cls.KEEP:
            return login

        return f"{login[: cls.KEEP]}…"


class ClientLabel(BaseModel):
    """Метка соединения для сервера: приложение, логин пользователя, инструмент.

    Уходит в application_name postgres и client_name clickhouse, поэтому режется
    до 63 байт — предела application_name.
    """

    model_config = ConfigDict(frozen=True)

    MAX_BYTES: ClassVar[int] = 63
    SEPARATOR: ClassVar[str] = ":"
    APPLICATION: ClassVar[str] = "boba"

    application: str
    login: str
    tool: str

    @classmethod
    def of(cls, login: str, tool: str) -> ClientLabel:
        return cls(application=cls.APPLICATION, login=login, tool=tool)

    def render(self) -> str:
        joined = self.SEPARATOR.join((self.application, self.login, self.tool))
        raw = joined.encode("utf-8")
        if len(raw) <= self.MAX_BYTES:
            return joined

        return raw[: self.MAX_BYTES].decode("utf-8", errors="ignore")

    def applied(self, profile: ConnectionProfile) -> ConnectionProfile:
        """Профиль с меткой в поле, которым сервер подписывает сессию."""
        if isinstance(profile, PostgresConfig):
            return profile.model_copy(update={"application_name": self.render()})

        if isinstance(profile, ClickHouseConfig):
            return profile.model_copy(update={"client_name": self.render()})

        return profile
