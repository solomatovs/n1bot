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
from typing import Annotated, Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.launcher import RowStream
from boba.toolkit.result import ResultTooLargeError, TableResult

__all__ = [
    "CatalogQuery",
    "ConnectionName",
    "MaxChars",
    "MaxRows",
    "RowBudget",
    "RowOffset",
    "RowPage",
    "RowWindow",
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

MaxRows = Annotated[
    int,
    Field(ge=1, description="Сколько строк вернуть на этой странице."),
]
"""LLM-аргумент max_rows: высота окна выдачи."""

MaxChars = Annotated[
    int,
    Field(
        ge=1,
        description=(
            "Потолок символов страницы: набор строк обрывается на нём, "
            "остаток достаётся следующим offset."
        ),
    ),
]
"""LLM-аргумент max_chars: вес окна выдачи."""


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


class RowWindow(BaseModel):
    """Окно выдачи, которым правит LLM: что пропустить и сколько отдать.

    Модель листает сама: следующая страница — тот же вызов с offset,
    сдвинутым на max_rows. Потолка со стороны приложения нет, границы
    выдачи целиком в этих трёх числах.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    offset: int = Field(ge=0)
    max_rows: int = Field(ge=1)
    max_chars: int = Field(ge=1)

    def probe(self) -> int:
        """Сколько строк тянуть у драйвера: окно, а сверху разведочная строка.

        Лишняя строка не показывается: по ней видно, что данные не кончились.
        """
        return self.offset + self.max_rows + 1


class RowPage:
    """Страница выборки по окну: пропуск, накопление и навигация в note.

    Останавливается мягко — по строкам или по символам, — потому что предел
    выдачи назначила сама модель и продолжение достаётся следующим вызовом.
    """

    def __init__(self, window: RowWindow) -> None:
        self._window = window
        self._rows: list[dict[str, Any]] = []
        self._skipped = 0
        self._chars = 0
        self._more = False

    @property
    def more(self) -> bool:
        """Данные за окном остались: следующий вызов их достанет."""
        return self._more

    def add(self, row: Mapping[str, Any]) -> bool:
        """Взять строку; False — окно набрано, читать дальше незачем."""
        if self._skipped < self._window.offset:
            self._skipped += 1
            return True

        if len(self._rows) >= self._window.max_rows:
            self._more = True
            return False

        plain = RowStream.plain(row)
        chars = len(RowStream.encode(plain))

        if self._rows and self._chars + chars > self._window.max_chars:
            self._more = True
            return False

        self._chars += chars
        self._rows.append(plain)

        return True

    def table(self) -> TableResult:
        """Собранная страница; note объясняет модели, как листать дальше."""
        return TableResult(rows=self._rows, note=self._note())

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
            "dict[connection_name, профиль соединения]. Ключ — значение tool-arg "
            "`connection_name`, по нему LLM выбирает БД. Приложение собирает "
            "whitelist из соединений пользователя на каждый вызов."
        ),
    )
    names: list[str] = Field(
        default_factory=list,
        description=(
            "Имена соединений, доступных пользователю, без профилей: видны в "
            "connection_list, а профиль приезжает только у выбранного соединения."
        ),
    )
    max_rows: int = Field(
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

    def targets(self) -> list[str]:
        known = set(self.names)
        known.update(self.profiles)
        return sorted(known)

    def targets_table(self) -> TableResult:
        """Выдача connection_list: строка на каждое имя подключения."""
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
