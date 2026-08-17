"""Общее у SQL-инструментов: whitelist профилей, лимиты, каталожный запрос.

Коннектор (postgres, clickhouse, ...) параметризует профиль своим типом
соединения; исполнение и показ живут в самих функциях инструментов.

Ошибки:
UnknownConnectionError — имя подключения вне whitelist'а; текст готов
    для пользователя.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Generic, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.toolkit.launcher import RowStream
from boba.toolkit.result import ResultTooLargeError, TableResult

__all__ = [
    "CatalogQuery",
    "ConnectionName",
    "RowBudget",
    "SqlErrorKind",
    "SqlProfiles",
    "UnknownConnectionError",
]


class SqlErrorKind(StrEnum):
    """Ожидаемые отказы SQL-инструментов; общие для всех коннекторов."""

    DATABASE_UNAVAILABLE = "database_unavailable"
    UNKNOWN_TARGET = "unknown_target"
    SQL_FAILED = "sql_failed"
    RESULT_TOO_LARGE = "result_too_large"


ConnectionName = Annotated[str, Field(min_length=1, description="Имя подключения")]
"""LLM-аргумент connection_name: выбор БД по whitelist'у профилей."""


class RowBudget:
    """Копилка строк выборки под лимитами max_rows и max_bytes.

    add возвращает False на потолке строк — выборка помечается усечённой;
    превышение max_bytes — ResultTooLargeError. Строка драйвера приводится
    к JSON-виду через RowStream.
    """

    def __init__(self, max_rows: int, max_bytes: int) -> None:
        self._max_rows = max_rows
        self._max_bytes = max_bytes
        self._rows: list[dict[str, Any]] = []
        self._size = 0
        self._truncated = False

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def size(self) -> int:
        """Съеденные байты: остаток нужен следующей команде того же запроса."""
        return self._size

    def add(self, row: Mapping[str, Any]) -> bool:
        """Добавить строку; False — потолок строк достигнут, хватит."""
        if len(self._rows) >= self._max_rows:
            self._truncated = True
            return False

        plain = RowStream.plain(row)

        self._size += len(RowStream.encode(plain))
        if self._size > self._max_bytes:
            raise ResultTooLargeError.bytes_limit(self._max_bytes)

        self._rows.append(plain)
        return True

    def table(self) -> TableResult:
        """Собранная выдача; усечение помечено в note."""
        note = None
        if self._truncated:
            note = f"truncated to max_rows ({self._max_rows})"

        return TableResult(rows=self._rows, note=note)


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

    def targets_table(self) -> TableResult:
        """Выдача list_targets: строка на каждое имя подключения."""
        rows: list[dict[str, Any]] = []
        for target in self.targets():
            rows.append({"connection_name": target})

        return TableResult(rows=rows)

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
