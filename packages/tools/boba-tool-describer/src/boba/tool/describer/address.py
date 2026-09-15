"""Адреса плагина: узлы-понятия и сводный реестр семейств.

Семейства систем живут в своих пакетах (PgAddresses, ChAddresses,
ConfluenceAddresses); здесь — адрес понятия без системы (`entity://<name>`)
и реестр Addresses, который по схеме url выбирает семейство, а по виду
объекта — класс внутри него.

Ошибки:
AddressError — строка не является адресом заявленного вида или ни одного
    известного семейства.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import ClassVar, Literal, Self
from urllib.parse import SplitResult, quote, unquote, urlsplit, urlunsplit

from pydantic import Field, ValidationError

from boba.confluence.address import ConfluenceAddresses, ConfluenceNodeKind
from boba.connections.address import Address, AddressError, AddressFamily
from boba.db.clickhouse.address import ChAddress, ChAddresses, ChNodeKind
from boba.db.postgres.address import PgAddress, PgAddresses, PgNodeKind
from boba.transport.http.profile import UrlScheme

__all__ = [
    "Addresses",
    "EntityAddress",
    "EntityAddresses",
    "EntityKind",
    "NodeKind",
]


class EntityKind(StrEnum):
    """Вид узла-понятия."""

    ENTITY = "entity"


class EntityAddress(Address):
    """Понятие без системы: `entity://<name>`, имя — reg-name с экранированием."""

    SCHEME: ClassVar[str] = "entity"
    KIND = EntityKind.ENTITY

    scheme: Literal["entity"] = "entity"
    name: str = Field(min_length=1)

    @classmethod
    def shape(cls) -> str:
        return "entity://<name>"

    def render(self) -> str:
        split = SplitResult(
            scheme=self.scheme,
            netloc=quote(self.name, safe=""),
            path="",
            query="",
            fragment="",
        )

        return urlunsplit(split)

    @classmethod
    def accepts(cls, text: str) -> bool:
        return urlsplit(text).scheme == cls.SCHEME

    @classmethod
    def parse(cls, text: str) -> Self:
        url = urlsplit(text)
        if url.scheme != cls.SCHEME:
            msg = (
                f"entity address {text!r}: expected scheme {cls.SCHEME}, "
                f"got {url.scheme!r}"
            )
            raise AddressError(msg)

        if url.path or url.query or url.fragment:
            msg = f"entity address {text!r}: expected entity://<name> and nothing else"
            raise AddressError(msg)

        name = unquote(url.netloc)
        if not name:
            msg = f"entity address {text!r}: name is required"
            raise AddressError(msg)

        try:
            return cls.model_validate({"scheme": url.scheme, "name": name})
        except ValidationError as exc:
            msg = f"{cls.__name__}: address {text!r} is not valid: {exc}"
            raise AddressError(msg) from exc


class EntityAddresses(AddressFamily):
    """Реестр адресов-понятий."""

    MODELS: ClassVar[tuple[type[EntityAddress], ...]] = (EntityAddress,)


NodeKind = PgNodeKind | ChNodeKind | ConfluenceNodeKind | EntityKind
"""Вид узла: объединение видов всех семейств; его называет модель."""


class Addresses:
    """Сводный реестр: семейство по схеме url, класс — по виду и форме."""

    FAMILIES: ClassVar[Mapping[str, type[AddressFamily]]] = {
        PgAddress.SCHEME: PgAddresses,
        ChAddress.SCHEME: ChAddresses,
        UrlScheme.HTTP.value: ConfluenceAddresses,
        UrlScheme.HTTPS.value: ConfluenceAddresses,
        EntityAddress.SCHEME: EntityAddresses,
    }

    @classmethod
    def parse(cls, kind: NodeKind, text: str) -> Address:
        """Строка → адрес заявленного вида."""
        family = cls._family_of_kind(kind)

        return family.parse(kind, text)

    @classmethod
    def parse_any(cls, text: str) -> Address:
        """Строка → адрес; семейство по схеме, вид по форме."""
        family = cls._family_of_scheme(text)

        return family.parse_any(text)

    @classmethod
    def prompt(cls) -> str:
        """Формы всех семейств по видам: по строке на вид."""
        return "\n".join(cls._prompts())

    @classmethod
    def _prompts(cls) -> Iterator[str]:
        for family in cls._families():
            yield family.prompt()

    @classmethod
    def _families(cls) -> Iterator[type[AddressFamily]]:
        seen: set[type[AddressFamily]] = set()
        for family in cls.FAMILIES.values():
            if family in seen:
                continue

            seen.add(family)
            yield family

    @classmethod
    def _family_of_kind(cls, kind: NodeKind) -> type[AddressFamily]:
        for family in cls._families():
            if kind in family.kinds():
                return family

        msg = f"address kind {kind!r} belongs to no known family"
        raise AddressError(msg)

    @classmethod
    def _family_of_scheme(cls, text: str) -> type[AddressFamily]:
        scheme = urlsplit(text).scheme
        family = cls.FAMILIES.get(scheme)
        if family is None:
            msg = (
                f"address {text!r}: unknown scheme {scheme!r}, "
                f"expected one of {sorted(cls.FAMILIES)}"
            )
            raise AddressError(msg)

        return family
