"""Контракт clickhouse-узлов: имена операций, args вызывающего, запросы payload'а.

Args узла несут только пользовательские поля; профиль соединения с секретами и
лимиты добавляет обогатитель, и в спецификацию графа они не попадают.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from boba.db.clickhouse import ClickHouseConfig
from boba.toolkit.channels import StreamFormat
from boba.toolkit.sql import ConnectionCall, SqlQueryRequest

__all__ = [
    "ChInsertArgs",
    "ChInsertRequest",
    "ChInsertTrailer",
    "ChQueryArgs",
    "ChQueryRequest",
    "ChQueryTrailer",
    "ChStage",
    "ChWireFormat",
]


class ChStage(StrEnum):
    """Имена узлов clickhouse в реестре стадий; они же — операции payload'а."""

    QUERY = "ch_query"
    INSERT = "ch_insert"


class ChWireFormat(StrEnum):
    """Имена форматов входного потока в ClickHouse."""

    JSON_EACH_ROW = "JSONEachRow"
    CSV_WITH_NAMES = "CSVWithNames"

    @classmethod
    def of(cls, stream: StreamFormat) -> ChWireFormat:
        """Формат канала -> формат вставки; иные форматы отсечены моделью запроса."""
        if stream is StreamFormat.NDJSON:
            return cls.JSON_EACH_ROW

        if stream is StreamFormat.CSV:
            return cls.CSV_WITH_NAMES

        msg = f"stream format is not insertable: {stream}"
        raise ValueError(msg)


class ChQueryArgs(BaseModel):
    """Args узла ch_query: что задаёт вызывающий, потолки даёт конфиг."""

    model_config = ConfigDict(extra="forbid")

    connection_name: str = Field(min_length=1)
    sql: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class ChInsertArgs(BaseModel):
    """Args узла ch_insert: куда вставлять и как трактовать байты входа.

    Формат входа объявляет вызывающий: по каналу текут байты, и подставить
    трактовку за него некому.
    """

    model_config = ConfigDict(extra="forbid")

    connection_name: str = Field(min_length=1)
    table: str = Field(min_length=1)
    stdin_format: StreamFormat


class ChQueryRequest(SqlQueryRequest[ClickHouseConfig, dict[str, Any]]):
    """Запрос строк с лимитом; параметры именованные, под подстановку {name:Type}."""

    OP: ClassVar[str] = ChStage.QUERY


class ChInsertRequest(ConnectionCall[ClickHouseConfig]):
    """Вставка входного потока в таблицу; трактовку байтов несёт stdin_format."""

    OP: ClassVar[str] = ChStage.INSERT

    INSERTABLE: ClassVar[frozenset[StreamFormat]] = frozenset(
        {StreamFormat.NDJSON, StreamFormat.CSV}
    )
    """Форматы, которые ClickHouse принимает на вставку; проверка своих args."""

    table: str = Field(min_length=1)
    stdin_format: StreamFormat

    @field_validator("stdin_format")
    @classmethod
    def _insertable(cls, value: StreamFormat) -> StreamFormat:
        if value not in ChInsertRequest.INSERTABLE:
            raise ValueError(f"input format is not insertable: {value}")

        return value


class ChQueryTrailer(BaseModel):
    """Итог запроса: строки ушли каналом данных, здесь признак превышения лимита."""

    model_config = ConfigDict(extra="forbid")

    truncated: bool


class ChInsertTrailer(BaseModel):
    """Итог вставки: сколько строк и байт принял сервер."""

    model_config = ConfigDict(extra="forbid")

    rows: int = Field(ge=0)
    bytes_written: int = Field(ge=0)
