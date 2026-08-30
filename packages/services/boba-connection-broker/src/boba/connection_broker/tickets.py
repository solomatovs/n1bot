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

from collections.abc import Callable, Sequence
from typing import ClassVar

from langchain_core.tools import BaseTool

from boba.connections.credentials import ArmedValues, CredentialSource, ProfileSections
from boba.connections.kerberos import DelegatedAuth
from boba.identity.context import NoUserCredential
from boba.toolrun.injected import (
    AsyncInjected,
    ConfigResolver,
    ToolConfigError,
)

__all__ = ["CredentialsRef", "ServiceTickets"]

CredentialsRef = Callable[[], CredentialSource]
"""Источник кредов вызова; зовётся на вызов, а не при загрузке инструментов."""


class ServiceTickets(AsyncInjected):
    """Обвязка секции: статический injected-конфиг с keytab едет билетом вызова."""

    NO_DELEGATION: ClassVar[str] = (
        "a delegated kerberos section needs a user session; "
        "service configs must carry keytab credentials"
    )

    def __init__(
        self, credentials_ref: CredentialsRef, param: str, base: object
    ) -> None:
        super().__init__(param, base)
        self._credentials_ref = credentials_ref

    @classmethod
    def bind_all(
        cls,
        tools: Sequence[BaseTool],
        credentials_ref: CredentialsRef,
        resolve: ConfigResolver,
    ) -> None:
        """Ставит обвязку на инструменты, чей injected-конфиг несёт kerberos-секцию.

        Зовётся до InjectedConfig: injected-поля читаются со схемы, пока их
        с неё не сняли.
        """

        def make(param: str, base: object) -> AsyncInjected:
            return cls(credentials_ref, param, base)

        cls.bind_each(tools, resolve, ProfileSections.needs_arming, make)

    async def value(self, name: str, kwargs: dict[str, object]) -> object:
        self._require_static()

        armed = ArmedValues(
            self._credentials_ref(), NoUserCredential(reason=self.NO_DELEGATION)
        )

        return await armed.arm(self._base)

    def _require_static(self) -> None:
        for profile in ProfileSections.profiles(self._base):
            section = ProfileSections.section_of(profile)
            if isinstance(section, DelegatedAuth):
                raise ToolConfigError(self.NO_DELEGATION)
