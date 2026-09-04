"""Корень профилей соединений: контракт, через который ядро работает с любым типом.

Конкретные типы (postgres, clickhouse, web, ...) живут в пакетах-владельцах и
наследуют этот класс; ядро, брокер и инструменты пользуются только его методами.

Ошибки:
ConnectionTypeError — наследник не покрыл обязательную часть контракта.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from boba.kerberos import KerberosAuthBase, TicketAuth

__all__ = ["ClientIdentity", "ConnectionProfileBase", "ConnectionTypeError"]


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


class ConnectionProfileBase(BaseModel):
    """Профиль соединения; наследник сужает kind до Literal своего значения."""

    kind: str = Field(description="Дискриминатор типа: значение задаёт наследник.")
    description: str = Field(
        default="",
        description=(
            "Для чего это соединение: текст читает LLM в connection_list, "
            "чтобы выбрать имя под задачу пользователя."
        ),
    )

    def kerberos_section(self) -> KerberosAuthBase | None:
        """Kerberos-часть профиля; None — тип аутентифицируется иначе."""
        return None

    def with_call_ticket(self, ticket: TicketAuth) -> Self:
        """Профиль с билетом вызова вместо своей kerberos-секции."""
        raise ConnectionTypeError(
            f"{self.kind}: profile carries a kerberos section "
            "but does not implement with_call_ticket"
        )

    def service_name(self) -> str:
        """SPN сервиса соединения: кому выпускается билет вызова."""
        raise ConnectionTypeError(
            f"{self.kind}: profile carries a kerberos section "
            "but does not implement service_name"
        )

    def trace(self) -> str:
        """Строка журнала: способ авторизации и под кем идём."""
        raise ConnectionTypeError(f"{self.kind}: profile does not implement trace")

    def labeled(self, client: ClientIdentity) -> Self:
        """Профиль, подписанный клиентом вызова.

        База не подписывает ничего: сервер, который такого поля не имеет,
        оставляет профиль как есть.
        """
        return self
