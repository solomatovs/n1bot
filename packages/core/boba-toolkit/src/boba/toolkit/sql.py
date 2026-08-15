"""Общее у SQL-инструментов: whitelist профилей, лимиты, каталожный запрос.

Коннектор (postgres, clickhouse, ...) параметризует профиль своим типом
соединения; исполнение и показ живут в самих функциях инструментов.

Ошибки: UnknownConnectionError — имя подключения вне whitelist'а; текст готов
для пользователя.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Generic, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "CatalogQuery",
    "SqlProfiles",
    "UnknownConnectionError",
]

TConn = TypeVar("TConn", bound=BaseModel)
"""Профиль соединения коннектора: PostgresConfig, ClickHouseConfig, ..."""

TParams = TypeVar("TParams")
"""Стиль параметров драйвера: позиционный кортеж psycopg, именованный dict ch."""


class UnknownConnectionError(RuntimeError):
    """Имя подключения не значится в whitelist'е инструмента."""


class SqlProfiles(BaseModel, Generic[TConn]):
    """Whitelist профилей подключения и потолки выдачи SQL-инструмента."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str]
    """Секция конфига инструмента (tool.pg, tool.ch); подкласс обязан задать."""

    profiles: dict[str, TConn] = Field(
        default_factory=dict,
        description=(
            "dict[connection_name, профиль соединения ссылкой]. "
            "Ключ — значение tool-arg `connection_name`, по нему LLM выбирает БД."
        ),
    )
    max_rows: int = Field(
        default=100,
        ge=1,
        description="Ограничение по кол-ву строк.",
    )
    max_bytes: int = Field(
        default=1_000_000,
        ge=1,
        description=(
            "Hardlimit на суммарный размер собранного результата (символов). "
            "Превышение -> ошибка LLM «добавьте LIMIT»."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.profiles:
            section = type(self).SECTION
            msg = (
                f"{section}: no profiles configured. Reference one: "
                f'[{section}.profiles] <name> = "${{<db>.<name>}}".'
            )
            raise ValueError(msg)
        return self

    def targets(self) -> list[str]:
        return sorted(self.profiles)

    def resolve(self, connection_name: str) -> TConn:
        conn = self.profiles.get(connection_name)
        if conn is None:
            msg = (
                f"{type(self).SECTION}: connection_name {connection_name!r} is not "
                f"in the whitelist (allowed={self.targets()})"
            )
            raise UnknownConnectionError(msg)
        return conn


@dataclass(frozen=True)
class CatalogQuery(Generic[TParams]):
    """Каталожный запрос: текст плюс параметры в стиле драйвера."""

    text: str
    params: TParams
