"""Соединения пользователя в конфиг инструмента перед каждым вызовом.

Whitelist SQL/web-инструмента не лежит в конфиге: на каждый вызов он
собирается из таблицы connections по грантам пользователя и его ролей и
подставляется в injected-параметр вместо статического конфига секции.
В песочницу уезжает профиль только того соединения, которое вызов назвал;
остальные — именами. Kerberos-секцию профиля источник кредов заменяет
билетом вызова: одним сервисным билетом к этому соединению, выпущенным из
делегированных пользователем кредов либо из keytab строки. Тело инструмента
получает готовый whitelist и про пользователя не знает.

Ошибки:
RefusalError — вызов вне сессии chainlit, соединение выдано пользователю
    дважды, хост URL вне web-соединения или делегированных кредов у сессии
    нет; kind из ConnectionRefusal.
ConnectionStoreError — таблица соединений недоступна.
KerberosError — билет к соединению не выпущен, вызов начинать нечем.
ToolConfigError — профиль строки непригоден для песочницы.
InjectedAsyncOnlyError — тело инструмента вызвано синхронно: whitelist
    собирается только в async-теле.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.tickets import CredentialsRef
from boba.connections.credentials import ProfileSections
from boba.transport.http.profile import HostPattern, HttpProfile
from boba.connections.marks import (
    ClientLabel,
    ConnectionRefusal,
    ConnectionTrace,
    UserConnectionsSpec,
)
from boba.connections.profile import ConnectionProfileBase
from boba.transport.http.web import WebConnection
from boba.connections.whitelist import (
    AmbiguousConnectionError,
    ConnectionWhitelist,
)
from boba.identity.context import CallContext
from boba.identity.errors import RefusalError
from boba.kerberos import TicketAuth
from boba.toolkit.sql import SqlProfiles
from boba.toolrun.injected import (
    AsyncInjected,
    ConfigResolver,
    ToolConfigError,
)

__all__ = [
    "ClientLabel",
    "ConnectionRefusal",
    "CredentialsRef",
    "StoreRef",
    "UserConnections",
    "UserConnectionsSpec",
]

logger = logging.getLogger(__name__)

StoreRef = Callable[[], ConnectionStore]
"""Хранилище соединений; зовётся на вызов, а не при загрузке инструментов."""


class WebArg(StrEnum):
    """Tool-arg'и web-инструментов, которые читает обвязка."""

    URL = "url"


class UserConnections(AsyncInjected):
    """Обвязка одного инструмента: профили субъекта в injected-конфиг на вызов."""

    def __init__(
        self,
        store_ref: StoreRef,
        credentials_ref: CredentialsRef,
        spec: UserConnectionsSpec,
        param: str,
        base: BaseModel,
    ) -> None:
        super().__init__(param, base)
        self._store_ref = store_ref
        self._credentials_ref = credentials_ref
        self._spec = spec
        self._base = base

    @classmethod
    def bind_all(
        cls,
        tools: Sequence[BaseTool],
        store_ref: StoreRef,
        credentials_ref: CredentialsRef,
        spec: UserConnectionsSpec,
        resolve: ConfigResolver,
    ) -> None:
        """Ставит обвязку на инструменты, чей injected-конфиг несёт profiles.

        Зовётся до InjectedConfig: injected-поля читаются со схемы, пока их
        с неё не сняли.
        """

        def make(param: str, base: object) -> AsyncInjected:
            if not isinstance(base, BaseModel):
                raise ToolConfigError(f"{param}: injected value is not a model")

            return cls(store_ref, credentials_ref, spec, param, base)

        cls.bind_each(tools, resolve, cls._accepts, make)

    @staticmethod
    def _accepts(base: object) -> bool:
        return isinstance(base, SqlProfiles | WebConnection)

    async def value(self, name: str, kwargs: dict[str, object]) -> object:
        return await self._config(name, kwargs)

    async def _config(self, name: str, kwargs: dict[str, object]) -> BaseModel:
        subject = CallContext.current().subject
        rows = await self._store_ref().for_subject(subject, self._spec.kind)
        whitelist = ConnectionWhitelist.of(rows, self._spec.keying)

        requested = self._spec.keying.requested(kwargs)
        try:
            picked = whitelist.pick(requested)
        except AmbiguousConnectionError as exc:
            msg = (
                f"connection {requested!r} matches more than one of your "
                "connections; ask the administrator to resolve the overlap"
            )
            raise RefusalError(ConnectionRefusal.AMBIGUOUS, msg) from exc

        shipped: dict[str, ConnectionProfileBase] = {}
        if picked is not None:
            profile = self._at_host(requested, picked.profile, kwargs)
            armed = await self._armed(self._labelled(profile, name))
            shipped[requested] = armed
            logger.info(
                "tool %s: connection %r (%s) %s",
                name,
                requested,
                self._spec.kind,
                ConnectionTrace.of(armed),
            )

        update: dict[str, object] = {
            "profiles": shipped,
            "names": sorted(whitelist.profiles),
        }
        if isinstance(self._base, WebConnection):
            update["hosts"] = self._hosts(whitelist.profiles)

        return self._base.model_copy(update=update)

    @staticmethod
    def _labelled(profile: ConnectionProfileBase, tool: str) -> ConnectionProfileBase:
        """Профиль с меткой клиента: логин субъекта вызова."""
        login = CallContext.current().subject.login

        return ClientLabel.of(login, tool).applied(profile)

    @staticmethod
    def _at_host(
        name: str, profile: ConnectionProfileBase, kwargs: Mapping[str, object]
    ) -> ConnectionProfileBase:
        """Web-профиль привязывается к хосту URL вызова; чужой хост — отказ.

        Билет negotiate выпускается к реальному хосту, поэтому привязка идёт
        до арминга; тело проверит то же самое ещё раз.
        """
        if not isinstance(profile, HttpProfile):
            return profile

        url = kwargs.get(WebArg.URL)
        if not isinstance(url, str):
            return profile

        host = HostPattern.host_of(url)
        if not profile.covers(host):
            msg = (
                f"host {host!r} is outside connection {name!r} "
                f"(it covers {profile.host()!r})"
            )
            raise RefusalError(ConnectionRefusal.HOST_NOT_ALLOWED, msg)

        return profile.bound_to(host)

    @staticmethod
    def _hosts(profiles: Mapping[str, ConnectionProfileBase]) -> dict[str, str]:
        hosts: dict[str, str] = {}
        for name, profile in profiles.items():
            if isinstance(profile, HttpProfile):
                hosts[name] = profile.host()

        return hosts

    async def _armed(self, profile: ConnectionProfileBase) -> ConnectionProfileBase:
        """Профиль с билетом вызова вместо kerberos-секции строки."""
        section = ProfileSections.section_of(profile)
        if isinstance(section, TicketAuth):
            msg = (
                "stored connection carries a ticket kerberos section: "
                "only delegated or keytab credentials are allowed in the table"
            )
            raise ToolConfigError(msg)

        credential = CallContext.current().credential

        return await self._credentials_ref().for_connection(profile, credential)
