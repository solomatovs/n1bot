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
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import ClassVar

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from boba.chainlit.agent.toolrun.injected import (
    AsyncInjected,
    ConfigResolver,
    ToolConfigError,
)
from boba.chainlit.connections.store import (
    ConnectionProfile,
    ConnectionStore,
)
from boba.chainlit.domain.context import ChatCallContext
from boba.chainlit.infra.session import ChainlitSession
from boba.chainlit.infra.tickets import TicketArming
from boba.connections.http import HostPattern, HttpProfile
from boba.connections.kerberos import DelegatedAuth, TicketAuth
from boba.connections.marks import (
    ClientLabel,
    ConnectionRefusal,
    ConnectionTrace,
    LoginMark,
    UserConnectionsSpec,
)
from boba.connections.web import WebConnection
from boba.connections.whitelist import (
    AmbiguousConnectionError,
    ConnectionWhitelist,
)
from boba.identity.context import CallContext, DelegatedTicket
from boba.identity.errors import RefusalError
from boba.krb import (
    CcacheRegistry,
    DelegatedCredentials,
    KerberosCredentials,
    RefreshWaiters,
)
from boba.toolkit.sql import SqlProfiles

__all__ = [
    "ClientLabel",
    "ConnectionRefusal",
    "KerberosRefreshSignal",
    "RegistryRef",
    "StoreRef",
    "UserConnections",
    "UserConnectionsSpec",
    "UserKerberos",
]

logger = logging.getLogger(__name__)

StoreRef = Callable[[], ConnectionStore]
"""Хранилище соединений; зовётся на вызов, а не при загрузке инструментов."""

RegistryRef = Callable[[], CcacheRegistry | None]
"""Реестр делегированных тикетов; None — SSO kerberos не настроен."""


class WebArg(StrEnum):
    """Tool-arg'и web-инструментов, которые читает обвязка."""

    URL = "url"


class KerberosRefreshSignal:
    """Просьба к фронту молча пройти SPNEGO ещё раз: сигнал в сокет сессии.

    Адрес обмена знает сам скрипт страницы: сервер сообщает только повод.
    """

    TYPE: ClassVar[str] = "boba:kerberos-refresh"
    EVENT: ClassVar[str] = "window_message"

    @classmethod
    async def send(cls) -> bool:
        """True — сигнал ушёл в живой сокет; False — слушать некому."""
        payload = {"type": cls.TYPE}

        context = CallContext.current()
        if not isinstance(context, ChatCallContext):
            return False

        try:
            return await context.surface.emit(cls.EVENT, payload)
        except Exception:
            logger.warning("kerberos refresh signal failed", exc_info=True)
            return False


class UserKerberos:
    """Делегированные креды SSO-входа текущей сессии.

    Тикет ищется по метке входа из JWT сессии: строка users общая для всех
    способов входа, а JWT подписан приложением и описывает ровно этот вход.
    """

    REFRESH_BELOW: ClassVar[int] = 300
    """Остаток тикета входа (сек), ниже которого просим браузер обменяться заново."""

    RETRY_HINT: ClassVar[str] = "retrying will not help until you sign in again"
    """Хвост отказа: агенту незачем повторять вызов, дело в самом входе."""

    def __init__(self, registry_ref: RegistryRef) -> None:
        self._registry_ref = registry_ref

    @classmethod
    def _ticket(cls) -> DelegatedTicket:
        """Ссылка на билет субъекта текущего вызова; без неё — NO_DELEGATION."""
        context = CallContext.current()
        credential = context.credential
        if isinstance(credential, DelegatedTicket):
            return credential

        logger.warning(
            "kerberos: %s asked for a delegated ticket without one: %s",
            context.subject.login,
            credential.reason,
        )
        msg = f"{credential.reason}; {cls.RETRY_HINT}"
        raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg)

    async def ensure_fresh(self) -> None:
        """Обновляет тикет входа молчаливым SPNEGO, пока сессия жива.

        Тикет входа короче сессии: constrained-креды не продлеваются, а JWT
        живёт дальше. Вместо повторного логина браузер домена проходит обмен
        ещё раз — незаметно для пользователя. Не получилось — работу продолжит
        credentials() и объяснит отказ.
        """
        sso = self._ticket()

        registry = self._registry_ref()
        if registry is None:
            return

        if self._enough(registry.of_login(sso.login)):
            return

        logger.info(
            "kerberos: sign-in %s has no fresh ticket, asking the browser",
            LoginMark.of(sso.login),
        )

        # ожидание заводится до просьбы: обмен может пройти быстрее нас
        with registry.arm_refresh(sso.login) as waiting:
            if not await KerberosRefreshSignal.send():
                logger.info("kerberos: nobody is listening for the refresh signal")
                return

            refreshed = await waiting.wait(RefreshWaiters.TIMEOUT_SEC)

        if refreshed:
            logger.info("kerberos: sign-in %s refreshed its ticket", sso.principal)
            return

        logger.info("kerberos: sign-in %s did not refresh in time", sso.principal)

    @classmethod
    def _enough(cls, credentials: DelegatedCredentials | None) -> bool:
        """Хватит ли тикета входа на вызов; чужие креды сюда не попадают."""
        if credentials is None:
            return False

        return credentials.lifetime() >= cls.REFRESH_BELOW

    def credentials(self) -> KerberosCredentials:
        sso = self._ticket()

        registry = self._registry_ref()
        if registry is None:
            msg = (
                "this connection acts on your behalf, but Kerberos SSO is not "
                "configured in this deployment: ask the administrator for a "
                "connection with its own credentials"
            )
            raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg)

        credentials = registry.of_login(sso.login)
        if credentials is None:
            logger.warning(
                "kerberos: session of %s asks for sign-in %s, registry holds %s",
                sso.principal,
                LoginMark.of(sso.login),
                [LoginMark.of(login) for login in registry.logins()],
            )
            msg = (
                f"the delegated Kerberos ticket of {sso.principal} is gone "
                "(the application restarted or you signed out): sign in again; "
                f"{self.RETRY_HINT}"
            )
            raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg)

        if credentials.principal != sso.principal:
            msg = (
                f"the delegated ticket belongs to {credentials.principal} while "
                f"this session is {sso.principal}: sign out and sign in again; "
                f"{self.RETRY_HINT}"
            )
            raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg)

        logger.info(
            "kerberos: tool acts as %s [sign-in %s] [ticket %ds]",
            credentials.principal,
            LoginMark.of(sso.login),
            credentials.lifetime(),
        )
        return credentials

    def forget(self, token: str) -> None:
        """Logout: тикет входа забывается, даже если JWT ещё не истёк."""
        sso = ChainlitSession.ticket_of_token(token)
        if sso is None:
            return

        registry = self._registry_ref()
        if registry is None:
            return

        registry.drop(sso.login)


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
    def bind_all(
        cls,
        tools: Sequence[BaseTool],
        store_ref: StoreRef,
        registry_ref: RegistryRef,
        spec: UserConnectionsSpec,
        resolve: ConfigResolver,
    ) -> None:
        """Ставит обвязку на инструменты, чей injected-конфиг несёт profiles.

        Зовётся до InjectedConfig: injected-поля читаются со схемы, пока их
        с неё не сняли.
        """
        kerberos = UserKerberos(registry_ref)

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
