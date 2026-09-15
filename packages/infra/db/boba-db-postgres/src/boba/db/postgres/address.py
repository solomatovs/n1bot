"""Адреса объектов PostgreSQL: подключение плюс роли объекта в query.

`postgresql://host:port/database?schema=dm&table=fact_orders` — libpq URI
без учётки, порт явный (в строке может быть опущен — тогда 5432 libpq),
path — одна база, роли объекта — query в порядке объявления полей. База
зафиксирована подключением, объект её не выбирает. Один класс на форму
объекта; вид (PgNodeKind) задаёт класс: колонка таблицы, view и matview —
три формы одного pg_column. Рутины pg_proc адресуются по prokind: function,
procedure, а pg_routine — рутина любого вида (агрегат, оконная функция или
вид не различён); перегрузки различаются ролью args — сигнатурой
pg_get_function_identity_arguments, пустая строка обязательна. Сборка и
разбор строки — здесь и только здесь, на urllib.parse.

Ошибки:
AddressError — строка не является адресом PostgreSQL: чужая схема, учётка
    или фрагмент, нет хоста, путь не одной базой, состав ролей не совпал.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from enum import StrEnum
from pathlib import PurePosixPath
from typing import ClassVar, Literal, Self
from urllib.parse import (
    SplitResult,
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urlsplit,
    urlunsplit,
)

from pydantic import Field, ValidationError

from boba.connections.address import Address, AddressError, AddressFamily
from boba.db.postgres.profile import PostgresConfig

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


class PgAddress(Address):
    """База адресов PostgreSQL: подключение и грамматика строки; роли объекта
    — поля наследника после полей подключения, по alias."""

    SCHEME: ClassVar[str] = "postgresql"
    LIBPQ_PORT: ClassVar[int] = 5432
    BASE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"scheme", "host", "port", "database"}
    )
    PATH_ROOT: ClassVar[str] = "/"

    scheme: Literal["postgresql"] = "postgresql"
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1)

    @classmethod
    def roles(cls) -> Sequence[str]:
        """Роли объекта в каноническом порядке, по alias."""
        return tuple(cls._role_names())

    @classmethod
    def _role_names(cls) -> Iterator[str]:
        for name, field in cls.model_fields.items():
            if name in cls.BASE_FIELDS:
                continue

            alias = field.alias
            if alias is None:
                alias = name

            yield alias

    @classmethod
    def shape(cls) -> str:
        roles = cls.roles()
        if not roles:
            return "the database itself, no roles"

        return ", ".join(roles)

    def render(self) -> str:
        values = self.model_dump(
            mode="json", by_alias=True, exclude=set(self.BASE_FIELDS)
        )
        query = urlencode(values, quote_via=quote)

        split = SplitResult(
            scheme=self.scheme,
            netloc=self._netloc(),
            path=PurePosixPath(
                self.PATH_ROOT, quote(self.database, safe="")
            ).as_posix(),
            query=query,
            fragment="",
        )

        return urlunsplit(split)

    def _netloc(self) -> str:
        """IPv6 — в скобках по RFC 3986 §3.2.2."""
        if ":" in self.host:
            return f"[{self.host}]:{self.port}"

        return f"{self.host}:{self.port}"

    @classmethod
    def accepts(cls, text: str) -> bool:
        given = set(cls._given_roles(text))

        return given == set(cls.roles())

    @classmethod
    def _given_roles(cls, text: str) -> Iterator[str]:
        for name, _ in parse_qsl(urlsplit(text).query, keep_blank_values=True):
            yield name

    @classmethod
    def parse(cls, text: str) -> Self:
        url = urlsplit(text)
        if url.scheme != cls.SCHEME:
            msg = (
                f"postgresql address {text!r}: expected scheme {cls.SCHEME}, "
                f"got {url.scheme!r}"
            )
            raise AddressError(msg)

        if url.username is not None:
            msg = f"postgresql address {text!r}: credentials are not part of an address"
            raise AddressError(msg)

        if url.fragment:
            msg = f"postgresql address {text!r}: fragment is not part of an address"
            raise AddressError(msg)

        host = url.hostname
        if not host:
            msg = f"postgresql address {text!r}: host is required"
            raise AddressError(msg)

        port = cls._port_of(url, text)

        database = cls._database_of(url, text)

        roles = parse_qsl(url.query, keep_blank_values=True)
        cls._check_roles(roles, text)

        parts: dict[str, str | int] = {
            "scheme": url.scheme,
            "host": host,
            "port": port,
            "database": database,
        }
        parts.update(roles)

        try:
            return cls.model_validate(parts)
        except ValidationError as exc:
            msg = f"{cls.__name__}: address {text!r} is not valid: {exc}"
            raise AddressError(msg) from exc

    @classmethod
    def _port_of(cls, url: SplitResult, text: str) -> int:
        try:
            port = url.port
        except ValueError as exc:
            msg = f"postgresql address {text!r}: port is not a number: {exc}"
            raise AddressError(msg) from exc

        if port is None:
            return cls.LIBPQ_PORT

        return port

    @classmethod
    def _database_of(cls, url: SplitResult, text: str) -> str:
        parts = PurePosixPath(url.path).parts
        if parts and parts[0] == cls.PATH_ROOT:
            parts = parts[1:]

        if len(parts) != 1:
            msg = (
                f"postgresql address {text!r}: path must be a single segment "
                f"/<database>, got {url.path!r}"
            )
            raise AddressError(msg)

        return unquote(parts[0])

    @classmethod
    def _check_roles(cls, roles: Sequence[tuple[str, str]], text: str) -> None:
        given: list[str] = []
        for name, _ in roles:
            given.append(name)

        if len(set(given)) != len(given):
            msg = f"{cls.__name__}: address {text!r} repeats a role: {given}"
            raise AddressError(msg)

        expected = list(cls.roles())
        if set(given) != set(expected):
            msg = (
                f"{cls.__name__}: address {text!r} expects roles {expected}, "
                f"got {given}"
            )
            raise AddressError(msg)


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
    def base_of(cls, profile: PostgresConfig) -> PgDatabaseAddress:
        """Адрес базы соединения: host из профиля (первый из списка libpq,
        либо hostaddr), порт профиля или libpq по умолчанию, dbname."""
        host = cls._host_of(profile)

        port = profile.port
        if port is None:
            port = PgAddress.LIBPQ_PORT

        database = profile.dbname
        if not database:
            msg = f"postgres connection to {host}: dbname is empty, address needs it"
            raise AddressError(msg)

        return PgDatabaseAddress(host=host, port=port, database=database)

    @classmethod
    def _host_of(cls, profile: PostgresConfig) -> str:
        if profile.host:
            return profile.host.split(cls.HOST_SEPARATOR)[0]

        if profile.hostaddr:
            return profile.hostaddr

        msg = "postgres connection: neither host nor hostaddr is set, address needs one"
        raise AddressError(msg)
