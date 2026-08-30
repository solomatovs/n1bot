"""Креды к соединению на вызов: порты и чистая логика kerberos-секций профиля.

Профиль в таблице или конфиге несёт keytab/password/delegated-секцию; в тело
инструмента уезжает профиль с TicketAuth — билетом к SPN соединения, выпущенным
перед этим вызовом. Кто и как выпускает — реализация CredentialSource.

Ошибки: своих не выпускает; RefusalError и KerberosError — у реализаций портов.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterator, Mapping
from typing import Protocol

from pydantic import BaseModel

from boba.connections.clickhouse import ClickHouseConfig
from boba.connections.http import HttpProfile, NegotiateAuth
from boba.connections.kerberos import (
    DelegatedAuth,
    KerberosAuthBase,
    KerberosPasswordAuth,
    KeytabAuth,
    SignInTicket,
)
from boba.connections.postgres import PostgresConfig
from boba.connections.profile import ConnectionProfile
from boba.identity.context import Credential

__all__ = ["ArmedValues", "CredentialSource", "ProfileSections", "SignInCredentials"]


class ProfileSections:
    """Kerberos-секция профиля: где лежит и нужна ли ей замена билетом."""

    @staticmethod
    def section_of(profile: ConnectionProfile) -> KerberosAuthBase | None:
        """Kerberos-часть профиля: у web внутри NegotiateAuth, у баз — сам auth."""
        if isinstance(profile, HttpProfile):
            if isinstance(profile.auth, NegotiateAuth):
                return profile.auth.kerberos

            return None

        if isinstance(profile.auth, KerberosAuthBase):
            return profile.auth

        return None

    @classmethod
    def needs_arming(cls, value: object) -> bool:
        """Есть ли в значении kerberos-секция, которую нельзя отдавать наружу."""
        for profile in cls.profiles(value):
            section = cls.section_of(profile)
            if isinstance(section, KeytabAuth | KerberosPasswordAuth | DelegatedAuth):
                return True

        return False

    @classmethod
    def profiles(cls, value: object) -> Iterator[ConnectionProfile]:
        """Профили соединений внутри значения любой вложенности."""
        if isinstance(value, PostgresConfig | ClickHouseConfig | HttpProfile):
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


class SignInCredentials(Protocol):
    """Билет входа под печатью приложения: печать при SSO, открытие на вызове."""

    @abstractmethod
    def seal(self, ticket: SignInTicket) -> str: ...

    @abstractmethod
    def open(self, sealed: str) -> SignInTicket:
        """TicketSealError — чужой ключ, порча или не тот формат."""


class CredentialSource(Protocol):
    """Профиль с билетом вызова вместо keytab/password/delegated-секции."""

    @abstractmethod
    async def for_connection(
        self, profile: ConnectionProfile, credential: Credential
    ) -> ConnectionProfile:
        """RefusalError — делегирования у субъекта нет; KerberosError — билет не
        выпущен.
        """


class ArmedValues:
    """Замена kerberos-секций билетами во всём значении: модели, словари, списки."""

    def __init__(self, source: CredentialSource, credential: Credential) -> None:
        self._source = source
        self._credential = credential

    async def arm(self, value: object) -> object:
        if isinstance(value, PostgresConfig | ClickHouseConfig | HttpProfile):
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
