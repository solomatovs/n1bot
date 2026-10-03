"""Билет вызова вместо keytab-секции в статическом конфиге инструмента.

Конфиг секции (kb, ingest) несёт keytab строки; в песочницу с ним уезжает
сервисный билет к SPN соединения, выпущенный источником кредов перед этим
самым вызовом. Делегированная секция в статическом конфиге — ошибка конфига:
делегировать тут некому.

Ошибки:
KerberosError — билет к соединению не выпущен, вызов начинать нечем.
ToolConfigError — секция требует делегирования, а источника кредов нет.
InjectedAsyncOnlyError — тело инструмента вызвано синхронно: билет выпускается
    только в async-теле.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from boba.connections.credentials import (
    ArmedValues,
    ConnectionSections,
    CredentialSource,
)
from boba.identity.context import NoUserCredential
from boba.kerberos import DelegatedAuth
from boba.toolrun.injected import ConfigArming, ToolConfigError

__all__ = ["CredentialsRef", "ServiceTickets"]

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
