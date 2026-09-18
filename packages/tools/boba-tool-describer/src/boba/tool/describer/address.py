"""Адреса плагина: семейство понятий и реестр установленных семейств.

Семейства систем живут в своих пакетах и находятся по entry points группы
boba.addresses; плагин их не перечисляет. Своё семейство здесь одно —
понятие без системы (`entity://<name>`), оно объявлено той же группой в
pyproject плагина.

Ошибки:
AddressError — строка не является адресом заявленного вида или ни одного
    установленного семейства.
AddressFamiliesError — установленный entry point группы boba.addresses не
    является семейством адресов.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, Literal, Self
from urllib.parse import SplitResult, quote, unquote, urlsplit, urlunsplit

from pydantic import Field, ValidationError

from boba.connections.address import (
    Address,
    AddressError,
    AddressFamilies,
    AddressFamily,
)

__all__ = ["Addresses", "EntityAddress", "EntityAddresses", "EntityKind"]


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

    SYSTEM: ClassVar[str] = "Domain entity (no system)"
    SCHEMES: ClassVar[frozenset[str]] = frozenset({EntityAddress.SCHEME})
    EXAMPLE: ClassVar[str] = "entity://<name>"
    MODELS: ClassVar[tuple[type[EntityAddress], ...]] = (EntityAddress,)


class Addresses:
    """Установленные семейства адресов: находятся один раз при первом обращении."""

    _FAMILIES: ClassVar[AddressFamilies | None] = None

    @classmethod
    def families(cls) -> AddressFamilies:
        if cls._FAMILIES is None:
            cls._FAMILIES = AddressFamilies.discover()

        return cls._FAMILIES

    @classmethod
    def parse(cls, kind: str, text: str) -> Address:
        return cls.families().parse(kind, text)

    @classmethod
    def parse_any(cls, text: str) -> Address:
        return cls.families().parse_any(text)

    @classmethod
    def prompt(cls) -> str:
        return cls.families().prompt()

    @classmethod
    def kinds_prompt(cls) -> str:
        return cls.families().kinds_prompt()
