"""Соединения пользователя в конфиг инструмента перед каждым вызовом.

Whitelist SQL/web-инструмента не лежит в конфиге: на каждый вызов он
собирается из таблицы connections по грантам пользователя и его ролей и
подставляется в injected-параметр вместо статического конфига секции.
В песочницу уезжает профиль только того соединения, которое вызов назвал;
остальные — именами. Kerberos-секция профиля заменяется билетом вызова
(TicketArming): одним сервисным билетом к этому соединению, выпущенным из
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
from abc import abstractmethod
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import ClassVar, Protocol

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from boba.connection_broker.store import (
    ConnectionProfile,
    ConnectionStore,
)
from boba.connection_broker.tickets import TicketArming
from boba.connections.http import HostPattern, HttpProfile
from boba.connections.kerberos import DelegatedAuth, TicketAuth
from boba.connections.marks import (
    ClientLabel,
    ConnectionRefusal,
    ConnectionTrace,
    UserConnectionsSpec,
)
from boba.connections.web import WebConnection
from boba.connections.whitelist import (
    AmbiguousConnectionError,
    ConnectionWhitelist,
)
from boba.identity.context import CallContext, Credential, DelegatedTicket
from boba.identity.errors import RefusalError
from boba.krb import (
    KerberosCredentials,
    SignInTicket,
    TicketSealError,
)
from boba.krb.seal import SsoTickets
from boba.toolkit.sql import SqlProfiles
from boba.toolrun.injected import (
    AsyncInjected,
    ConfigResolver,
    ToolConfigError,
)

__all__ = [
    "ClientLabel",
    "ConnectionRefusal",
    "RefreshSignal",
    "StoreRef",
    "TicketsRef",
    "UserConnections",
    "UserConnectionsSpec",
    "UserKerberos",
]

logger = logging.getLogger(__name__)

StoreRef = Callable[[], ConnectionStore]
"""Хранилище соединений; зовётся на вызов, а не при загрузке инструментов."""

TicketsRef = Callable[[], SsoTickets | None]
"""Открыватель билетов входа; None — SSO kerberos не настроен."""


class WebArg(StrEnum):
    """Tool-arg'и web-инструментов, которые читает обвязка."""

    URL = "url"


class RefreshSignal(Protocol):
    """Просьба к фронту молча пройти SPNEGO ещё раз; реализация — у приложения."""

    @abstractmethod
    async def send(self) -> bool:
        """True — сигнал ушёл живому слушателю; False — слушать некому."""


class UserKerberos:
    """Делегированные креды SSO-входа текущего вызова.

    Билет лежит запечатанным в JWT входа: строка users общая для всех способов
    входа, а JWT подписан приложением и описывает ровно этот вход. Процесс
    ничего не хранит — любой процесс с тем же секретом откроет билет.
    """

    REFRESH_BELOW: ClassVar[int] = 300
    """Остаток тикета входа (сек), ниже которого просим браузер обменяться заново."""

    RETRY_HINT: ClassVar[str] = "retrying will not help until you sign in again"
    """Хвост отказа: агенту незачем повторять вызов, дело в самом входе."""

    def __init__(self, tickets_ref: TicketsRef, refresh: RefreshSignal) -> None:
        self._tickets_ref = tickets_ref
        self._refresh = refresh

    @classmethod
    def _ticket(cls) -> DelegatedTicket:
        """Ссылка на билет субъекта текущего вызова; без неё — NO_DELEGATION."""
        context = CallContext.current()

        return cls.ticket_of(context.credential, context.subject.login)

    async def ensure_fresh(self) -> None:
        """Просит браузер обновить билет входа, когда тот на исходе.

        Обмен идёт молча и кладёт в сессию новый JWT; ждать его вызов не
        обязан — пока билет жив, работает текущий, а истёкший объяснит
        credentials().
        """
        sso = self._ticket()
        tickets = self._tickets_ref()
        if tickets is None:
            return

        ticket = self._opened(tickets, sso)
        if ticket.lifetime() >= self.REFRESH_BELOW:
            return

        logger.info(
            "kerberos: sign-in ticket of %s has %ds left, asking the browser",
            sso.principal,
            ticket.lifetime(),
        )
        if not await self._refresh.send():
            logger.info("kerberos: nobody is listening for the refresh signal")

    def credentials(self) -> KerberosCredentials:
        return self.open_credentials(self._ticket(), self._tickets_ref())

    @classmethod
    def open_credentials(
        cls, sso: DelegatedTicket, tickets: SsoTickets | None
    ) -> KerberosCredentials:
        """Креды по билету входа; отказ — RefusalError NO_DELEGATION с причиной."""
        if tickets is None:
            msg = (
                "this connection acts on your behalf, but Kerberos SSO is not "
                "configured in this deployment: ask the administrator for a "
                "connection with its own credentials"
            )
            raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg)

        ticket = cls._opened(tickets, sso)
        if ticket.principal != sso.principal:
            msg = (
                f"the delegated ticket belongs to {ticket.principal} while "
                f"this session is {sso.principal}: sign out and sign in again; "
                f"{cls.RETRY_HINT}"
            )
            raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg)

        logger.info(
            "kerberos: acting as %s [ticket %ds]", ticket.principal, ticket.lifetime()
        )

        return tickets.credentials_of(ticket)

    @classmethod
    def ticket_of(cls, credential: Credential, login: str) -> DelegatedTicket:
        """Билет из секретов субъекта; без него — NO_DELEGATION с причиной."""
        if isinstance(credential, DelegatedTicket):
            return credential

        logger.warning(
            "kerberos: %s asked for a delegated ticket without one: %s",
            login,
            credential.reason,
        )
        msg = f"{credential.reason}; {cls.RETRY_HINT}"
        raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg)

    @classmethod
    def _opened(cls, tickets: SsoTickets, sso: DelegatedTicket) -> SignInTicket:
        try:
            return tickets.open(sso.sealed)
        except TicketSealError as exc:
            msg = (
                f"the delegated Kerberos ticket in the session of {sso.principal} "
                "does not open (the application secret changed?): sign in again; "
                f"{cls.RETRY_HINT}"
            )
            raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg) from exc


class UserConnections(AsyncInjected):
    """Обвязка одного инструмента: профили субъекта в injected-конфиг на вызов."""

    def __init__(
        self,
        store_ref: StoreRef,
        kerberos: UserKerberos,
        spec: UserConnectionsSpec,
        param: str,
        base: BaseModel,
    ) -> None:
        super().__init__(param, base)
        self._store_ref = store_ref
        self._kerberos = kerberos
        self._spec = spec
        self._base = base
        self._arming = TicketArming(kerberos.credentials)

    @classmethod
    def bind_all(  # noqa: PLR0913 — обвязка собирается всеми зависимостями сразу
        cls,
        tools: Sequence[BaseTool],
        store_ref: StoreRef,
        tickets_ref: TicketsRef,
        spec: UserConnectionsSpec,
        resolve: ConfigResolver,
        refresh: RefreshSignal,
    ) -> None:
        """Ставит обвязку на инструменты, чей injected-конфиг несёт profiles.

        Зовётся до InjectedConfig: injected-поля читаются со схемы, пока их
        с неё не сняли.
        """
        kerberos = UserKerberos(tickets_ref, refresh)

        def make(param: str, base: object) -> AsyncInjected:
            if not isinstance(base, BaseModel):
                raise ToolConfigError(f"{param}: injected value is not a model")

            return cls(store_ref, kerberos, spec, param, base)

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

        shipped: dict[str, ConnectionProfile] = {}
        if picked is not None:
            profile = self._at_host(requested, picked.profile, kwargs)
            armed = await self._armed(self._labelled(profile, name))
            shipped[requested] = armed
            logger.info(
                "tool %s: connection %r (%s) %s",
                name,
                requested,
                self._spec.kind.value,
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
    def _labelled(profile: ConnectionProfile, tool: str) -> ConnectionProfile:
        """Профиль с меткой клиента: логин субъекта вызова."""
        login = CallContext.current().subject.login

        return ClientLabel.of(login, tool).applied(profile)

    @staticmethod
    def _at_host(
        name: str, profile: ConnectionProfile, kwargs: Mapping[str, object]
    ) -> ConnectionProfile:
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
    def _hosts(profiles: Mapping[str, ConnectionProfile]) -> dict[str, str]:
        hosts: dict[str, str] = {}
        for name, profile in profiles.items():
            if isinstance(profile, HttpProfile):
                hosts[name] = profile.host()

        return hosts

    async def _armed(self, profile: ConnectionProfile) -> ConnectionProfile:
        """Профиль с билетом вызова вместо kerberos-секции строки."""
        section = TicketArming.section_of(profile)
        if isinstance(section, DelegatedAuth):
            await self._kerberos.ensure_fresh()

        if isinstance(section, TicketAuth):
            msg = (
                "stored connection carries a ticket kerberos section: "
                "only delegated or keytab credentials are allowed in the table"
            )
            raise ToolConfigError(msg)

        return await self._arming.arm_profile(profile)
