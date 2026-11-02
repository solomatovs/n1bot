"""Адреса объектов ClickHouse: подключение плюс роли объекта в query.

`clickhouse://host:port/database?table=events&column=ts` — без учётки, порт
обязателен (у ClickHouse нет одного канонического: native 9000, http 8123),
path — одна база, роли объекта — query в порядке объявления полей. Схем
нет, объекты сразу в базе; база соединения — лишь база по умолчанию,
поэтому объект называет свою базу сегментом пути. Общий разбор и сборка URI —
в boba.connections.database_address.

Ошибки:
AddressError — строка не является адресом ClickHouse: чужая схема, учётка
    или фрагмент, нет хоста или порта, путь не одной базой, состав ролей
    не совпал.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import ClassVar, Literal
from urllib.parse import (
    SplitResult,
    urlunsplit,
)

from pydantic import Field

from boba.connections.address import AddressError, AddressFamily
from boba.connections.database_address import DatabaseAddress
from boba.db.clickhouse.connection import ClickHouseConfig

__all__ = [
    "ChAddress",
    "ChAddresses",
    "ChDatabaseAddress",
    "ChDictionaryAddress",
    "ChFunctionAddress",
    "ChIndexAddress",
    "ChMatviewAddress",
    "ChMatviewColumnAddress",
    "ChNodeKind",
    "ChProjectionAddress",
    "ChTableAddress",
    "ChTableColumnAddress",
    "ChViewAddress",
    "ChViewColumnAddress",
]


class ChNodeKind(StrEnum):
    """Виды объектов ClickHouse, которые адресуются."""

    DATABASE = "ch_database"
    TABLE = "ch_table"
    VIEW = "ch_view"
    MATVIEW = "ch_matview"
    COLUMN = "ch_column"
    INDEX = "ch_index"
    PROJECTION = "ch_projection"
    DICTIONARY = "ch_dictionary"
    FUNCTION = "ch_function"


class ChAddress(DatabaseAddress):
    """Схема и поля адресов ClickHouse; общий URI-контракт — в boba-connections."""

    SCHEME: ClassVar[str] = "clickhouse"

    scheme: Literal["clickhouse"] = "clickhouse"


class ChDatabaseAddress(ChAddress):
    KIND = ChNodeKind.DATABASE


class ChTableAddress(ChAddress):
    KIND = ChNodeKind.TABLE

    table: str = Field(min_length=1)


class ChViewAddress(ChAddress):
    KIND = ChNodeKind.VIEW

    view: str = Field(min_length=1)


class ChMatviewAddress(ChAddress):
    KIND = ChNodeKind.MATVIEW

    matview: str = Field(min_length=1)


class ChTableColumnAddress(ChAddress):
    KIND = ChNodeKind.COLUMN

    table: str = Field(min_length=1)
    column: str = Field(min_length=1)


class ChViewColumnAddress(ChAddress):
    KIND = ChNodeKind.COLUMN

    view: str = Field(min_length=1)
    column: str = Field(min_length=1)


class ChMatviewColumnAddress(ChAddress):
    KIND = ChNodeKind.COLUMN

    matview: str = Field(min_length=1)
    column: str = Field(min_length=1)


class ChIndexAddress(ChAddress):
    """Skip-индекс уникален внутри таблицы."""

    KIND = ChNodeKind.INDEX

    table: str = Field(min_length=1)
    index: str = Field(min_length=1)


class ChProjectionAddress(ChAddress):
    KIND = ChNodeKind.PROJECTION

    table: str = Field(min_length=1)
    projection: str = Field(min_length=1)


class ChDictionaryAddress(ChAddress):
    KIND = ChNodeKind.DICTIONARY

    dictionary: str = Field(min_length=1)


class ChFunctionAddress(ChAddress):
    """Перегрузок у функций ClickHouse нет, сигнатура не нужна."""

    KIND = ChNodeKind.FUNCTION

    function: str = Field(min_length=1)


class ChAddresses(AddressFamily):
    """Реестр адресов ClickHouse и адрес базы по профилю соединения."""

    SYSTEM: ClassVar[str] = "ClickHouse"
    SCHEMES: ClassVar[frozenset[str]] = frozenset({ChAddress.SCHEME})
    EXAMPLE: ClassVar[str] = "clickhouse://host:port/database?<role>=<name>&..."
    MODELS: ClassVar[Sequence[type[ChAddress]]] = (
        ChDatabaseAddress,
        ChTableAddress,
        ChViewAddress,
        ChMatviewAddress,
        ChTableColumnAddress,
        ChViewColumnAddress,
        ChMatviewColumnAddress,
        ChIndexAddress,
        ChProjectionAddress,
        ChDictionaryAddress,
        ChFunctionAddress,
    )

    @classmethod
    def base_of(cls, connection: ClickHouseConfig) -> ChDatabaseAddress:
        """Адрес базы по умолчанию соединения; без базы в профиле адреса нет:
        объект тогда называет базу сам."""
        host, port = cls._server_of(connection)

        database = connection.database
        if not database:
            msg = (
                f"clickhouse connection to {host}:{port}: no default database in "
                "the connection, address the object with an explicit database"
            )
            raise AddressError(msg)

        return ChDatabaseAddress(host=host, port=port, database=database)

    @classmethod
    def url_of(cls, connection: ClickHouseConfig) -> str:
        """URL соединения для показа: адрес базы по умолчанию, если она в
        профиле есть, иначе корень сервера clickhouse://host:port/."""
        if connection.database:
            return cls.base_of(connection).render()

        host, port = cls._server_of(connection)
        split = SplitResult(
            scheme=ChAddress.SCHEME,
            netloc=ChAddress.netloc_of(host, port),
            path=ChAddress.PATH_ROOT,
            query="",
            fragment="",
        )

        return urlunsplit(split)

    @staticmethod
    def _server_of(connection: ClickHouseConfig) -> tuple[str, int]:
        host = connection.host
        if not host:
            msg = "clickhouse connection: host is empty, address needs it"
            raise AddressError(msg)

        port = connection.port
        if not port:
            msg = f"clickhouse connection to {host}: port is empty, address needs it"
            raise AddressError(msg)

        return host, port
