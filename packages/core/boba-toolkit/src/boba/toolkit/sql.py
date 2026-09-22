"""Общее у SQL-инструментов: окно выдачи, лимиты секции, запрос и его сборщик.

Профиль соединения инструмент получает параметром (маркер UserConnection),
поэтому здесь остались границы выдачи, сборка страницы результата и база
сборщика запроса кусками, которую диалекты дополняют своей склейкой.

Ошибки:
QueryBuildError — сборщик получил противоречивые куски: один параметр с двумя
    значениями или кусок с плейсхолдером, которому ничего не передано.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Generic, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.result import SqlStatement

__all__ = [
    "AbstractQuery",
    "QueryBuildError",
    "QueryBuilder",
    "QueryParams",
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
    """Базовый сборщик запроса кусками, которые вызывающий добавляет по ходу
    своей логики: кусок с условием попадает в запрос только при истинном
    условии, значения уезжают параметрами драйвера, а имена (идентификаторы,
    готовые фрагменты) подставляются в текст куска. Диалект — PgQueryBuilder,
    OraQueryBuilder, ChQueryBuilder — решает, что считать именем, как
    подставить его в кусок и как склеить куски.
    """

    def __init__(self, **names: Any) -> None:
        """Имена, переданные конструктору, подставляются в каждый кусок."""
        for name, value in names.items():
            if self.takes_name(value):
                continue

            msg = (
                f"query builder: standing name {name!r} expects a name for the "
                f"text, got {value!r}"
            )
            raise QueryBuildError(msg)

        self._names: dict[str, Any] = dict(names)
        self._pieces: list[TQuery] = []
        self._params: QueryParams = {}

    @abstractmethod
    def takes_name(self, value: object) -> bool:
        """Значение — имя для подстановки в текст, а не параметр драйвера."""

    @abstractmethod
    def render_piece(self, text: str, names: Mapping[str, Any]) -> TQuery:
        """Кусок с подставленными именами."""

    @abstractmethod
    def join_pieces(self, pieces: Sequence[TQuery]) -> TQuery:
        """Куски в один запрос."""

    def add(self, text: str, /, **bind: Any) -> Self:
        names: dict[str, Any] = dict(self._names)
        for name, value in bind.items():
            if isinstance(value, AbstractQuery):
                msg = (
                    f"query builder: {name!r} got a built query; pass its .text as a "
                    "name or bind its values"
                )
                raise QueryBuildError(msg)

            if self.takes_name(value):
                names[name] = value
                continue

            if name in self._params and self._params[name] != value:
                msg = (
                    f"query builder: parameter {name!r} bound twice with different "
                    f"values: {self._params[name]!r} and {value!r}"
                )
                raise QueryBuildError(msg)

            self._params[name] = value

        self._pieces.append(self.render_piece(text, names))

        return self

    def when(self, condition: bool, text: str, /, **bind: Any) -> Self:
        if not condition:
            return self

        return self.add(text, **bind)

    def read(self, path: Path, /, **bind: Any) -> Self:
        """Кусок из файла пакета: текст читается целиком и добавляется как add."""
        text = path.read_text(encoding="utf-8")

        return self.add(text, **bind)

    def build(self) -> AbstractQuery[TQuery, QueryParams | None]:
        """Текст и параметры для драйвера; без привязанных значений параметры None,
        и драйвер не трогает текст (проценты и скобки в нём остаются как есть)."""
        params: QueryParams | None = None
        if self._params:
            params = dict(self._params)

        return AbstractQuery(text=self.join_pieces(self._pieces), params=params)
