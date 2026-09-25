"""Общее у SQL-инструментов: виды отказов, лимиты секции, запрос и его сборщик.

Профиль соединения инструмент получает параметром (маркер UserConnection),
окно выдачи — модуль window; здесь остались лимиты секции и база сборщика
запроса кусками, которую диалекты дополняют своей склейкой.

Ошибки:
QueryBuildError — сборщик получил противоречивые куски: один параметр с двумя
    значениями, кусок с плейсхолдером, которому ничего не передано, или
    собранный запрос вместо значения.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AbstractQuery",
    "QueryBuildError",
    "QueryBuilder",
    "QueryParams",
    "SqlErrorKind",
    "SqlLimits",
]


class SqlErrorKind(StrEnum):
    """Ожидаемые отказы SQL-инструментов; общие для всех коннекторов."""

    DATABASE_UNAVAILABLE = "database_unavailable"
    UNKNOWN_TARGET = "unknown_target"
    SQL_FAILED = "sql_failed"
    RESULT_TOO_LARGE = "result_too_large"


class SqlLimits(BaseModel):
    """Потолки выдачи SQL-инструмента; секцию задаёт наследник в плагине."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str]
    """Секция конфига инструмента (tool.pg, tool.ch); подкласс обязан задать."""

    limit: int = Field(
        default=100,
        ge=1,
        description=(
            "Ограничение по кол-ву строк для pg_query/ch_query: там окно "
            "выдачи пишется в самом SQL, а не аргументами вызова."
        ),
    )
    max_bytes: int = Field(
        default=1_000_000,
        ge=1,
        description=(
            "Hardlimit на суммарный размер выдачи pg_query/ch_query (символов). "
            "Превышение -> ошибка LLM «добавьте LIMIT». Инструменты со своим "
            "окном (list_tables, describe_table) сюда не смотрят."
        ),
    )


TQuery = TypeVar("TQuery")
TParams = TypeVar("TParams")
"""Стиль параметров драйвера: позиционный кортеж psycopg, именованный dict ch."""

QueryParams = dict[str, Any]
"""Именованные параметры драйвера: имя из текста запроса, значение из словаря."""


@dataclass(frozen=True)
class AbstractQuery(Generic[TQuery, TParams]):
    """Каталожный запрос: текст плюс параметры в стиле драйвера."""

    text: TQuery
    params: TParams


class QueryBuildError(Exception):
    """Сборщик запроса получил противоречивые куски."""


class QueryBuilder(ABC, Generic[TQuery]):
    """Интерфейс сборщика запроса кусками: вызывающий добавляет куски по ходу
    своей логики, значения уезжают параметрами драйвера, а build отдаёт
    запрос в стиле драйвера. Как принимается кусок и как в него попадают
    имена, решает реализация под свой драйвер — PgQueryBuilder,
    ChQueryBuilder, OraQueryBuilder.
    """

    @abstractmethod
    def build(self) -> AbstractQuery[TQuery, QueryParams | None]:
        """Текст и параметры для драйвера; без привязанных значений параметры None."""
