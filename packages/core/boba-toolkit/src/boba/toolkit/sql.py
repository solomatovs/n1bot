"""Общее у SQL-инструментов: окно выдачи, лимиты секции, каталожный запрос.

Профиль соединения инструмент получает параметром (маркер UserConnection),
поэтому здесь остались только границы выдачи и сборка страницы результата.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.result import SqlStatement

__all__ = [
    "AbstractQuery",
    "RowLimit",
    "RowOffset",
    "RowPage",
    "RowWindow",
    "SqlErrorKind",
    "SqlLimits",
]


class SqlErrorKind(StrEnum):
    """Ожидаемые отказы SQL-инструментов; общие для всех коннекторов."""

    DATABASE_UNAVAILABLE = "database_unavailable"
    UNKNOWN_TARGET = "unknown_target"
    SQL_FAILED = "sql_failed"
    RESULT_TOO_LARGE = "result_too_large"


RowOffset = Annotated[
    int,
    Field(
        ge=0,
        description=(
            "Сколько строк пропустить: 0 — первая страница. Следующую бери "
            "тем же вызовом со значением next offset из note предыдущей."
        ),
    ),
]
"""LLM-аргумент offset: начало окна выдачи."""

RowLimit = Annotated[
    int,
    Field(ge=1, description="Сколько строк вернуть"),
]
"""LLM-аргумент max_rows: высота окна выдачи."""


class RowWindow(BaseModel):
    """Окно выдачи, которым правит LLM: что пропустить и сколько отдать.

    Модель листает сама: следующая страница — тот же вызов с offset,
    сдвинутым на max_rows. Потолка со стороны приложения нет, границы
    выдачи целиком в этих трёх числах.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    offset: int = Field(ge=0)
    limit: int | None = Field(default=None)

    def probe(self) -> int | None:
        """Сколько строк тянуть у драйвера: окно, а сверху разведочная строка.

        Лишняя строка не показывается: по ней видно, что данные не кончились.
        """
        if not self.limit:
            return None

        return self.offset + self.limit + 1


class RowPage:
    """Страница выборки по окну: пропуск, накопление и навигация в note.

    Останавливается мягко — по строкам или по символам, — потому что предел
    выдачи назначила сама модель и продолжение достаётся следующим вызовом.
    """

    def __init__(self, window: RowWindow) -> None:
        self._window = window
        self._rows: list[Mapping[str, Any]] = []
        self._skipped = 0
        # self._chars = 0
        self._more = False

    @property
    def more(self) -> bool:
        """Данные за окном остались: следующий вызов их достанет."""
        return self._more

    def add(self, row: Mapping[str, Any]) -> bool:
        """
        Добавляет строку в результат
        - False - если результат уже набрали и больше добавлять не будет
        - True - если результат еще не набран и можно дальше добавлять
        """
        if self._skipped < self._window.offset:
            # пропускаем столько строк, сколько передано в настройках
            self._skipped += 1
            return True

        if self._window.limit and len(self._rows) >= self._window.limit:
            # добиваем до указанного лимита строк
            self._more = True
            return False

        self._rows.append(row)

        return True

    def statement(self) -> SqlStatement:
        """Собранная страница; note объясняет модели, как листать дальше."""
        return SqlStatement(rows=self._rows, note=self._note())

    def _note(self) -> str:
        if not self._rows:
            return f"no rows at offset {self._window.offset}"

        first = self._window.offset + 1
        last = self._window.offset + len(self._rows)
        shown = f"rows {first}-{last}"

        if not self._more:
            return f"{shown}; end of result"

        # продолжать надо с непоказанной строки: набор мог оборваться
        # по символам раньше, чем набралось max_rows
        return f"{shown}; more rows available, next offset={last}"


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


@dataclass(frozen=True)
class AbstractQuery(Generic[TQuery, TParams]):
    """Каталожный запрос: текст плюс параметры в стиле драйвера."""

    text: TQuery
    params: TParams
