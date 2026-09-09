"""Корень профилей соединений: контракт, через который ядро работает с любым типом.

Конкретные типы (postgres, clickhouse, web, ...) живут в пакетах-владельцах и
наследуют этот класс; ядро, брокер и инструменты пользуются только его методами.

Ошибки:
ConnectionTypeError — наследник не покрыл обязательную часть контракта.
"""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from boba.kerberos import KerberosAuthBase, TicketAuth

__all__ = [
    "ClientIdentity",
    "ConnectionProfileBase",
    "ConnectionSource",
    "ConnectionTypeError",
]


class ConnectionTypeError(Exception):
    """Наследник профиля не покрыл обязательную часть контракта."""


class ClientIdentity(BaseModel):
    """Кто пришёл в систему этим вызовом: приложение, логин, инструмент.

    Хост знает только это. Как подписать сессию и куда положить подпись —
    дело профиля: у каждого сервера своё поле и свои пределы длины.
    """

    model_config = ConfigDict(frozen=True)

    application: str = Field(min_length=1)
    login: str = Field(min_length=1)
    tool: str = Field(min_length=1)


class ConnectionSource(BaseModel):
    """Строка соединений, из которой взят профиль: её id и имя. Хост
    подписывает ими профиль перед вызовом, чтобы тело инструмента знало, о
    каком соединении речь (снимок каталога ложится под этим id). Профиль из
    конфига или формы строки не имеет — нулевой id и пустое имя; в схему
    форм и в хранилище поле не попадает."""

    model_config = ConfigDict(frozen=True)

    id: UUID = UUID(int=0)
    name: str = ""

    @property
    def stored(self) -> bool:
        return self.id.int != 0


class ConnectionProfileBase(BaseModel):
    """Профиль соединения; наследник сужает kind до Literal своего значения."""

    kind: str = Field(description="Дискриминатор типа: значение задаёт наследник.")
    source: SkipJsonSchema[ConnectionSource] = Field(
        default_factory=ConnectionSource,
        description="Строка соединений, из которой взят профиль; ставит хост.",
    )
    description: str = Field(
        default="",
        description=(
            "Для чего это соединение: текст читает LLM в connection_list и "
            "connection_search, чтобы выбрать имя под задачу пользователя."
        ),
    )

    def kerberos_section(self) -> KerberosAuthBase | None:
        """Kerberos-часть профиля; None — тип аутентифицируется иначе."""
        return None

    def with_call_ticket(self, ticket: TicketAuth) -> Self:
        """Профиль с билетом вызова вместо своей kerberos-секции."""
        msg = (
            f"connection type {self.kind!r}: profile {type(self).__name__} carries "
            "a kerberos section but does not implement with_call_ticket"
        )
        raise ConnectionTypeError(msg)

    def service_name(self) -> str:
        """SPN сервиса соединения: кому выпускается билет вызова."""
        msg = (
            f"connection type {self.kind!r}: profile {type(self).__name__} carries "
            "a kerberos section but does not implement service_name"
        )
        raise ConnectionTypeError(msg)

    def trace(self) -> str:
        """Строка журнала: способ авторизации и под кем идём."""
        msg = (
            f"connection type {self.kind!r}: profile {type(self).__name__} "
            "does not implement trace"
        )
        raise ConnectionTypeError(msg)

    @classmethod
    def common_fields(cls) -> frozenset[str]:
        """Поля, общие всем профилям (kind, description, source): драйверу
        они не параметры соединения."""
        return frozenset(ConnectionProfileBase.model_fields)

    def labeled(self, client: ClientIdentity) -> Self:
        """Профиль, подписанный клиентом вызова.

        База не подписывает ничего: сервер, который такого поля не имеет,
        оставляет профиль как есть.
        """
        return self

    def identified(self, connection_id: UUID, name: str) -> Self:
        """Профиль, подписанный строкой соединений, из которой взят."""
        return self.model_copy(
            update={"source": ConnectionSource(id=connection_id, name=name)}
        )
