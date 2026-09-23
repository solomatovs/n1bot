"""Адреса объектов Oracle: подключение плюс роли объекта в query.

`oracle://host:port/service?schema=HR&table=EMPLOYEES` — адрес без учётки,
порт явный (в строке может быть опущен — тогда 1521), path — имя сервиса
(PDB или сервис экземпляра), роли объекта — query в порядке объявления полей.
Сервис зафиксирован подключением, объект его не выбирает. Один класс на форму
объекта; вид (OraNodeKind) задаёт класс: колонка таблицы, представления и
mview — три формы одного ora_column. Грамматика та же, что у формул ссылок
ora-meta-scraper (surface_url): адрес каталога и адрес инструмента совпадают
строка в строку. Сборка и разбор — здесь и только здесь, на urllib.parse.

Ошибки:
AddressError — строка не является адресом Oracle: чужая схема, учётка или
    фрагмент, нет хоста, путь не одним сервисом, состав ролей не совпал.
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


class OraAddress(Address):
    """База адресов Oracle: подключение и грамматика строки; роли объекта —
    поля наследника после полей подключения, по alias."""

    SCHEME: ClassVar[str] = "oracle"
    LISTENER_PORT: ClassVar[int] = 1521
    BASE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"scheme", "host", "port", "database"}
    )
    PATH_ROOT: ClassVar[str] = "/"

    scheme: Literal["oracle"] = "oracle"
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1, description="Имя сервиса соединения.")

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
                f"oracle address {text!r}: expected scheme {cls.SCHEME}, "
                f"got {url.scheme!r}"
            )
            raise AddressError(msg)

        if url.username is not None:
            msg = f"oracle address {text!r}: credentials are not part of an address"
            raise AddressError(msg)

        if url.fragment:
            msg = f"oracle address {text!r}: fragment is not part of an address"
            raise AddressError(msg)

        host = url.hostname
        if not host:
            msg = f"oracle address {text!r}: host is required"
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
            msg = f"oracle address {text!r}: port is not a number: {exc}"
            raise AddressError(msg) from exc

        if port is None:
            return cls.LISTENER_PORT

        return port

    @classmethod
    def _database_of(cls, url: SplitResult, text: str) -> str:
        parts = PurePosixPath(url.path).parts
        if parts and parts[0] == cls.PATH_ROOT:
            parts = parts[1:]

        if len(parts) != 1:
            msg = (
                f"oracle address {text!r}: path must be a single segment "
                f"/<service>, got {url.path!r}"
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
