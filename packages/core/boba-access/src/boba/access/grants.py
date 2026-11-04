"""Субъекты и ресурсы grants; условия отбора доступных соединений.

Ошибки:
ValueError — неверный идентификатор ресурса.
ValidationError — неверный субъект гранта.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

__all__ = ["ConnectionFilter", "GrantResource", "GrantSubject", "GrantTarget"]


@dataclass(frozen=True, kw_only=True)
class ConnectionFilter:
    """Фильтр каталога соединений; SQL строит ConnectionStore в брокере."""

    kind: str = ""
    name: str = ""
    host: str = ""
    description: str = ""
    unique_only: bool = False

    @property
    def empty(self) -> bool:
        return self == ConnectionFilter()


class GrantSubject(StrEnum):
    """Виды субъектов доступа; ресурсы объявляют использующие grants репозитории."""

    USERS = "users"
    ROLES = "roles"


@dataclass(frozen=True)
class GrantResource:
    """Идентификатор ресурса, передаваемый репозиторием в запрос выдачи прав."""

    value: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]*", self.value) is None:
            raise ValueError(
                f"grant resource expects a lowercase identifier, got {self.value!r}"
            )


class GrantTarget(BaseModel):
    """Подтверждённый тип и UUID получателя гранта для репозитория ресурса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: GrantSubject
    id: UUID

    @classmethod
    def user(cls, user_id: UUID) -> GrantTarget:
        return cls(kind=GrantSubject.USERS, id=user_id)

    @classmethod
    def role(cls, role_id: UUID) -> GrantTarget:
        return cls(kind=GrantSubject.ROLES, id=role_id)
