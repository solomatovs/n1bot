"""Билет вызова вместо keytab/делегированной секции в конфиге инструмента.

Keytab остаётся у приложения, делегированный TGT — в реестре входов; в
песочницу с конфигом соединения уезжает один сервисный билет к его SPN,
выпущенный перед этим самым вызовом. Обвязка ServiceTickets делает это для
статических конфигов секций (kb, ingest), UserConnections — для соединений
пользователя из таблицы.

Ошибки:
KerberosError — билет к соединению не выпущен, вызов начинать нечем.
ToolConfigError — секция требует делегирования, а источника кредов нет.
InjectedAsyncOnlyError — тело инструмента вызвано синхронно: билет выпускается
    только в async-теле.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from boba.connections.clickhouse import ClickHouseConfig
from boba.connections.http import HttpProfile, NegotiateAuth
from boba.connections.kerberos import (
    DelegatedAuth,
    KerberosAuthBase,
    KerberosPasswordAuth,
    KeytabAuth,
    TicketAuth,
)
from boba.connections.postgres import PostgresConfig
from boba.connections.profile import ConnectionProfile
from boba.krb import (
    KerberosCredentials,
    KeytabCredentials,
    PasswordCredentials,
    ServiceTicketIssuer,
)
from boba.toolrun.injected import (
    AsyncInjected,
    ConfigResolver,
    ToolConfigError,
)

__all__ = ["DelegationSource", "ServiceTickets", "TicketArming"]

logger = logging.getLogger(__name__)

DelegationSource = Callable[[], KerberosCredentials]
"""Делегированные креды текущего вызова; зовётся, когда секция их требует."""


class TicketArming:
    """Замена kerberos-секций профилей соединения билетами вызова.

    Обходит значение любой вложенности (модели, словари, списки); профиль
    с keytab получает билет из кредов keytab, с delegated — из кредов
    источника делегирования; билет в секции остаётся как есть.
    """

    def __init__(self, delegation: DelegationSource) -> None:
        self._delegation = delegation

    @staticmethod
    def no_delegation() -> KerberosCredentials:
        """Источник для статических конфигов: делегировать тут некому."""
        msg = (
            "a delegated kerberos section needs a user session; "
            "service configs must carry keytab credentials"
        )
        raise ToolConfigError(msg)

    @classmethod
    def needs_arming(cls, value: object) -> bool:
        """Есть ли в значении kerberos-секция, которую нельзя отдавать наружу."""
        for profile in cls._profiles(value):
            section = cls.section_of(profile)
            if isinstance(section, KeytabAuth | KerberosPasswordAuth | DelegatedAuth):
                return True

        return False

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
    def _profiles(cls, value: object) -> Iterator[ConnectionProfile]:
        if isinstance(value, PostgresConfig | ClickHouseConfig | HttpProfile):
            yield value
            return

        if isinstance(value, BaseModel):
            for name in type(value).model_fields:
                yield from cls._profiles(getattr(value, name))
            return

        if isinstance(value, Mapping):
            for nested in value.values():
                yield from cls._profiles(nested)
            return

        if isinstance(value, list | tuple):
            for nested in value:
                yield from cls._profiles(nested)

    async def arm(self, value: object) -> object:
        """Значение с билетами вместо keytab/delegated-секций; прочее как есть."""
        if isinstance(value, PostgresConfig | ClickHouseConfig | HttpProfile):
            return await self.arm_profile(value)

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

    async def arm_profile(self, profile: ConnectionProfile) -> ConnectionProfile:
        section = self.section_of(profile)
        if section is None:
            return profile

        if isinstance(section, TicketAuth):
            return profile

        source = self._source(section)
        issuer = ServiceTicketIssuer(section.min_lifetime)
        service = profile.service_name()
        ticket = await issuer.issue_async(source, service)

        logger.info(
            "kerberos: call ticket for %s -> %s (source: %s)",
            ticket.principal,
            service,
            section.trace(),
        )

        if isinstance(profile, HttpProfile):
            auth = profile.auth.model_copy(update={"kerberos": ticket})
            return profile.model_copy(update={"auth": auth})

        return profile.model_copy(update={"auth": ticket})

    def _source(self, section: KerberosAuthBase) -> KerberosCredentials:
        """Креды, которыми выпускается билет вызова; билет источником не бывает."""
        if isinstance(section, DelegatedAuth):
            return self._delegation()

        if isinstance(section, KeytabAuth):
            return KeytabCredentials.of(section)

        if isinstance(section, KerberosPasswordAuth):
            return PasswordCredentials.of(section)

        msg = f"{type(section).__name__}: not a source of a call ticket"
        raise ToolConfigError(msg)


class ServiceTickets(AsyncInjected):
    """Обвязка секции: статический injected-конфиг с keytab едет билетом вызова."""

    def __init__(self, param: str, base: object) -> None:
        super().__init__(param, base)
        self._arming = TicketArming(TicketArming.no_delegation)

    @classmethod
    def bind_all(cls, tools: Sequence[BaseTool], resolve: ConfigResolver) -> None:
        """Ставит обвязку на инструменты, чей injected-конфиг несёт kerberos-секцию.

        Зовётся до InjectedConfig: injected-поля читаются со схемы, пока их
        с неё не сняли.
        """
        cls.bind_each(tools, resolve, TicketArming.needs_arming, cls)

    async def value(self, name: str, kwargs: dict[str, object]) -> object:
        return await self._arming.arm(self._base)
