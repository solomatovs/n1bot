"""Билет вызова вместо keytab-секции в статическом конфиге инструмента.

Конфиг секции (kb, ingest) несёт keytab строки; в песочницу с ним уезжает
сервисный билет к SPN соединения, выпущенный источником кредов перед этим
самым вызовом. Делегированная секция в статическом конфиге — ошибка конфига:
делегировать тут некому. Профили в секции плагина хост находит по
указателям манифеста и разбирает моделью их вида (SectionTickets).

Ошибки:
KerberosError — билет к соединению не выпущен, вызов начинать нечем.
ToolConfigError — секция требует делегирования, а источника кредов нет;
    профиль по указателю манифеста не разбирается моделью своего вида.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any, ClassVar

from boba.connections.base import ConnectionBase
from boba.connections.credentials import (
    ArmedValues,
    ConnectionSections,
    CredentialSource,
)
from boba.connections.manifest import ConnectionTypes, ConnectionTypesError
from boba.identity.context import NoUserCredential
from boba.kerberos import DelegatedAuth
from boba.toolkit.entry import ToolArgv
from boba.toolkit.manifest import ConfigConnection
from boba.toolrun.hosted import PluginSection
from boba.toolrun.injected import ConfigArming, SectionArming, ToolConfigError

__all__ = ["CredentialsRef", "SectionTickets", "ServiceTickets"]

CredentialsRef = Callable[[], CredentialSource]
"""Источник кредов вызова; зовётся на вызов, а не при загрузке инструментов."""


class ServiceTickets(ConfigArming):
    """Реализация ConfigArming билетом вызова: статический injected-конфиг с
    keytab-секцией едет в песочницу сервисным билетом к SPN соединения.

    Создаёт его загрузчик инструментов из источника кредов вызова и отдаёт
    источнику конфига (InjectedConfig).
    """

    NO_DELEGATION: ClassVar[str] = (
        "a delegated kerberos section needs a user session; "
        "service configs must carry keytab credentials"
    )

    def __init__(self, credentials_ref: CredentialsRef) -> None:
        self._credentials_ref = credentials_ref

    def needs(self, value: object) -> bool:
        return ConnectionSections.needs_arming(value)

    async def armed(self, param: str, value: object) -> object:
        self._require_static(param, value)

        armed = ArmedValues(
            self._credentials_ref(), NoUserCredential(reason=self.NO_DELEGATION)
        )

        return await armed.arm(value)

    def _require_static(self, param: str, value: object) -> None:
        for profile in ConnectionSections.connections(value):
            section = profile.kerberos_section()
            if isinstance(section, DelegatedAuth):
                msg = (
                    f"injected config {param!r}: profile "
                    f"{type(profile).__name__} carries a delegated kerberos "
                    f"section; {self.NO_DELEGATION}"
                )
                raise ToolConfigError(msg)


class SectionTickets(SectionArming):
    """Реализация SectionArming билетами вызова: профили соединений внутри
    секции плагина находятся по указателям манифеста, разбираются моделью
    своего вида и довооружаются ServiceTickets; остальная секция едет как
    есть.

    Создаёт его загрузчик плагинов из довооружения профилей и реестра типов
    соединений и отдаёт источнику конфига (InjectedConfig) и подготовке
    хранилищ плагинов.
    """

    def __init__(
        self, tickets: ConfigArming, types_ref: Callable[[], ConnectionTypes]
    ) -> None:
        self._tickets = tickets
        self._types_ref = types_ref

    def needs(self, section: PluginSection) -> bool:
        for _connection, profile in self._profiles(section):
            if self._tickets.needs(profile):
                return True

        return False

    async def armed(self, param: str, section: PluginSection) -> Mapping[str, Any]:
        values: dict[str, Any] = dict(section.values)
        for connection, profile in self._profiles(section):
            armed = await self._tickets.armed(param, profile)
            values = self._placed(values, connection.path(), ToolArgv.reveal(armed))

        return values

    def _profiles(
        self, section: PluginSection
    ) -> Iterator[tuple[ConfigConnection, ConnectionBase]]:
        """Профили секции по указателям манифеста; пустой путь — профиля нет."""
        for connection in section.connections:
            raw = self._at(section.values, connection.path())
            if raw is None:
                continue

            yield connection, self._parsed(connection, raw)

    def _parsed(
        self, connection: ConfigConnection, raw: Mapping[str, Any]
    ) -> ConnectionBase:
        try:
            model = self._types_ref().manifest_of(connection.kind).model
        except ConnectionTypesError as exc:
            msg = f"plugin config {connection.pointer}: {exc}"
            raise ToolConfigError(msg) from exc

        try:
            return model.model_validate(dict(raw))
        except ValueError as exc:
            msg = (
                f"plugin config {connection.pointer}: expected a {connection.kind} "
                f"connection profile, it does not parse as {model.__name__}"
            )
            raise ToolConfigError(msg) from exc

    @staticmethod
    def _at(
        values: Mapping[str, Any], path: tuple[str, ...]
    ) -> Mapping[str, Any] | None:
        """Значение по пути; ключа нет или не словарь — None."""
        current: object = values
        for key in path:
            if not isinstance(current, Mapping):
                return None

            current = current.get(key)

        if not isinstance(current, Mapping):
            return None

        return current

    @classmethod
    def _placed(
        cls, values: Mapping[str, Any], path: tuple[str, ...], value: object
    ) -> dict[str, Any]:
        """Копия values со значением value по пути path."""
        copied = dict(values)
        head = path[0]
        if len(path) == 1:
            copied[head] = value
            return copied

        below = copied[head]
        copied[head] = cls._placed(below, path[1:], value)
        return copied
