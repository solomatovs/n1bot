"""Креды к соединению на вызов: порты и чистая логика kerberos-секций профиля.

Профиль в таблице или конфиге несёт keytab/password/delegated-секцию; в тело
инструмента уезжает профиль с TicketAuth — билетом к SPN соединения, выпущенным
перед этим вызовом. Кто и как выпускает — реализация CredentialSource. Где в
профиле лежит секция, знает сам профиль (ConnectionProfileBase).

Ошибки: своих не выпускает; RefusalError и KerberosError — у реализаций портов.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterator, Mapping
from typing import Protocol

from pydantic import BaseModel

from boba.connections.base import ConnectionProfileBase
from boba.identity.context import Credential
from boba.kerberos import (
    DelegatedAuth,
    KerberosAuthBase,
    KerberosPasswordAuth,
    KeytabAuth,
)

__all__ = ["ArmedValues", "CredentialSource", "ProfileSections"]


class ProfileSections:
    """Kerberos-секции профилей внутри произвольного значения."""

    @staticmethod
    def section_of(profile: ConnectionProfileBase) -> KerberosAuthBase | None:
        """Kerberos-часть профиля: где она лежит, знает сам профиль."""
        return profile.kerberos_section()

    @classmethod
    def needs_arming(cls, value: object) -> bool:
        """Есть ли в значении kerberos-секция, которую нельзя отдавать наружу."""
        for profile in cls.profiles(value):
            section = profile.kerberos_section()
            if isinstance(section, KeytabAuth | KerberosPasswordAuth | DelegatedAuth):
                return True

        return False

    @classmethod
    def profiles(cls, value: object) -> Iterator[ConnectionProfileBase]:
        """Профили соединений внутри значения любой вложенности."""
        if isinstance(value, ConnectionProfileBase):
            yield value
            return

        if isinstance(value, BaseModel):
            for name in type(value).model_fields:
                yield from cls.profiles(getattr(value, name))
            return

        if isinstance(value, Mapping):
            for nested in value.values():
                yield from cls.profiles(nested)
            return

        if isinstance(value, list | tuple):
            for nested in value:
                yield from cls.profiles(nested)


class CredentialSource(Protocol):
    """Профиль с билетом вызова вместо keytab/password/delegated-секции."""

    @abstractmethod
    async def for_connection(
        self, profile: ConnectionProfileBase, credential: Credential
    ) -> ConnectionProfileBase:
        """RefusalError — делегирования у субъекта нет; KerberosError — билет не
        выпущен.
        """


class ArmedValues:
    """Замена kerberos-секций билетами во всём значении: модели, словари, списки."""

    def __init__(self, source: CredentialSource, credential: Credential) -> None:
        self._source = source
        self._credential = credential

    async def arm(self, value: object) -> object:
        if isinstance(value, ConnectionProfileBase):
            return await self._source.for_connection(value, self._credential)

        if isinstance(value, BaseModel):
            return await self._arm_model(value)

        if isinstance(value, Mapping):
            armed_items: dict[object, object] = {}
            for key, nested in value.items():
                armed_items[key] = await self.arm(nested)
            return armed_items

        if isinstance(value, list | tuple):
            armed_list: list[object] = []
            for nested in value:
                armed_list.append(await self.arm(nested))
            return armed_list

        return value

    async def _arm_model(self, model: BaseModel) -> BaseModel:
        update: dict[str, object] = {}
        for name in type(model).model_fields:
            current = getattr(model, name)
            armed = await self.arm(current)
            if armed is not current:
                update[name] = armed

        if not update:
            return model

        return model.model_copy(update=update)
