"""Адреса объектов PostgreSQL: подключение плюс роли объекта в query.

`postgresql://host:port/database?schema=dm&table=fact_orders` — libpq URI
без учётки, порт явный (в строке может быть опущен — тогда 5432 libpq),
path — одна база, роли объекта — query в порядке объявления полей. База
зафиксирована подключением, объект её не выбирает. Один класс на форму
объекта; вид (PgNodeKind) задаёт класс: колонка таблицы, view и matview —
три формы одного pg_column. Рутины pg_proc адресуются по prokind: function,
procedure, а pg_routine — рутина любого вида (агрегат, оконная функция или
вид не различён); перегрузки различаются ролью args — сигнатурой
pg_get_function_identity_arguments, пустая строка обязательна. Общий разбор
и сборка URI — в boba.connections.database_address.

Ошибки:
AddressError — строка не является адресом PostgreSQL: чужая схема, учётка
    или фрагмент, нет хоста, путь не одной базой, состав ролей не совпал.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import ClassVar, Literal

from pydantic import Field

from boba.connections.address import AddressError, AddressFamily
from boba.connections.database_address import DatabaseAddress
from boba.db.postgres.connection import PostgresConfig

__all__ = [
    "PgAddress",
    "PgAddresses",
    "PgConstraintAddress",
    "PgDatabaseAddress",
    "PgFunctionAddress",
    "PgIndexAddress",
    "PgMatviewAddress",
    "PgMatviewColumnAddress",
    "PgNodeKind",
    "PgProcedureAddress",
    "PgRoutineAddress",
    "PgSchemaAddress",
    "PgSequenceAddress",
    "PgTableAddress",
    "PgTableColumnAddress",
    "PgTriggerAddress",
    "PgViewAddress",
    "PgViewColumnAddress",
]


class PgNodeKind(StrEnum):
    """Виды объектов PostgreSQL, которые адресуются."""

    DATABASE = "pg_database"
    SCHEMA = "pg_schema"
    TABLE = "pg_table"
    VIEW = "pg_view"
    MATVIEW = "pg_matview"
    COLUMN = "pg_column"
    INDEX = "pg_index"
    CONSTRAINT = "pg_constraint"
    FUNCTION = "pg_function"
    PROCEDURE = "pg_procedure"
    ROUTINE = "pg_routine"
    TRIGGER = "pg_trigger"
    SEQUENCE = "pg_sequence"


class PgAddress(DatabaseAddress):
    """Схема и поля адресов PostgreSQL; общий URI-контракт — в boba-connections."""

    SCHEME: ClassVar[str] = "postgresql"
    LIBPQ_PORT: ClassVar[int] = 5432
    DEFAULT_PORT: ClassVar[int | None] = LIBPQ_PORT

    scheme: Literal["postgresql"] = "postgresql"


class PgDatabaseAddress(PgAddress):
    KIND = PgNodeKind.DATABASE


class PgSchemaAddress(PgAddress):
    KIND = PgNodeKind.SCHEMA

    schema_name: str = Field(alias="schema", min_length=1)


class PgTableAddress(PgAddress):
    KIND = PgNodeKind.TABLE

    schema_name: str = Field(alias="schema", min_length=1)
    table: str = Field(min_length=1)


class PgViewAddress(PgAddress):
    KIND = PgNodeKind.VIEW

    schema_name: str = Field(alias="schema", min_length=1)
    view: str = Field(min_length=1)


class PgMatviewAddress(PgAddress):
    KIND = PgNodeKind.MATVIEW

    schema_name: str = Field(alias="schema", min_length=1)
    matview: str = Field(min_length=1)


class PgTableColumnAddress(PgAddress):
    KIND = PgNodeKind.COLUMN

    schema_name: str = Field(alias="schema", min_length=1)
    table: str = Field(min_length=1)
    column: str = Field(min_length=1)


class PgViewColumnAddress(PgAddress):
    KIND = PgNodeKind.COLUMN

    schema_name: str = Field(alias="schema", min_length=1)
    view: str = Field(min_length=1)
    column: str = Field(min_length=1)


class PgMatviewColumnAddress(PgAddress):
    KIND = PgNodeKind.COLUMN

    schema_name: str = Field(alias="schema", min_length=1)
    matview: str = Field(min_length=1)
    column: str = Field(min_length=1)


class PgIndexAddress(PgAddress):
    """Индекс уникален в схеме, таблица в адрес не входит."""

    KIND = PgNodeKind.INDEX

    schema_name: str = Field(alias="schema", min_length=1)
    index: str = Field(min_length=1)


class PgSequenceAddress(PgAddress):
    KIND = PgNodeKind.SEQUENCE

    schema_name: str = Field(alias="schema", min_length=1)
    sequence: str = Field(min_length=1)


class PgFunctionAddress(PgAddress):
    KIND = PgNodeKind.FUNCTION

    schema_name: str = Field(alias="schema", min_length=1)
    function: str = Field(min_length=1)
    args: str


class PgProcedureAddress(PgAddress):
    KIND = PgNodeKind.PROCEDURE

    schema_name: str = Field(alias="schema", min_length=1)
    procedure: str = Field(min_length=1)
    args: str


class PgRoutineAddress(PgAddress):
    """Рутина любого prokind: агрегат, оконная функция или вид не различён."""

    KIND = PgNodeKind.ROUTINE

    schema_name: str = Field(alias="schema", min_length=1)
    routine: str = Field(min_length=1)
    args: str


class PgConstraintAddress(PgAddress):
    KIND = PgNodeKind.CONSTRAINT

    schema_name: str = Field(alias="schema", min_length=1)
    table: str = Field(min_length=1)
    constraint: str = Field(min_length=1)


class PgTriggerAddress(PgAddress):
    KIND = PgNodeKind.TRIGGER

    schema_name: str = Field(alias="schema", min_length=1)
    table: str = Field(min_length=1)
    trigger: str = Field(min_length=1)


class PgAddresses(AddressFamily):
    """Реестр адресов PostgreSQL и адрес базы по профилю соединения."""

    SYSTEM: ClassVar[str] = "PostgreSQL"
    SCHEMES: ClassVar[frozenset[str]] = frozenset({PgAddress.SCHEME})
    EXAMPLE: ClassVar[str] = "postgresql://host:port/database?<role>=<name>&..."
    MODELS: ClassVar[Sequence[type[PgAddress]]] = (
        PgDatabaseAddress,
        PgSchemaAddress,
        PgTableAddress,
        PgViewAddress,
        PgMatviewAddress,
        PgTableColumnAddress,
        PgViewColumnAddress,
        PgMatviewColumnAddress,
        PgIndexAddress,
        PgSequenceAddress,
        PgFunctionAddress,
        PgProcedureAddress,
        PgRoutineAddress,
        PgConstraintAddress,
        PgTriggerAddress,
    )

    HOST_SEPARATOR: ClassVar[str] = ","
    """libpq принимает список хостов через запятую; адрес берёт первый."""

    @classmethod
    def base_of(cls, connection: PostgresConfig) -> PgDatabaseAddress:
        """Адрес базы соединения: host из профиля (первый из списка libpq,
        либо hostaddr), порт профиля или libpq по умолчанию, dbname."""
        host = cls._host_of(connection)

        port = connection.port
        if port is None:
            port = PgAddress.LIBPQ_PORT

        return PgDatabaseAddress(host=host, port=port, database=connection.dbname)

    @classmethod
    def url_of(cls, connection: PostgresConfig) -> str:
        """URL соединения для показа: база в профиле обязательна, поэтому это
        всегда адрес базы."""
        return cls.base_of(connection).render()

    @classmethod
    def _host_of(cls, connection: PostgresConfig) -> str:
        if connection.host:
            return connection.host.split(cls.HOST_SEPARATOR)[0]

        if connection.hostaddr:
            return connection.hostaddr

        msg = "postgres connection: neither host nor hostaddr is set, address needs one"
        raise AddressError(msg)
