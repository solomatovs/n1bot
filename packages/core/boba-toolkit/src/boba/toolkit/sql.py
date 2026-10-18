"""Общее у SQL-инструментов: виды отказов, лимиты секции, запрос и его сборщик.

Профиль соединения инструмент получает параметром (маркер UserConnection),
окно выдачи — модуль window; здесь остались лимиты секции и база сборщика
запроса кусками, которую диалекты дополняют своей склейкой.

Ошибки:
ToolRefusalError — окно или ответ запроса больше потолков секции (SqlCeiling).
QueryBuildError — сборщик получил противоречивые куски: один параметр с двумя
    значениями, кусок с плейсхолдером, которому ничего не передано, или
    собранный запрос вместо значения.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Generic, TypeAlias, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.failure import ToolRefusalError
from boba.toolkit.result import SqlResult
from boba.toolkit.window import RowWindow

__all__ = [
    "AbstractQuery",
    "QueryBuildError",
    "QueryBuilder",
    "QueryParams",
    "QueryRowLimit",
    "SqlCeiling",
    "SqlLimits",
]


class SqlCeiling(StrEnum):
    """Виды отказа SQL-запроса сверх потолков секции: ErrorResult.error_kind."""

    ROWS = "sql_rows_ceiling"
    BYTES = "sql_bytes_ceiling"


QueryRowLimit: TypeAlias = Annotated[
    int,
    Field(
        ge=1,
        description=(
            "Сколько строк вернуть:\n"
            "   - не больше потолка строк, который администратор задал "
            "инструменту (max_rows)\n"
            "   - больше потолка — отказ с числом потолка; следующую страницу "
            "бери тем же вызовом со сдвинутым offset\n"
        ),
    ),
]
"""LLM-аргумент limit запросов *_query: высота окна под потолком секции."""


class SqlLimits(BaseModel):
    """Потолки одного ответа pg_query, ch_query и ora_query, которые задаёт
    администратор в секции плагина; секцию называет наследник в плагине.

    Модель просит окно сама, но больше max_rows строк не получит: такой
    вызов отказывается с числом потолка. Ответ длиннее max_bytes байт текста
    для модели тоже отказывается, а не обрезается молча.
    """

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str]
    """Секция конфига инструмента (tool.pg, tool.ch); подкласс обязан задать."""

    ENCODING: ClassVar[str] = "utf-8"
    """Кодировка, в которой меряется размер ответа для модели."""

    max_rows: int = Field(
        ge=1,
        description="Потолок строк одного ответа запроса: limit больше — отказ.",
    )
    max_bytes: int = Field(
        ge=1,
        description=(
            "Потолок размера одного ответа запроса в байтах текста, который "
            "получает модель: ответ больше — отказ с советом сузить выборку."
        ),
    )

    def window(self, offset: int, limit: int) -> RowWindow:
        """Окно выдачи вызова под потолком строк.

        Ошибки:
        ToolRefusalError — limit больше потолка max_rows.
        """
        if limit > self.max_rows:
            msg = (
                f"limit {limit} is above the ceiling of {self.max_rows} rows "
                f"per answer set for {self.SECTION}: ask for at most "
                f"{self.max_rows} rows and page on with offset"
            )
            raise ToolRefusalError(SqlCeiling.ROWS, msg)

        return RowWindow(offset=offset, limit=limit)

    def checked(self, result: SqlResult) -> SqlResult:
        """Ответ как есть, если он не длиннее потолка байт.

        Ошибки:
        ToolRefusalError — текст ответа для модели длиннее max_bytes байт.
        """
        size = len(result.llm_view().encode(self.ENCODING))
        if size > self.max_bytes:
            msg = (
                f"the answer is {size} bytes, above the ceiling of "
                f"{self.max_bytes} bytes per answer set for {self.SECTION}: "
                "select fewer columns, cut long values in the query or ask for "
                "fewer rows"
            )
            raise ToolRefusalError(SqlCeiling.BYTES, msg)

        return result


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
