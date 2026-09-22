"""OracleConfig: параметры thin-соединения python-oracledb и границы сессии."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import ConfigDict, Field

from boba.connections.base import ClientIdentity, ConnectionProfileBase
from boba.db.oracle.profile.auth import OracleAuth

__all__ = ["OracleConfig"]


class ProgramName:
    """Подпись сессии для Oracle: v$session.program вмещает 48 байт.

    Длиннее сервер обрезает сам, и в журнале остаётся кусок без имени
    инструмента, поэтому режем осознанно — по границе байтов utf-8.
    """

    MAX_BYTES: ClassVar[int] = 48
    SEPARATOR: ClassVar[str] = ":"

    @classmethod
    def of(cls, client: ClientIdentity) -> str:
        joined = cls.SEPARATOR.join((client.application, client.login, client.tool))
        raw = joined.encode("utf-8")
        if len(raw) <= cls.MAX_BYTES:
            return joined

        return raw[: cls.MAX_BYTES].decode("utf-8", errors="ignore")


class OracleConfig(ConnectionProfileBase):
    """Параметры oracledb.connect_async (thin-режим) + границы сессии.

    Соединение идёт по имени сервиса: одна PDB или сервис экземпляра, как dbname у
    postgres. call_timeout ограничивает каждый вызов к серверу — это единственный
    таймаут запроса, который есть у драйвера; arraysize задаёт, сколько строк курсор
    берёт за одну поездку к серверу.
    """

    model_config = ConfigDict(extra="ignore")

    # не аргументы connect(): границы вызова и выборки, способ авторизации
    NOT_CONNECT_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"call_timeout", "arraysize", "auth"}
    )

    kind: Literal["oracle"] = Field(
        default="oracle",
        description="Дискриминатор соединения при хранении в базе.",
    )

    host: str = Field(min_length=1, description="Хост или IP listener'а.")
    port: int = Field(gt=0, description="Порт listener'а, обычно 1521.")
    service: str = Field(
        min_length=1,
        description="Имя сервиса (service_name): PDB или сервис экземпляра.",
    )
    connect_timeout: int = Field(
        gt=0, description="Таймаут установки TCP-соединения (сек)."
    )
    call_timeout: int = Field(gt=0, description="Потолок одного вызова к серверу (мс).")
    arraysize: int = Field(
        gt=0, description="Строк за один fetch с сервера; столько же строк в пачке CSV."
    )
    program: str = Field(
        default="",
        description="Подпись сессии; её показывает v$session.program.",
    )

    auth: OracleAuth = Field(
        description="Как аутентифицируемся: password. Поля задаёт сам вариант."
    )

    def address_prefix(self) -> str:
        """Адрес для сообщений: host:port/service."""
        return f"{self.host}:{self.port}/{self.service}"

    def trace(self) -> str:
        return self.auth.trace()

    def labeled(self, client: ClientIdentity) -> OracleConfig:
        """Подпись сессии в program: её показывает v$session."""
        return self.model_copy(update={"program": ProgramName.of(client)})

    def connect_settings(self) -> dict[str, Any]:
        """kwargs oracledb.connect_async: адрес, таймаут соединения, подпись, креды."""
        settings: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "service_name": self.service,
            "tcp_connect_timeout": self.connect_timeout,
        }
        if self.program:
            settings["program"] = self.program

        settings.update(self.auth.connect())
        return settings
