"""Общий слой SQL-инструментов: профили, контракт payload'а, исполнение.

Коннектор (postgres, clickhouse, ...) параметризует этот слой своим типом
профиля соединения и стилем параметров драйвера, а сам оставляет себе только
нативный SQL, вызов payload'а и фасады @tool.

Ошибки:
SqlQueryError — запрос не выполнен.
UnknownConnectionError — имя подключения вне whitelist'а.
CollectorCapacityError/CollectorRowLimitError (boba.toolkit.launcher) — выдача
    переросла max_bytes/max_rows потребителя. Текст всех готов для
    пользователя.
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

from boba.toolkit.channels import ChannelSink
from boba.toolkit.launcher import (
    CollectorCapacityError,
    CollectorRowLimitError,
    LauncherError,
    RowCollector,
)
from boba.toolkit.result import ErrorResult
from boba.toolkit.secrets import SecretDump

__all__ = [
    "ConnectionCall",
    "ConnectionProfile",
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


class ConnectionProfile(BaseModel):
    """Базовый профиль соединения: несёт ключ раскрытия секретов в дампе."""

    REVEAL_SECRETS: ClassVar[str] = SecretDump.REVEAL
    """Ключ контекста сериализации, по которому профиль раскрывает секреты."""


TConn = TypeVar("TConn", bound=ConnectionProfile)
"""Профиль соединения коннектора: PostgresConfig, ClickHouseConfig, ..."""

TParams = TypeVar("TParams")
"""Стиль параметров драйвера: позиционный кортеж psycopg, именованный dict ch."""

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


class ConnectionCall(BaseModel, Generic[TConn]):
    """Общая часть запроса payload'а к БД: операция и профиль подключения."""

    model_config = ConfigDict(extra="forbid")

    OP: ClassVar[str]
    """Имя операции payload'а; подкласс обязан задать, в поле op едет оно же."""

    op: str = Field(min_length=1)
    connection: TConn = Field(
        description="Профиль подключения целиком: payload подключается по нему сам.",
    )

    @field_serializer("connection", when_used="json")
    def _dump_connection(self, value: TConn) -> dict[str, Any]:
        """tool_args — доверенный канал: только здесь пароль едет раскрытым."""
        return SecretDump.of(value)


class SqlCall(ConnectionCall[TConn], Generic[TConn]):
    """Запрос с нативным SQL: куда подключаться и что выполнять."""

    sql: str = Field(min_length=1)


class SqlQueryRequest(SqlCall[TConn], Generic[TConn, TParams]):
    """Запрос строк с лимитом; стиль параметров задаёт коннектор."""

    params: TParams = Field(
        description="Параметры запроса в стиле драйвера; пусто — без них.",
    )
    row_limit: int = Field(ge=1)


class SqlQueryTrailer(BaseModel):
    """Итог запроса: строки ушли потоком данных, здесь — что с ними стало."""

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


class SqlCaller(Protocol, Generic[TParams_contra]):
    """Один вызов payload'а на запрос: имя подключения и SQL уходят в песочницу.

    Профиль соединения по имени разрешает обогатитель узла на стороне реестра
    стадий — в спецификацию графа секреты не попадают.
    """

    @abstractmethod
    def query(
        self,
        *,
        connection_name: str,
        sql: str,
        params: TParams_contra,
        sink: ChannelSink,
    ) -> SqlQueryTrailer:
        """Выполнить запрос: NDJSON-байты строк в sink, итог — трейлером.

        Исполнитель закрывает sink по концу данных до возврата трейлера.
        """
        ...


class SqlExecutor(Generic[TConn, TParams]):
    """Исполняет SQL в песочнице; каждый вызов — отдельный процесс и соединение."""

    def __init__(
        self,
        *,
        cfg: SqlProfiles[TConn],
        caller: SqlCaller[TParams],
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
        params: TParams,
    ) -> SqlResult:
        """Запрос с потолком строк из конфига; вызов песочницы уходит в поток."""
        # whitelist проверяется здесь: имя вне списка — отказ фасада, не стадии
        self.connection_of(connection_name)

        collector = RowCollector(
            max_chars=self._cfg.max_bytes,
            limit_rows=self._cfg.max_rows,
        )

        try:
            trailer = await asyncio.to_thread(
                self._caller.query,
                connection_name=connection_name,
                sql=query,
                params=params,
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

    CATCHES: ClassVar[
        tuple[
            type[SqlQueryError],
            type[CollectorCapacityError],
            type[CollectorRowLimitError],
        ]
    ] = (SqlQueryError, CollectorCapacityError, CollectorRowLimitError)
    """Ошибки SQL-тула, которые pack превращает в ErrorResult."""

    def __init__(self, *, max_rows: int, max_bytes: int) -> None:
        self._max_rows = max_rows
        self._max_bytes = max_bytes

    def pack(
        self,
        error: SqlQueryError | CollectorCapacityError | CollectorRowLimitError,
    ) -> ErrorResult:
        """Единая карта отказов фасада: тип ошибки -> ErrorResult."""
        if isinstance(error, UnknownConnectionError):
            return self.unknown_target(error)
        if isinstance(error, SqlQueryError):
            return self.failed(error)
        if isinstance(error, CollectorCapacityError):
            return self.too_large()
        return self.too_many_rows()

    def failed(self, error: SqlQueryError) -> ErrorResult:
        return ErrorResult(message=str(error), error_kind="sql_failed")

    def unknown_target(self, error: UnknownConnectionError) -> ErrorResult:
        return ErrorResult(message=str(error), error_kind="unknown_target")

    def too_large(self) -> ErrorResult:
        msg = (
            f"result exceeded the limit of {self._max_bytes} characters; "
            f"add LIMIT to the query"
        )
        return ErrorResult(message=msg, error_kind="result_too_large")

    def too_many_rows(self) -> ErrorResult:
        msg = (
            f"query returned more than {self._max_rows} rows; "
            f"add LIMIT to the query"
        )
        return ErrorResult(message=msg, error_kind="too_many_rows")

    def note(self, truncated: bool) -> str | None:
        if not truncated:
            return None
        return f"list truncated to max_rows ({self._max_rows})"


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
    def of_columns(cls, names: Sequence[str], row: Sequence[Any]) -> dict[str, Any]:
        """Строка-кортеж с отдельным списком имён колонок (блоки ClickHouse)."""
        out: dict[str, Any] = {}
        for index, name in enumerate(names):
            out[name] = cls.scalar(row[index])
        return out

    @classmethod
    def scalar(cls, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (list, tuple)):
            items: list[Any] = []
            for item in value:
                items.append(cls.scalar(item))
            return items
        if isinstance(value, (set, frozenset)):
            # порядок set недетерминирован — сортируем по строковому образу
            unordered: list[Any] = []
            for item in value:
                unordered.append(cls.scalar(item))
            unordered.sort(key=str)
            return unordered
        if isinstance(value, Mapping):
            mapping: dict[str, Any] = {}
            for key, item in value.items():
                mapping[str(key)] = cls.scalar(item)
            return mapping
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", errors="replace")
        return str(value)
