"""Адреса объектов Oracle: подключение плюс роли объекта в query.

`oracle://host:port/service?schema=HR&table=EMPLOYEES` — адрес без учётки,
порт явный (в строке может быть опущен — тогда 1521), path — имя сервиса
(PDB или сервис экземпляра), роли объекта — query в порядке объявления полей.
Сервис зафиксирован подключением, объект его не выбирает. Один класс на форму
объекта; вид (OraNodeKind) задаёт класс: колонка таблицы, представления и
mview — три формы одного ora_column. Грамматика та же, что у формул ссылок
ora-meta-scraper (surface_url): адрес каталога и адрес инструмента совпадают
строка в строку. Общий разбор и сборка URI — в boba.connections.database_address.

Ошибки:
AddressError — строка не является адресом Oracle: чужая схема, учётка или
    фрагмент, нет хоста, путь не одним сервисом, состав ролей не совпал.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import ClassVar, Literal

from pydantic import Field

from boba.connections.address import AddressFamily
from boba.connections.database_address import DatabaseAddress
from boba.db.oracle.connection import OracleConfig

__all__ = [
    "OraAddress",
    "OraAddresses",
    "OraDatabaseAddress",
    "OraIndexAddress",
    "OraMviewAddress",
    "OraMviewColumnAddress",
    "OraMviewIndexAddress",
    "OraNodeKind",
    "OraRoutineAddress",
    "OraSchemaAddress",
    "OraSequenceAddress",
    "OraSynonymAddress",
    "OraTableAddress",
    "OraTableColumnAddress",
    "OraTableConstraintAddress",
    "OraTableTriggerAddress",
    "OraViewAddress",
    "OraViewColumnAddress",
    "OraViewConstraintAddress",
    "OraViewTriggerAddress",
]


class OraNodeKind(StrEnum):
    """Виды объектов Oracle, которые адресуются."""

    DATABASE = "ora_database"
    SCHEMA = "ora_schema"
    TABLE = "ora_table"
    VIEW = "ora_view"
    MVIEW = "ora_mview"
    COLUMN = "ora_column"
    CONSTRAINT = "ora_constraint"
    INDEX = "ora_index"
    SEQUENCE = "ora_sequence"
    SYNONYM = "ora_synonym"
    TRIGGER = "ora_trigger"
    ROUTINE = "ora_routine"


class OraAddress(DatabaseAddress):
    """Схема и поля адресов Oracle; общий URI-контракт — в boba-connections."""

    SCHEME: ClassVar[str] = "oracle"
    LISTENER_PORT: ClassVar[int] = 1521
    DEFAULT_PORT: ClassVar[int | None] = LISTENER_PORT
    PATH_LABEL: ClassVar[str] = "service"

    scheme: Literal["oracle"] = "oracle"
    database: str = Field(min_length=1, description="Имя сервиса соединения.")


class OraDatabaseAddress(OraAddress):
    KIND = OraNodeKind.DATABASE


class OraSchemaAddress(OraAddress):
    KIND = OraNodeKind.SCHEMA

    schema_name: str = Field(alias="schema", min_length=1)


class OraTableAddress(OraAddress):
    KIND = OraNodeKind.TABLE

    schema_name: str = Field(alias="schema", min_length=1)
    table: str = Field(min_length=1)


class OraViewAddress(OraAddress):
    KIND = OraNodeKind.VIEW

    schema_name: str = Field(alias="schema", min_length=1)
    view: str = Field(min_length=1)


class OraMviewAddress(OraAddress):
    KIND = OraNodeKind.MVIEW

    schema_name: str = Field(alias="schema", min_length=1)
    mview: str = Field(min_length=1)


class OraTableColumnAddress(OraAddress):
    KIND = OraNodeKind.COLUMN

    schema_name: str = Field(alias="schema", min_length=1)
    table: str = Field(min_length=1)
    column: str = Field(min_length=1)


class OraViewColumnAddress(OraAddress):
    KIND = OraNodeKind.COLUMN

    schema_name: str = Field(alias="schema", min_length=1)
    view: str = Field(min_length=1)
    column: str = Field(min_length=1)


class OraMviewColumnAddress(OraAddress):
    KIND = OraNodeKind.COLUMN

    schema_name: str = Field(alias="schema", min_length=1)
    mview: str = Field(min_length=1)
    column: str = Field(min_length=1)


class OraTableConstraintAddress(OraAddress):
    KIND = OraNodeKind.CONSTRAINT

    schema_name: str = Field(alias="schema", min_length=1)
    table: str = Field(min_length=1)
    constraint: str = Field(min_length=1)


class OraViewConstraintAddress(OraAddress):
    """Check option и read only представления — тоже constraint'ы словаря."""

    KIND = OraNodeKind.CONSTRAINT

    schema_name: str = Field(alias="schema", min_length=1)
    view: str = Field(min_length=1)
    constraint: str = Field(min_length=1)


class OraIndexAddress(OraAddress):
    KIND = OraNodeKind.INDEX

    schema_name: str = Field(alias="schema", min_length=1)
    table: str = Field(min_length=1)
    index: str = Field(min_length=1)


class OraMviewIndexAddress(OraAddress):
    KIND = OraNodeKind.INDEX

    schema_name: str = Field(alias="schema", min_length=1)
    mview: str = Field(min_length=1)
    index: str = Field(min_length=1)


class OraSequenceAddress(OraAddress):
    KIND = OraNodeKind.SEQUENCE

    schema_name: str = Field(alias="schema", min_length=1)
    sequence: str = Field(min_length=1)


class OraSynonymAddress(OraAddress):
    KIND = OraNodeKind.SYNONYM

    schema_name: str = Field(alias="schema", min_length=1)
    synonym: str = Field(min_length=1)


class OraTableTriggerAddress(OraAddress):
    KIND = OraNodeKind.TRIGGER

    schema_name: str = Field(alias="schema", min_length=1)
    table: str = Field(min_length=1)
    trigger: str = Field(min_length=1)


class OraViewTriggerAddress(OraAddress):
    """Триггер instead of на представлении."""

    KIND = OraNodeKind.TRIGGER

    schema_name: str = Field(alias="schema", min_length=1)
    view: str = Field(min_length=1)
    trigger: str = Field(min_length=1)


class OraRoutineAddress(OraAddress):
    """Процедура, функция, пакет или тип: в схеме у них одно пространство имён."""

    KIND = OraNodeKind.ROUTINE

    schema_name: str = Field(alias="schema", min_length=1)
    routine: str = Field(min_length=1)


class OraAddresses(AddressFamily):
    """Реестр адресов Oracle и адрес базы по профилю соединения."""

    SYSTEM: ClassVar[str] = "Oracle"
    SCHEMES: ClassVar[frozenset[str]] = frozenset({OraAddress.SCHEME})
    EXAMPLE: ClassVar[str] = "oracle://host:port/service?<role>=<name>&..."
    MODELS: ClassVar[Sequence[type[OraAddress]]] = (
        OraDatabaseAddress,
        OraSchemaAddress,
        OraTableAddress,
        OraViewAddress,
        OraMviewAddress,
        OraTableColumnAddress,
        OraViewColumnAddress,
        OraMviewColumnAddress,
        OraTableConstraintAddress,
        OraViewConstraintAddress,
        OraIndexAddress,
        OraMviewIndexAddress,
        OraSequenceAddress,
        OraSynonymAddress,
        OraTableTriggerAddress,
        OraViewTriggerAddress,
        OraRoutineAddress,
    )

    @classmethod
    def base_of(cls, connection: OracleConfig) -> OraDatabaseAddress:
        """Адрес базы соединения: host, port и сервис профиля."""
        return OraDatabaseAddress(
            host=connection.host, port=connection.port, database=connection.service
        )

    @classmethod
    def url_of(cls, connection: OracleConfig) -> str:
        """URL соединения для показа: сервис всегда в профиле, это адрес базы."""
        return cls.base_of(connection).render()
