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
UserConnectionsError — тело инструмента вызвано синхронно: whitelist
    собирается только в async-теле.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import wraps
from typing import ClassVar, NoReturn

import jwt
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict

from boba.chainlit.agent.toolrun.injected import ConfigResolver, ToolConfigError
from boba.chainlit.agent.toolrun.wrapping import AsyncCall, SyncCall, ToolBody
from boba.chainlit.auth.kerberos import KerberosAuth
from boba.chainlit.connections.store import (
    ConnectionKind,
    ConnectionProfile,
    ConnectionStore,
    Subject,
)
from boba.chainlit.connections.whitelist import (
    AmbiguousConnectionError,
    ConnectionKeying,
    ConnectionWhitelist,
)
from boba.chainlit.domain.errors import RefusalError
from boba.chainlit.domain.session import (
    UserMetadataField,
    current_login_user,
    current_user_id,
    current_user_roles,
)
from boba.chainlit.infra.tickets import TicketArming
from boba.krb import CcacheRegistry, KerberosCredentials, TicketConfig
from boba.tool.web.connection import WebConnection
from boba.toolkit.entry import ToolArgv
from boba.toolkit.sql import SqlProfiles
from boba.transport.http import HostPattern, HttpProfile
from chainlit.auth.jwt import decode_jwt

__all__ = [
    "ConnectionRefusal",
    "RegistryRef",
    "SsoLogin",
    "StoreRef",
    "UserConnections",
    "UserConnectionsError",
    "UserConnectionsSpec",
    "UserKerberos",
]

logger = logging.getLogger(__name__)

StoreRef = Callable[[], ConnectionStore]
"""Хранилище соединений; зовётся на вызов, а не при загрузке инструментов."""

RegistryRef = Callable[[], CcacheRegistry | None]
"""Реестр делегированных тикетов; None — SSO kerberos не настроен."""


class UserConnectionsError(Exception):
    """Обвязка поставлена, но тело вызвано путём, где whitelist не собрать."""


class WebArg(StrEnum):
    """Tool-arg'и web-инструментов, которые читает обвязка."""

    URL = "url"


class ConnectionRefusal(StrEnum):
    """Отказы сборки whitelist'а."""

    NO_SESSION = "no_session"
    AMBIGUOUS = "ambiguous_connection"
    NO_DELEGATION = "no_delegated_credentials"
    HOST_NOT_ALLOWED = "host_not_allowed"


@dataclass(frozen=True)
class UserConnectionsSpec:
    """Как секция инструментов адресует соединения: вид и ключ вызова."""

    kind: ConnectionKind
    keying: ConnectionKeying


class SsoLogin(BaseModel):
    """Метки SSO-входа из подписанного JWT: чей тикет и какому входу он выдан."""

    model_config = ConfigDict(frozen=True)

    RETRY_HINT: ClassVar[str] = "retrying will not help until you sign in again"
    """Хвост отказа: агенту незачем повторять вызов, дело в самом входе."""

    principal: str
    login: str

    @classmethod
    def of_session(cls) -> SsoLogin:
        """Вход текущей сессии; строка users тут не при чём — только JWT.

        Ошибки: RefusalError NO_DELEGATION — сессия создана не SSO-входом
        с делегированием; текст называет причину и что сделать.
        """
        user = current_login_user()
        if user is None:
            cls._refuse("this session has no signed sign-in")

        found = cls.of_metadata(user.metadata)
        if not isinstance(found, SsoLogin):
            cls._refuse(found)

        return found

    @classmethod
    def of_token(cls, token: str) -> SsoLogin | None:
        """Метки входа из JWT-cookie; None — не SSO-вход или токен негоден."""
        try:
            user = decode_jwt(token)
        except jwt.PyJWTError:
            return None

        found = cls.of_metadata(user.metadata)
        if not isinstance(found, SsoLogin):
            return None

        return found

    @classmethod
    def of_metadata(cls, metadata: Mapping[str, object]) -> SsoLogin | str:
        """Метки входа либо причина, почему их нет; причина готова для показа."""
        provider = metadata.get(UserMetadataField.PROVIDER)
        if provider != KerberosAuth.__name__:
            return (
                f"you signed in with {cls._provider_name(provider)}, and this "
                "connection acts in the database on your behalf: sign in with "
                "the Kerberos SSO button instead"
            )

        principal = metadata.get(UserMetadataField.PRINCIPAL)
        if not isinstance(principal, str) or not principal:
            return (
                "your Kerberos sign-in predates delegated connections "
                "(the session token names no principal): sign out and sign in again"
            )

        login = metadata.get(UserMetadataField.LOGIN)
        if not isinstance(login, str) or not login:
            return (
                f"the Kerberos sign-in of {principal} carried no delegated ticket: "
                "either Active Directory does not allow this service to act for "
                "you, or the browser sent no ticket; sign in again from a "
                "domain-joined browser"
            )

        return cls(principal=principal, login=login)

    @staticmethod
    def _provider_name(provider: object) -> str:
        if isinstance(provider, str) and provider:
            return provider

        return "no known provider"

    @classmethod
    def _refuse(cls, reason: str) -> NoReturn:
        msg = f"{reason}; {cls.RETRY_HINT}"
        raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg)


class UserKerberos:
    """Делегированные креды SSO-входа текущей сессии.

    Тикет ищется по метке входа из JWT сессии: строка users общая для всех
    способов входа, а JWT подписан приложением и описывает ровно этот вход.
    """

    def __init__(self, registry_ref: RegistryRef) -> None:
        self._registry_ref = registry_ref

    def credentials(self) -> KerberosCredentials:
        sso = SsoLogin.of_session()

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
            msg = (
                f"the delegated Kerberos ticket of {sso.principal} is gone "
                "(the application restarted or you signed out): sign in again; "
                f"{SsoLogin.RETRY_HINT}"
            )
            raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg)

        if credentials.principal != sso.principal:
            msg = (
                f"the delegated ticket belongs to {credentials.principal} while "
                f"this session is {sso.principal}: sign out and sign in again; "
                f"{SsoLogin.RETRY_HINT}"
            )
            raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg)

        return credentials

    def forget(self, token: str) -> None:
        """Logout: тикет входа забывается, даже если JWT ещё не истёк."""
        sso = SsoLogin.of_token(token)
        if sso is None:
            return

        registry = self._registry_ref()
        if registry is None:
            return

        registry.drop(sso.login)


class UserConnections:
    """Обвязка одного инструмента: профили субъекта в injected-конфиг на вызов."""

    def __init__(
        self,
        store_ref: StoreRef,
        kerberos: UserKerberos,
        spec: UserConnectionsSpec,
        param: str,
        base: BaseModel,
    ) -> None:
        self._store_ref = store_ref
        self._kerberos = kerberos
        self._spec = spec
        self._param = param
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
        for tool in tools:
            cls._bind(tool, store_ref, kerberos, spec, resolve)

    @classmethod
    def _bind(
        cls,
        tool: BaseTool,
        store_ref: StoreRef,
        kerberos: UserKerberos,
        spec: UserConnectionsSpec,
        resolve: ConfigResolver,
    ) -> None:
        if not isinstance(tool, StructuredTool):
            return

        schema = tool.args_schema
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            return

        for param, annotation in ToolArgv.injected_fields(schema).items():
            base = resolve(param, annotation)
            if not isinstance(base, SqlProfiles | WebConnection):
                continue

            hook = cls(store_ref, kerberos, spec, param, base)
            ToolBody.wrap_all([tool], hook._wrap, hook._wrap_async)

    def _wrap(self, call: SyncCall, name: str) -> SyncCall:
        @wraps(call)
        def guarded(*args: object, **kwargs: object) -> object:
            msg = f"tool {name!r}: user connections are built in the async body only"
            raise UserConnectionsError(msg)

        return guarded

    def _wrap_async(self, call: AsyncCall, name: str) -> AsyncCall:
        @wraps(call)
        async def guarded(*args: object, **kwargs: object) -> object:
            kwargs[self._param] = await self._config(name, kwargs)
            return await call(*args, **kwargs)

        return guarded

    async def _config(self, name: str, kwargs: dict[str, object]) -> BaseModel:
        subject = self._subject()
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
            shipped[requested] = await self._armed(profile)

        update: dict[str, object] = {
            "profiles": shipped,
            "names": sorted(whitelist.profiles),
        }
        if isinstance(self._base, WebConnection):
            update["hosts"] = self._hosts(whitelist.profiles)

        return self._base.model_copy(update=update)

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

    @staticmethod
    def _subject() -> Subject:
        user_id = current_user_id()
        if not user_id:
            raise RefusalError(ConnectionRefusal.NO_SESSION, "no chainlit user session")

        try:
            numeric = int(user_id)
        except ValueError as exc:
            msg = f"user id {user_id!r} is not the users.id integer"
            raise ToolConfigError(msg) from exc

        return Subject(user_id=numeric, roles=sorted(current_user_roles()))

    async def _armed(self, profile: ConnectionProfile) -> ConnectionProfile:
        """Профиль с билетом вызова вместо kerberos-секции строки."""
        section = TicketArming.section_of(profile)
        if isinstance(section, TicketConfig):
            msg = (
                "stored connection carries a ticket kerberos section: "
                "only delegated or keytab credentials are allowed in the table"
            )
            raise ToolConfigError(msg)

        return await self._arming.arm_profile(profile)
