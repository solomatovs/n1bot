"""SSO через Kerberos/SPNEGO для chainlit.

Ошибки: AuthenticationError, AuthorizationError — отказ входа;
ExternalServiceError — недоступен внешний сервис (KDC, LDAP);
InternalServiceError — keytab/SPN/конфиг непригодны.
"""

import asyncio
import base64
import html
import logging
import os
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol

from fastapi import FastAPI
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

import chainlit as cl
from boba.chainlit.auth.ldap import ADDirectory
from boba.chainlit.infra.session import ChainlitSession, ChainlitSessions
from boba.identity.context import DelegatedTicket
from boba.identity.directory import (
    ADUserEntry,
    LDAPError,
    LDAPInvalidCredentialsError,
    LDAPServerUnavailableError,
    LDAPUserNotFoundError,
)
from boba.identity.errors import (
    AuthenticationError,
    AuthorizationError,
    BaseError,
    ExternalServiceError,
    FailureReport,
    InternalServiceError,
)
from boba.identity.roles import (
    DnExcludeUserProvider,
    DnUserRolesProvider,
    LocalExcludeUserProvider,
    LocalUserRolesProvider,
    MemberOfExcludeUserProvider,
    MemberOfUserRolesProvider,
    RoleExcludeConfig,
    RoleMappingConfig,
    SAMAccountNameExcludeUserProvider,
    SAMAccountNameUserRolesProvider,
)
from boba.identity.session import (
    LoginTemplate,
    LogLine,
    SignInProvider,
    UserLogin,
    UserMetadataField,
)
from boba.identity.sso import SidsHeader, SsoAdmission
from boba.krb import (
    CredentialsExpiredError,
    DelegationNotPermittedError,
    InvalidTokenError,
    KerberosError,
    KeytabError,
    SpnegoAcceptor,
    SpnegoIdentity,
    TicketCapture,
)
from boba.krb.seal import SsoTickets, TicketSealer
from boba.runtime.auth_config import (
    KerberosAuthConfig,
    KerberosRolesInLdapConfig,
)
from boba.toolkit.template import TemplateError
from chainlit.auth import create_jwt, set_auth_cookie
from chainlit.config import config as cl_config


class ButtonJsVar(StrEnum):
    """Плейсхолдеры sso_button.js, которые сервер заменяет на URL."""

    SSO_URL = "__SSO_URL__"
    REFRESH_URL = "__REFRESH_URL__"
    REFRESH_HEADER = "__REFRESH_HEADER__"
    REFRESH_HEADER_VALUE = "__REFRESH_HEADER_VALUE__"
    TRANSLATIONS_URL = "__TRANSLATIONS_URL__"


class SsoLoginError(StrEnum):
    """Коды исхода SSO для страницы логина chainlit: auth.login.errors.<code>."""

    TICKET = "sso_ticket"
    DENIED = "sso_denied"
    FAILED = "sso_failed"

    def login_url(self, login_url: str) -> str:
        return f"{login_url}?error={self.value}"

    def challenge_page(self, login_url: str) -> str:
        """Тело 401: с тикетом браузер повторит запрос сам, без него уйдёт на логин."""
        url = html.escape(self.login_url(login_url), quote=True)

        return f'<!doctype html><meta http-equiv="refresh" content="0;url={url}">'


class SsoUrls(BaseModel):
    """Адреса SSO с учётом url_prefix: роут, обновление, скрипт, логин, чат."""

    sso: str
    refresh: str
    js: str
    translations: str
    login: str
    app: str

    @classmethod
    def of(cls, url_prefix: str, sso_path: str) -> "SsoUrls":
        sso = f"{url_prefix}{sso_path}"

        return cls(
            sso=sso,
            refresh=f"{sso}/refresh",
            js=f"{sso}.js",
            translations=f"{url_prefix}/project/translations",
            login=f"{url_prefix}/login",
            app=f"{url_prefix}/",
        )


class SsoRefresh(StrEnum):
    """Признак того, что обмен запросила своя страница, а не чужой сайт."""

    HEADER = "x-boba-sso-refresh"
    VALUE = "1"

    @classmethod
    def asked(cls, headers: Headers) -> bool:
        """Заголовок ставит только свой fetch: кросс-сайтовый запрос его не несёт."""
        return headers.get(cls.HEADER) == cls.VALUE


class KerberosErrorToDomain:
    "Классификация ошибок kerberos-слоя в доменную ошибку."

    @staticmethod
    def map(e: KerberosError) -> BaseError:
        # нужен повторный логин — не наша вина
        if isinstance(e, CredentialsExpiredError):
            return ExternalServiceError(
                "kerberos", "Kerberos credentials expired, please re-login"
            )

        # делегирование запрещено политикой (msDS-AllowedToDelegateTo) — конфиг AD
        if isinstance(e, DelegationNotPermittedError):
            return InternalServiceError(
                internal_detail=f"kerberos delegation not permitted: {e}",
                user_detail=None,
            )

        # keytab/SPN сервиса непригодны — наша сторона
        if isinstance(e, KeytabError):
            return InternalServiceError(
                internal_detail=f"kerberos keytab problem: {e}",
                user_detail=None,
            )

        return InternalServiceError(
            internal_detail=f"kerberos error: {e}",
            user_detail=None,
        )


class SidUserRolesProvider:
    """Мапер SID группы - список ролей"""

    def __init__(self, mapping: RoleMappingConfig):
        self._mapping = mapping

    def roles_of(self, sids: list[str]) -> Iterable[str]:
        for s in sids:
            yield from self._mapping.roles_of(s)


class SidExcludeUserProvider:
    """Список SID групп, членам которых запрещён вход"""

    def __init__(self, mapping: RoleExcludeConfig):
        self._mapping = mapping

    def exclude_of(self, sids: list[str]) -> Iterable[bool]:
        for s in sids:
            yield from self._mapping.exclude_of(s)


class SsoPass:
    """Запечатанный билет входа от middleware к роуту: через state запроса."""

    KEY: ClassVar[str] = "sso_ticket"

    @classmethod
    def put(cls, scope: Scope, sealed: str) -> None:
        state = scope.setdefault("state", {})
        state[cls.KEY] = sealed

    @classmethod
    def take(cls, request: Request) -> str:
        """Пустая строка — middleware билета не приложила."""
        sealed = getattr(request.state, cls.KEY, "")
        if not isinstance(sealed, str):
            return ""

        return sealed


class SsoUserBuilder(Protocol):
    """Пользователь chainlit после (повторного) SPNEGO: роли и билет входа."""

    @abstractmethod
    async def refreshed_user(
        self, principal: str, group_sids: Sequence[str], sealed: str
    ) -> cl.User: ...


@dataclass(frozen=True)
class SsoRuntime:
    """Части SSO, которыми работает middleware: адреса, приём, делегирование, допуск."""

    urls: SsoUrls
    config: KerberosAuthConfig
    acceptor: SpnegoAcceptor
    capture: TicketCapture
    sealer: TicketSealer
    admission: SsoAdmission
    builder: SsoUserBuilder


class SpnegoMiddleware:
    "SPNEGO-accept на /auth/sso и молчаливый повторный обмен на /auth/sso/refresh"

    def __init__(self, app: ASGIApp, *, sso: SsoRuntime) -> None:
        self._app = app
        self._urls = sso.urls
        self._config = sso.config
        self._acceptor = sso.acceptor
        self._capture = sso.capture
        self._sealer = sso.sealer
        self._builder = sso.builder
        self._admission = sso.admission
        self._negotiate = {"WWW-Authenticate": "Negotiate"}
        self._logger = logging.getLogger(SpnegoMiddleware.__name__)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):  # noqa: PLR0911
        if scope["type"] != "http":
            return await self._app(scope, receive, send)

        path = scope.get("path")
        if not isinstance(path, str):
            return await self._app(scope, receive, send)

        path = path.removesuffix("/").lower()

        if path == self._urls.refresh:
            return await self._refreshed(scope, receive, send)

        if path != self._urls.sso:
            return await self._app(scope, receive, send)

        headers = Headers(scope=scope).mutablecopy()
        client = self._client(headers, scope)

        found = self._token_of(headers)
        if isinstance(found, str):
            # начало handshake токена не несёт — это не ошибка
            return await self._challenge(scope, receive, send, found, logging.INFO)

        token = found

        try:
            identity: SpnegoIdentity = await self._acceptor.accept_async(token)
        except InvalidTokenError as e:
            # проблема клиента: битый, просроченный или неполный токен
            return await self._challenge(scope, receive, send, str(e))
        except KerberosError as e:
            # проблема сервера: keytab/SPN
            self._logger.exception(
                "kerberos: spnego accept failed (keytab/SPN) [client=%s]", client
            )
            raise KerberosErrorToDomain.map(e) from e

        self._logger.info(
            "kerberos authenticated [principal=%s] [client=%s]",
            identity.principal,
            client,
        )

        sealed = self._sealed(identity)
        header = self._config.header.lower()
        headers[header] = identity.principal
        # заголовок с SID-ами перетираем всегда — клиентское значение не пройдёт
        headers[self._config.sids_header.lower()] = SidsHeader.render(
            identity.group_sids
        )
        scope["headers"] = headers.raw
        SsoPass.put(scope, sealed)
        self._logger.info(
            "kerberos: sign-in ticket passed to the app [%d chars] [path=%s]",
            len(sealed),
            path,
        )
        return await self._app(scope, receive, send)

    def _sealed(self, identity: SpnegoIdentity) -> str:
        """Запечатанный билет входа; пустая строка — делегирования у входа нет."""
        ticket = self._capture.capture(identity)
        if ticket is None:
            return ""

        return self._sealer.seal(ticket)

    async def _refreshed(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Повторный SPNEGO живой сессии: свежие креды под её же меткой входа.

        JWT остаётся прежним: обновляется только тикет, на который он ссылается.
        """
        headers = Headers(scope=scope)
        client = self._client(headers, scope)

        allowed = self._refresh_marks(headers, client)
        if isinstance(allowed, str):
            await self._refusal(scope, receive, send, allowed)
            return

        marks = allowed
        found = self._token_of(headers)
        if isinstance(found, str):
            # 401 с Negotiate: браузер домена повторит запрос с токеном сам
            await self._ask_negotiate(scope, receive, send, found, client)
            return

        try:
            identity: SpnegoIdentity = await self._acceptor.accept_async(found)
        except InvalidTokenError as e:
            await self._ask_negotiate(scope, receive, send, str(e), client)
            return
        except KerberosError as e:
            self._logger.exception(
                "kerberos: spnego refresh failed (keytab/SPN) [client=%s]", client
            )
            raise KerberosErrorToDomain.map(e) from e

        if identity.principal != marks.principal:
            reason = (
                f"refresh token of {identity.principal} does not match the "
                f"session of {marks.principal}"
            )
            await self._refusal(scope, receive, send, reason)
            return

        try:
            await self._admission.roles_of(identity.principal, identity.group_sids)
        except AuthorizationError:
            await self._refusal(
                scope, receive, send, f"{identity.principal} is no longer admitted"
            )
            return

        sealed = self._sealed(identity)
        if not sealed:
            await self._refusal(
                scope, receive, send, f"no delegated credentials for {marks.principal}"
            )
            return

        user = await self._builder.refreshed_user(
            identity.principal, identity.group_sids, sealed
        )
        token = create_jwt(user)
        response = Response(status_code=204)
        set_auth_cookie(Request(scope), response, token)
        adopted = ChainlitSessions().adopt_token(user.identifier, token)
        self._logger.info(
            "kerberos: refreshed sign-in ticket [principal=%s] [client=%s] "
            "[sessions=%d]",
            identity.principal,
            client,
            adopted,
        )
        await response(scope, receive, send)

    def _refresh_marks(self, headers: Headers, client: str) -> DelegatedTicket | str:
        """Метки сессии, которой позволено обменяться, либо причина отказа."""
        if not SsoRefresh.asked(headers):
            # заголовок ставит только свой fetch: чужая страница обмен не запустит
            return f"refresh without its own header [{client}]"

        marks = self._session_marks(headers)
        if marks is None:
            return "request carries no signed sign-in"

        return marks

    @staticmethod
    def _session_marks(headers: Headers) -> DelegatedTicket | None:
        """Метки входа из JWT-cookie запроса; None — сессии нет или она не SSO."""
        from chainlit.auth.cookie import get_token_from_cookies  # noqa: PLC0415

        cookies = Request(scope={"type": "http", "headers": headers.raw}).cookies
        token = get_token_from_cookies(cookies)
        if not token:
            return None

        return ChainlitSession.ticket_of_token(token)

    @staticmethod
    def _token_of(headers: Headers) -> bytes | str:
        """Токен Negotiate из заголовка либо причина, почему его нет."""
        auth = headers.get("authorization")
        if not auth:
            return "no Authorization header"

        scheme, _, value = auth.partition(" ")
        if scheme.lower() != "negotiate":
            return f"unexpected auth scheme {scheme!r}"

        if not value:
            return f"unexpected auth scheme {scheme!r}"

        try:
            return base64.b64decode(value)
        except ValueError as e:
            return f"invalid base64 token: {e}"

    async def _ask_negotiate(
        self, scope: Scope, receive: Receive, send: Send, reason: str, client: str
    ) -> None:
        """401 Negotiate без страницы логина: ответ читает скрипт, не человек."""
        self._logger.info("kerberos refresh challenge [client=%s]: %s", client, reason)
        response = Response(status_code=401, headers=self._negotiate)
        await response(scope, receive, send)

    async def _refusal(
        self, scope: Scope, receive: Receive, send: Send, reason: str
    ) -> None:
        """403: повторный обмен не относится к этой сессии, повтор не поможет."""
        self._logger.warning("kerberos refresh refused: %s", reason)
        await Response(status_code=403)(scope, receive, send)

    def _client(self, headers: Headers, scope: Scope) -> str:
        "Лучший идентификатор клиента для логов: реальный IP за прокси, иначе peer."
        if xff := headers.get("x-forwarded-for"):
            first, _, _ = xff.partition(",")
            return first.strip()

        if real := headers.get("x-real-ip"):
            return real

        peer = scope.get("client")
        if peer:
            return peer[0]

        return "unknown"

    async def _challenge(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        reason: str,
        level: int = logging.WARNING,
    ) -> None:
        "Логирует причину и отдаёт 401 Negotiate (пользователь неизвестен)."
        client = self._client(Headers(scope=scope), scope)
        self._logger.log(level, "kerberos challenge [client=%s]: %s", client, reason)

        response = Response(
            content=SsoLoginError.TICKET.challenge_page(self._urls.login),
            status_code=401,
            headers=self._negotiate,
            media_type="text/html",
        )
        await response(scope, receive, send)


class KerberosRolesInLdapProvider:
    def __init__(self, config: KerberosRolesInLdapConfig):
        self._config = config
        self._ad = ADDirectory

        self._init_mapping()

    def _init_mapping(self):
        self._samaccountname_roles: SAMAccountNameUserRolesProvider | None = None
        self._samaccountname_roles_ex: SAMAccountNameExcludeUserProvider | None = None
        self._member_of_roles: MemberOfUserRolesProvider | None = None
        self._member_of_roles_ex: MemberOfExcludeUserProvider | None = None
        self._dn_roles: DnUserRolesProvider | None = None
        self._dn_roles_ex: DnExcludeUserProvider | None = None

        if roles := self._config.mapping.samaccountname:
            self._samaccountname_roles = SAMAccountNameUserRolesProvider(roles)

        if roles := self._config.mapping.samaccountname_ex:
            self._samaccountname_roles_ex = SAMAccountNameExcludeUserProvider(roles)

        if roles := self._config.mapping.member_of:
            self._member_of_roles = MemberOfUserRolesProvider(roles)

        if roles := self._config.mapping.member_of_ex:
            self._member_of_roles_ex = MemberOfExcludeUserProvider(roles)

        if roles := self._config.mapping.dn:
            self._dn_roles = DnUserRolesProvider(roles)

        if roles := self._config.mapping.dn_ex:
            self._dn_roles_ex = DnExcludeUserProvider(roles)

    async def request(self, principal: str) -> ADUserEntry:
        search_filter = f"(userPrincipalName={principal})"

        try:
            user_dn, samaccountname, member_of = await asyncio.to_thread(
                self._ad.fetch_userdn_samaccountname_member_of,
                server=self._config.server,
                bind_dn=self._config.bind_dn,
                bind_password=self._config.bind_password,
                search_base=self._config.base_dn,
                search_filter=search_filter,
            )

            return ADUserEntry(
                dn=user_dn,
                samaccountname=samaccountname,
                member_of=member_of,
            )
        except LDAPUserNotFoundError as e:
            raise AuthenticationError("User is not registered") from e
        except LDAPServerUnavailableError as e:
            raise ExternalServiceError(
                "ldap",
                "LDAP service is unavailable, please try again later",
            ) from e
        except LDAPInvalidCredentialsError as e:
            raise InternalServiceError(
                internal_detail=f"ldap service bind rejected: {e}",
                user_detail=None,
            ) from e
        except LDAPError as e:
            raise InternalServiceError(
                internal_detail=f"ldap error: {e}", user_detail=None
            ) from e

    def roles_of(self, user: ADUserEntry) -> Iterable[str]:
        if self._samaccountname_roles:
            yield from self._samaccountname_roles.roles_of(user.samaccountname)

        if self._member_of_roles:
            yield from self._member_of_roles.roles_of(user.member_of)

        if self._dn_roles:
            yield from self._dn_roles.roles_of(user.dn)

    def excluded_of(self, user: ADUserEntry) -> bool:
        return any(self._exclusions_of(user))

    def _exclusions_of(self, user: ADUserEntry) -> Iterator[bool]:
        if self._samaccountname_roles_ex:
            yield from self._samaccountname_roles_ex.exclude_of(user.samaccountname)

        if self._member_of_roles_ex:
            yield from self._member_of_roles_ex.exclude_of(user.member_of)

        if self._dn_roles_ex:
            yield from self._dn_roles_ex.exclude_of(user.dn)


class KerberosAuth(SsoAdmission, SsoUserBuilder):
    """SSO через Kerberos/SPNEGO: кнопка на /login ведёт на /auth/sso (SpnegoMiddleware)

    Собран на FastAPI без chainlit header-auth: вход по явной кнопке, не автоматом.
    """

    _BUTTON_JS: ClassVar[Path] = Path(__file__).parent / "sso_button.js"
    """JS кнопки едет в wheel как package-data — см. pyproject boba-chainlit."""

    _CUSTOM_AUTH_ENV: ClassVar[str] = "CHAINLIT_CUSTOM_AUTH"
    """Флаг chainlit: вход обязателен, хотя свой колбэк авторизации не задан."""

    def __init__(self, url_prefix: str, config: KerberosAuthConfig):
        self._config = config
        # роуты без префикса (root_path учтёт роутер), middleware и кнопка — с полным
        self._sso_path = config.sso_path
        self._urls = SsoUrls.of(url_prefix, config.sso_path)
        self._provider = "kerberos"
        self._ad = ADDirectory
        self.acceptor = SpnegoAcceptor(config.accept, config.delegation)
        self.capture = TicketCapture(config.delegation)
        self.sealer = TicketSealer(self._secret())
        self.krb5_config = config.delegation.krb5_config
        self._logger = logging.getLogger(KerberosAuth.__name__)

        self._init_mapping()

    @staticmethod
    def _secret() -> str:
        """Секрет JWT chainlit: им же запечатывается билет входа."""
        from chainlit.auth.jwt import get_jwt_secret  # noqa: PLC0415

        secret = get_jwt_secret()
        if not secret:
            msg = "CHAINLIT_AUTH_SECRET is required: it seals the SSO sign-in ticket"
            raise RuntimeError(msg)

        return secret

    def tickets(self) -> SsoTickets:
        """Открыватель билетов входа для обвязок инструментов."""
        return SsoTickets(sealer=self.sealer, krb5_config=self.krb5_config)

    def _init_mapping(self):
        self._principal_roles: LocalUserRolesProvider | None = None
        self._principal_roles_ex: LocalExcludeUserProvider | None = None
        self._sid_roles: SidUserRolesProvider | None = None
        self._sid_roles_ex: SidExcludeUserProvider | None = None
        self._kerberos_roles_in_ldap: KerberosRolesInLdapProvider | None = None

        if roles := self._config.roles:
            if roles.principal:
                self._principal_roles = LocalUserRolesProvider(roles.principal)

            if roles.principal_ex:
                self._principal_roles_ex = LocalExcludeUserProvider(roles.principal_ex)

            if roles.sid:
                self._sid_roles = SidUserRolesProvider(roles.sid)

            if roles.sid_ex:
                self._sid_roles_ex = SidExcludeUserProvider(roles.sid_ex)

        if ldap_roles := self._config.ldap_roles:
            self._kerberos_roles_in_ldap = KerberosRolesInLdapProvider(ldap_roles)

    @staticmethod
    def _username_from_principal(
        principal_format: str,
        principal: str,
    ) -> str:
        """user@REALM | DOMAIN\\user -> sAMAccountName по шаблону с {username}."""
        try:
            return LoginTemplate.username_of(principal_format, principal)
        except TemplateError as exc:
            raise InternalServiceError(
                internal_detail=(
                    f"principal {principal!r} does not match "
                    f"principal_format {principal_format!r}: {exc}"
                ),
                user_detail=None,
            ) from exc

    def install(self, chainlit_app: FastAPI) -> None:
        # без password/header-колбэка chainlit считает, что логина нет, и пускает
        # анонима; флаг включает обязательный вход без автозапроса /auth/header
        os.environ[self._CUSTOM_AUTH_ENV] = "1"

        chainlit_app.add_middleware(SpnegoMiddleware, sso=self.runtime())
        self._install_routes(chainlit_app)
        self._install_button_js()

    def runtime(self) -> SsoRuntime:
        """Части SSO для middleware; тест собирает его тем же набором."""
        return SsoRuntime(
            urls=self._urls,
            config=self._config,
            acceptor=self.acceptor,
            capture=self.capture,
            sealer=self.sealer,
            admission=self,
            builder=self,
        )

    async def _sso_user(self, request: Request) -> cl.User | None:
        """Пользователь после SPNEGO, заведённый в data layer (без билета входа)."""
        from chainlit.data import get_data_layer  # noqa: PLC0415

        user = await self._build_user(request.headers, SsoPass.take(request))
        if user is None:
            return None

        data_layer = get_data_layer()
        if data_layer is None:
            return user

        try:
            await data_layer.create_user(self._without_ticket(user))
        except Exception as exc:
            raise InternalServiceError(
                internal_detail=f"failed to persist SSO user: {exc}",
                user_detail=None,
            ) from exc

        return user

    @staticmethod
    def _without_ticket(user: cl.User) -> cl.User:
        """Копия для строки users: билет входа живёт только в JWT."""
        metadata: dict[str, Any] = {}
        for key, value in user.metadata.items():
            if key == UserMetadataField.TICKET:
                continue

            metadata[key] = value

        return cl.User(
            identifier=user.identifier,
            display_name=user.display_name,
            metadata=metadata,
        )

    def _login_redirect(self, exc: BaseError) -> RedirectResponse:
        """Исход SSO кодом на страницу логина: браузер пришёл навигацией, не fetch."""
        self._logger.error("%s", LogLine.safe(FailureReport.of(exc).log))

        code = SsoLoginError.FAILED
        if isinstance(exc, AuthorizationError):
            code = SsoLoginError.DENIED

        return RedirectResponse(url=code.login_url(self._urls.login), status_code=303)

    async def _build_user(self, headers: Headers, sealed: str) -> cl.User | None:
        """По X-Remote-User строит cl.User: username из принципала + роли из AD."""
        principal = headers.get(self._config.header)
        if not principal:
            return None

        raw_sids = headers.get(self._config.sids_header)
        if raw_sids is None:
            raw_sids = ""

        return await self.refreshed_user(principal, SidsHeader.parse(raw_sids), sealed)

    async def refreshed_user(
        self, principal: str, group_sids: Sequence[str], sealed: str
    ) -> cl.User:
        metadata = self._sso_metadata(principal, sealed)

        roles = await self.roles_of(principal, group_sids)
        if roles:
            metadata[UserMetadataField.ROLES] = roles

        username = self._username_from_principal(
            self._config.principal_format,
            principal,
        )
        login = UserLogin.of(username)

        return cl.User(
            identifier=login.key,
            display_name=login.display,
            metadata=metadata,
        )

    async def roles_of(self, principal: str, group_sids: Sequence[str]) -> list[str]:
        """Роли принципала по всем источникам; исключение — AuthorizationError.

        Зовётся и при входе, и при повторном обмене: запрет в AD должен
        отсекать обмен так же, как отсёк бы новый вход.
        """
        roles: list[str] = []
        excluded = False

        if self._principal_roles:
            roles.extend(self._principal_roles.roles_of(principal))

        if self._principal_roles_ex:
            excluded = any(self._principal_roles_ex.exclude_of(principal))

        sid_roles, sid_excluded = self._sid_mapping(group_sids)
        roles.extend(sid_roles)
        if sid_excluded:
            excluded = True

        if self._kerberos_roles_in_ldap:
            user = await self._kerberos_roles_in_ldap.request(principal)
            roles.extend(self._kerberos_roles_in_ldap.roles_of(user))

            if self._kerberos_roles_in_ldap.excluded_of(user):
                excluded = True

        if excluded:
            self._logger.warning("access denied for %s (excluded)", principal)
            raise AuthorizationError("Access denied")

        mapped = sorted(set(roles))

        if self._config.require_roles and not mapped:
            self._logger.warning("access denied for %s (no roles mapped)", principal)
            raise AuthorizationError("Access denied")

        return mapped

    def _sso_metadata(self, principal: str, sealed: str) -> dict[str, Any]:
        """Metadata входа: провайдер, принципал и запечатанный билет."""
        metadata: dict[str, Any] = {
            UserMetadataField.PROVIDER: SignInProvider.KERBEROS,
            UserMetadataField.PRINCIPAL: principal,
        }

        if sealed:
            metadata[UserMetadataField.TICKET] = sealed
            return metadata

        # без билета сессия останется без делегированных кредов: причина в логе выше
        self._logger.warning(
            "kerberos: sign-in of %s carries no delegated ticket", principal
        )

        return metadata

    def _sid_mapping(self, sids: Sequence[str]) -> tuple[list[str], bool]:
        "Роли и исключение по SID групп из PAC; сами SID приходят от вызывающего."
        has_roles = self._sid_roles is not None
        has_exclusions = self._sid_roles_ex is not None

        if not has_roles and not has_exclusions:
            return [], False

        roles: list[str] = []
        if self._sid_roles:
            roles = list(self._sid_roles.roles_of(list(sids)))

        excluded = False
        if self._sid_roles_ex:
            excluded = any(self._sid_roles_ex.exclude_of(list(sids)))

        return roles, excluded

    def _install_routes(self, chainlit_app: FastAPI) -> None:
        """Регистрирует /auth/sso (вход после SPNEGO) и /sso.js (кнопка)."""
        js = self._get_static_button()

        async def auth_sso(request: Request) -> RedirectResponse:
            # сюда долетаем после успешного SPNEGO: заводим сессию chainlit (JWT-cookie)

            try:
                user = await self._sso_user(request)
            except BaseError as exc:
                return self._login_redirect(exc)

            if user is None:
                url = SsoLoginError.TICKET.login_url(self._urls.login)
                return RedirectResponse(url=url, status_code=303)

            resp = RedirectResponse(url=self._urls.app, status_code=303)
            set_auth_cookie(request, resp, create_jwt(user))
            return resp

        async def sso_js() -> Response:
            return Response(content=js, media_type="application/javascript")

        self._prepend_route(chainlit_app, self._sso_path, auth_sso)
        self._prepend_route(chainlit_app, self._urls.js, sso_js)

    def _install_button_js(self) -> None:
        """Подключает sso.js на странице логина через custom_js."""
        existing = cl_config.ui.custom_js
        if not existing:
            cl_config.ui.custom_js = self._urls.js
            return

        if existing == self._urls.js:
            return

        # слот один на приложение: занявший его скрипт обязан подгрузить sso.js
        self._logger.info(
            "custom_js already set (%s) — expecting it to load %s itself",
            existing,
            self._urls.js,
        )

    @staticmethod
    def _prepend_route(
        chainlit_app: FastAPI, path: str, endpoint: Callable[..., Awaitable[Any]]
    ) -> None:
        """Добавляет GET-роут в начало, иначе его перехватит chainlit"""
        chainlit_app.add_api_route(
            path, endpoint, methods=["GET"], include_in_schema=False
        )

        route = chainlit_app.router.routes.pop()
        chainlit_app.router.routes.insert(0, route)

    def _get_static_button(self) -> str:
        """JS кнопки SSO из файла-ресурса рядом с модулем; сервер знает только URL."""
        template = self._BUTTON_JS.read_text(encoding="utf-8")
        with_sso = template.replace(ButtonJsVar.SSO_URL, self._urls.sso)
        with_refresh = with_sso.replace(ButtonJsVar.REFRESH_URL, self._urls.refresh)
        with_header = with_refresh.replace(
            ButtonJsVar.REFRESH_HEADER, SsoRefresh.HEADER
        )
        with_value = with_header.replace(
            ButtonJsVar.REFRESH_HEADER_VALUE, SsoRefresh.VALUE
        )

        return with_value.replace(ButtonJsVar.TRANSLATIONS_URL, self._urls.translations)
