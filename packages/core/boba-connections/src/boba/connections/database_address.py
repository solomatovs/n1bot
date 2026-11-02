"""URI объектов БД: узел, одна база в path и именованные роли в query.

Общий разбор и сборка моделей адресов не открывают соединений и не зависят
от драйверов. Конкретные схемы и виды объектов объявляют пакеты адаптеров.

Ошибки:
AddressError — схема, узел, путь или роли не соответствуют модели адреса.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import PurePosixPath
from typing import ClassVar, Self
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

from boba.connections.address import Address, AddressError

__all__ = ["DatabaseAddress"]


class DatabaseAddress(Address):
    """Общая URI-модель объекта БД без драйверов и соединений.

    Семейства PostgreSQL, Oracle и ClickHouse задают схему, порт по умолчанию
    и поля объекта; реестр AddressFamily выбирает их по составу query-ролей.
    """

    SCHEME: ClassVar[str]
    DEFAULT_PORT: ClassVar[int | None] = None
    PATH_LABEL: ClassVar[str] = "database"
    BASE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"scheme", "host", "port", "database"}
    )
    PATH_ROOT: ClassVar[str] = "/"

    scheme: str
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
            netloc=self.netloc_of(self.host, self.port),
            path=PurePosixPath(
                self.PATH_ROOT, quote(self.database, safe="")
            ).as_posix(),
            query=query,
            fragment="",
        )

        return urlunsplit(split)

    @staticmethod
    def netloc_of(host: str, port: int) -> str:
        """IPv6 — в скобках по RFC 3986 §3.2.2."""
        if ":" in host:
            return f"[{host}]:{port}"

        return f"{host}:{port}"

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
                f"{cls.SCHEME} address {text!r}: expected scheme {cls.SCHEME}, "
                f"got {url.scheme!r}"
            )
            raise AddressError(msg)

        if url.username is not None:
            msg = (
                f"{cls.SCHEME} address {text!r}: credentials are not part of an address"
            )
            raise AddressError(msg)

        if url.fragment:
            msg = f"{cls.SCHEME} address {text!r}: fragment is not part of an address"
            raise AddressError(msg)

        host = url.hostname
        if not host:
            msg = f"{cls.SCHEME} address {text!r}: host is required"
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
            msg = f"{cls.SCHEME} address {text!r}: port is not a number: {exc}"
            raise AddressError(msg) from exc

        if port is not None:
            return port

        if cls.DEFAULT_PORT is not None:
            return cls.DEFAULT_PORT

        msg = f"{cls.SCHEME} address {text!r}: port is required"
        raise AddressError(msg)

    @classmethod
    def _database_of(cls, url: SplitResult, text: str) -> str:
        parts = PurePosixPath(url.path).parts
        if parts and parts[0] == cls.PATH_ROOT:
            parts = parts[1:]

        if len(parts) != 1:
            msg = (
                f"{cls.SCHEME} address {text!r}: path must be a single segment "
                f"/<{cls.PATH_LABEL}>, got {url.path!r}"
            )
            raise AddressError(msg)

        return unquote(parts[0])

    @classmethod
    def _check_roles(cls, roles: Sequence[tuple[str, str]], text: str) -> None:
        given = [name for name, _ in roles]

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
