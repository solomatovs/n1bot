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
from boba.chainlit.domain.context import CallContext, DelegatedTicket
from boba.chainlit.domain.errors import RefusalError
from boba.chainlit.domain.session import SsoMarks
from boba.chainlit.infra.session import current_session
from boba.chainlit.infra.tickets import TicketArming
from boba.db.clickhouse import ClickHouseConfig
from boba.db.postgres import PostgresConfig
from boba.krb import (
    CcacheRegistry,
    DelegatedAuth,
    DelegatedCredentials,
    KerberosCredentials,
    RefreshWaiters,
    TicketAuth,
)
from boba.tool.web.connection import WebConnection
from boba.toolkit.entry import ToolArgv
from boba.toolkit.sql import SqlProfiles
from boba.transport.http import HostPattern, HttpProfile
from chainlit.auth.jwt import decode_jwt

__all__ = [
    "ClientLabel",
    "ConnectionRefusal",
    "KerberosRefreshSignal",
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
    def of_context(cls) -> SsoLogin:
        """Вход субъекта текущего вызова по ссылке на билет из контекста.

        Ошибки: RefusalError NO_DELEGATION — у вызова нет делегированного
        билета; текст называет причину и что сделать.
        """
        context = CallContext.current()
        credential = context.credential
        if not isinstance(credential, DelegatedTicket):
            logger.warning(
                "kerberos: %s asked for a delegated ticket without one: %s",
                context.subject.login,
                credential.reason,
            )
            cls._refuse(credential.reason)

        return cls(principal=credential.principal, login=credential.sso_login)

    @classmethod
    def of_token(cls, token: str) -> SsoLogin | None:
        """Метки входа из JWT-cookie; None — не SSO-вход или токен негоден."""
        try:
            user = decode_jwt(token)
        except jwt.PyJWTError:
            return None

        marks = SsoMarks.of_metadata(user.metadata)
        if marks is None:
            return None

        return cls(principal=marks.principal, login=marks.login)

    @classmethod
    def _refuse(cls, reason: str) -> NoReturn:
        msg = f"{reason}; {cls.RETRY_HINT}"
        raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg)


class ConnectionTrace:
    """Как соединение выглядит в журнале: способ авторизации и под кем идём.

    Пишется по профилю, который уже уехал бы в песочницу, поэтому у
    делегированных строк здесь виден выпущенный билет вызова, а не строка
    таблицы.
    """

    @staticmethod
    def of(profile: ConnectionProfile) -> str:
        if isinstance(profile, HttpProfile):
            return f"{profile.auth.trace()} url={profile.base_url}"

        return profile.auth.trace()


class LoginMark:
    """Метка входа в журнале: сама метка — ключ к тикету, целиком её не пишем."""

    KEEP: ClassVar[int] = 8

    @classmethod
    def of(cls, login: str) -> str:
        if len(login) <= cls.KEEP:
            return login

        return f"{login[: cls.KEEP]}…"


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

        try:
            return await current_session().emit(cls.EVENT, payload)
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

    def __init__(self, registry_ref: RegistryRef) -> None:
        self._registry_ref = registry_ref

    async def ensure_fresh(self) -> None:
        """Обновляет тикет входа молчаливым SPNEGO, пока сессия жива.

        Тикет входа короче сессии: constrained-креды не продлеваются, а JWT
        живёт дальше. Вместо повторного логина браузер домена проходит обмен
        ещё раз — незаметно для пользователя. Не получилось — работу продолжит
        credentials() и объяснит отказ.
        """
        sso = SsoLogin.of_context()

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
        sso = SsoLogin.of_context()

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

        logger.info(
            "kerberos: tool acts as %s [sign-in %s] [ticket %ds]",
            credentials.principal,
            LoginMark.of(sso.login),
            credentials.lifetime(),
        )
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


class ClientLabel(BaseModel):
    """Метка соединения для сервера: приложение, логин пользователя, инструмент.

    Уходит в application_name postgres и client_name clickhouse, поэтому режется
    до 63 байт — предела application_name.
    """

    model_config = ConfigDict(frozen=True)

    MAX_BYTES: ClassVar[int] = 63
    SEPARATOR: ClassVar[str] = ":"
    APPLICATION: ClassVar[str] = "boba"

    application: str
    login: str
    tool: str

    @classmethod
    def of(cls, login: str, tool: str) -> ClientLabel:
        return cls(application=cls.APPLICATION, login=login, tool=tool)

    def render(self) -> str:
        joined = self.SEPARATOR.join((self.application, self.login, self.tool))
        raw = joined.encode("utf-8")
        if len(raw) <= self.MAX_BYTES:
            return joined

        return raw[: self.MAX_BYTES].decode("utf-8", errors="ignore")

    def applied(self, profile: ConnectionProfile) -> ConnectionProfile:
        """Профиль с меткой в поле, которым сервер подписывает сессию."""
        if isinstance(profile, PostgresConfig):
            return profile.model_copy(update={"application_name": self.render()})

        if isinstance(profile, ClickHouseConfig):
            return profile.model_copy(update={"client_name": self.render()})

        return profile


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

    @staticmethod
    def _subject() -> Subject:
        subject = CallContext.current().subject

        return Subject(user_id=subject.user_id, roles=sorted(subject.roles))

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
