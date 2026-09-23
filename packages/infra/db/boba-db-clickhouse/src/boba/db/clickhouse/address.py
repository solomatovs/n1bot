"""Адреса объектов ClickHouse: подключение плюс роли объекта в query.

`clickhouse://host:port/database?table=events&column=ts` — без учётки, порт
обязателен (у ClickHouse нет одного канонического: native 9000, http 8123),
path — одна база, роли объекта — query в порядке объявления полей. Схем
нет, объекты сразу в базе; база соединения — лишь база по умолчанию,
поэтому объект называет свою базу сегментом пути. Сборка и разбор строки —
здесь и только здесь, на urllib.parse.

Ошибки:
AddressError — строка не является адресом ClickHouse: чужая схема, учётка
    или фрагмент, нет хоста или порта, путь не одной базой, состав ролей
    не совпал.
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


class ChAddress(Address):
    """База адресов ClickHouse: подключение и грамматика строки; роли объекта
    — поля наследника после полей подключения, по alias."""

    SCHEME: ClassVar[str] = "clickhouse"
    BASE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"scheme", "host", "port", "database"}
    )
    PATH_ROOT: ClassVar[str] = "/"

    scheme: Literal["clickhouse"] = "clickhouse"
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
                f"clickhouse address {text!r}: expected scheme {cls.SCHEME}, "
                f"got {url.scheme!r}"
            )
            raise AddressError(msg)

        if url.username is not None:
            msg = f"clickhouse address {text!r}: credentials are not part of an address"
            raise AddressError(msg)

        if url.fragment:
            msg = f"clickhouse address {text!r}: fragment is not part of an address"
            raise AddressError(msg)

        host = url.hostname
        if not host:
            msg = f"clickhouse address {text!r}: host is required"
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
            msg = f"clickhouse address {text!r}: port is not a number: {exc}"
            raise AddressError(msg) from exc

        if port is None:
            msg = f"clickhouse address {text!r}: port is required"
            raise AddressError(msg)

        return port

    @classmethod
    def _database_of(cls, url: SplitResult, text: str) -> str:
        parts = PurePosixPath(url.path).parts
        if parts and parts[0] == cls.PATH_ROOT:
            parts = parts[1:]

        if len(parts) != 1:
            msg = (
                f"clickhouse address {text!r}: path must be a single segment "
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
        host = connection.host
        if not host:
            msg = "clickhouse connection: host is empty, address needs it"
            raise AddressError(msg)

        port = connection.port
        if not port:
            msg = f"clickhouse connection to {host}: port is empty, address needs it"
            raise AddressError(msg)

        database = connection.database
        if not database:
            msg = (
                f"clickhouse connection to {host}:{port}: no default database in "
                "the connection, address the object with an explicit database"
            )
            raise AddressError(msg)

        return ChDatabaseAddress(host=host, port=port, database=database)
