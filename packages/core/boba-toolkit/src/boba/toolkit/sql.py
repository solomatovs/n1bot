"""Общий слой SQL-инструментов: профили, контракт payload'а, исполнение.

Коннектор (postgres, clickhouse, ...) параметризует этот слой своим типом
профиля соединения и стилем параметров драйвера, а сам оставляет себе только
нативный SQL, вызов payload'а и фасады @tool.

Ошибки: SqlQueryError — запрос не выполнен; UnknownConnectionError — имя
подключения вне whitelist'а. Текст обеих готов для пользователя.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, Protocol, Self, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    model_validator,
)

from boba.toolkit.launcher import ChunkSink, LauncherError, RowCollector
from boba.toolkit.result import ErrorResult

__all__ = [
    "SqlCall",
    "SqlCaller",
    "SqlErrors",
    "SqlExecutor",
    "SqlProfiles",
    "SqlQueryError",
    "SqlQueryRequest",
    "SqlQueryTrailer",
    "SqlResult",
    "SqlRows",
    "UnknownConnectionError",
]

TConn = TypeVar("TConn", bound=BaseModel)
"""Профиль соединения коннектора: PostgresConfig, ClickHouseConfig, ..."""

TParams = TypeVar("TParams")
"""Стиль параметров драйвера: позиционный кортеж psycopg, именованный dict ch."""

TConn_contra = TypeVar("TConn_contra", bound=BaseModel, contravariant=True)
"""То же, что TConn, но для протокола: тип стоит только во входных позициях."""

TParams_contra = TypeVar("TParams_contra", contravariant=True)
"""То же, что TParams, но для протокола."""


class SqlQueryError(RuntimeError):
    """Запрос не выполнен: песочница или драйвер вернули отказ."""


class UnknownConnectionError(SqlQueryError):
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


class SqlCall(BaseModel, Generic[TConn]):
    """Общая часть запроса payload'а: куда подключаться и что выполнять."""

    model_config = ConfigDict(extra="forbid")

    REVEAL_SECRETS: ClassVar[str]
    """Ключ контекста, по которому профиль раскрывает секреты; задаёт подкласс."""

    op: str = Field(min_length=1)
    connection: TConn = Field(
        description="Профиль подключения целиком: payload подключается по нему сам.",
    )
    sql: str = Field(min_length=1)

    @field_serializer("connection", when_used="json")
    def _dump_connection(self, value: TConn) -> dict[str, Any]:
        """stdin песочницы — доверенный канал: только здесь пароль едет раскрытым."""
        return value.model_dump(
            mode="json",
            context={type(self).REVEAL_SECRETS: True},
        )


class SqlQueryRequest(SqlCall[TConn], Generic[TConn, TParams]):
    """Запрос строк с лимитом; стиль параметров задаёт коннектор."""

    params: TParams = Field(
        description="Параметры запроса в стиле драйвера; пусто — без них.",
    )
    row_limit: int = Field(ge=1)


class SqlQueryTrailer(BaseModel):
    """Итог запроса: строки ушли кадрами, здесь — что с ними стало."""

    model_config = ConfigDict(extra="forbid")

    truncated: bool
    returns_rows: bool
    """False — набора строк не было (DML/DDL); тогда смысл несёт rowcount."""
    rowcount: int | None
    """Число затронутых строк; None там, где драйвер счётчика не даёт."""
    status: str | None
    """Нативный статус выполнения, напр. statusmessage psycopg — 'DELETE 5'."""


@dataclass(frozen=True)
class SqlResult:
    """Результат запроса: строки либо счётчик затронутых, плюс флаг усечения."""

    rows: list[dict[str, Any]]
    truncated: bool
    returns_rows: bool
    rowcount: int | None
    status: str | None


class SqlCaller(Protocol, Generic[TConn_contra, TParams_contra]):
    """Один вызов payload'а на запрос: соединение и SQL уходят в песочницу."""

    @abstractmethod
    def query(
        self,
        *,
        connection: TConn_contra,
        sql: str,
        params: TParams_contra,
        row_limit: int,
        sink: ChunkSink,
    ) -> SqlQueryTrailer:
        """Выполнить запрос: строки кадрами в sink, итог — трейлером."""
        ...


class SqlExecutor(Generic[TConn, TParams]):
    """Исполняет SQL в песочнице; каждый вызов — отдельный процесс и соединение."""

    def __init__(
        self,
        *,
        cfg: SqlProfiles[TConn],
        caller: SqlCaller[TConn, TParams],
    ) -> None:
        self._cfg = cfg
        self._caller = caller

    @property
    def max_rows_cap(self) -> int:
        return self._cfg.max_rows

    @property
    def max_bytes(self) -> int:
        return self._cfg.max_bytes

    def allowed_targets(self) -> list[str]:
        return self._cfg.targets()

    def connection_of(self, connection_name: str) -> TConn:
        """Профиль цели как есть; коннектор вправе довернуть режим сессии."""
        return self._cfg.resolve(connection_name)

    async def execute(
        self,
        query: str,
        *,
        connection_name: str,
        row_limit: int,
        params: TParams,
    ) -> SqlResult:
        """Запрос с лимитом строк; синхронный вызов песочницы уходит в поток."""
        effective_limit = min(max(row_limit, 1), self._cfg.max_rows)
        collector = RowCollector(
            max_chars=self._cfg.max_bytes,
            limit_rows=effective_limit,
        )
        connection = self.connection_of(connection_name)
        try:
            trailer = await asyncio.to_thread(
                self._caller.query,
                connection=connection,
                sql=query,
                params=params,
                row_limit=effective_limit,
                sink=collector,
            )
        except LauncherError as e:
            msg = f"SQL execute failed (connection_name={connection_name!r}): {e}"
            raise SqlQueryError(msg) from e

        return SqlResult(
            rows=collector.rows(),
            truncated=trailer.truncated,
            returns_rows=trailer.returns_rows,
            rowcount=trailer.rowcount,
            status=trailer.status,
        )


class SqlErrors:
    """Отказы SQL-инструмента в виде ErrorResult с текстом для LLM."""

    def __init__(self, *, max_rows: int, max_bytes: int) -> None:
        self._max_rows = max_rows
        self._max_bytes = max_bytes

    def failed(self, error: SqlQueryError) -> ErrorResult:
        return ErrorResult(message=str(error), error_kind="sql_failed")

    def unknown_target(self, error: UnknownConnectionError) -> ErrorResult:
        return ErrorResult(message=str(error), error_kind="unknown_target")

    def too_large(self) -> ErrorResult:
        msg = (
            f"результат превысил лимит {self._max_bytes} символов; "
            f"добавьте LIMIT в запрос"
        )
        return ErrorResult(message=msg, error_kind="result_too_large")

    def too_many_rows(self) -> ErrorResult:
        msg = (
            f"запрос вернул больше {self._max_rows} строк; "
            f"добавьте LIMIT в запрос"
        )
        return ErrorResult(message=msg, error_kind="too_many_rows")

    def note(self, truncated: bool) -> str | None:
        if not truncated:
            return None
        return f"список усечён до max_rows ({self._max_rows})"


class SqlRows:
    """Значения драйвера -> JSON-совместимые: JSON другого не умеет."""

    @classmethod
    def of_mapping(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        """Строка dict-курсора (psycopg row_factory=dict_row)."""
        out: dict[str, Any] = {}
        for name, value in row.items():
            out[name] = cls.scalar(value)
        return out

    @classmethod
    def of_columns(
        cls, names: Sequence[str], row: Sequence[Any]
    ) -> dict[str, Any]:
        """Строка-кортеж с отдельным списком имён колонок (блоки ClickHouse)."""
        out: dict[str, Any] = {}
        for index, name in enumerate(names):
            out[name] = cls.scalar(row[index])
        return out

    @classmethod
    def scalar(cls, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (list, tuple, set)):
            items: list[Any] = []
            for item in value:
                items.append(cls.scalar(item))
            return items
        if isinstance(value, Mapping):
            mapping: dict[str, Any] = {}
            for key, item in value.items():
                mapping[str(key)] = cls.scalar(item)
            return mapping
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", errors="replace")
        return str(value)
