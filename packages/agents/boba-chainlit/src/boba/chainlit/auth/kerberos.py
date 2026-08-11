"""SSO через Kerberos/SPNEGO для chainlit.

Ошибки: AuthenticationError, AuthorizationError — отказ входа;
ExternalServiceError — недоступен внешний сервис (KDC, LDAP);
InternalServiceError — keytab/SPN/конфиг непригодны.
"""

import asyncio
import base64
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, ClassVar, Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

import chainlit as cl
from boba.chainlit.domain.errors import (
    AuthenticationError,
    AuthorizationError,
    BaseError,
    ExternalServiceError,
    InternalServiceError,
)
from boba.chainlit.auth.ldap import (
    ADDirectory,
    ADUserEntry,
    DnExcludeUserProvider,
    DnUserRolesProvider,
    LDAPError,
    LDAPInvalidCredentialsError,
    LdapRolesConfig,
    LDAPServerUnavailableError,
    LDAPUserNotFoundError,
    MemberOfExcludeUserProvider,
    MemberOfUserRolesProvider,
    SAMAccountNameExcludeUserProvider,
    SAMAccountNameUserRolesProvider,
)
from boba.chainlit.auth.local import (
    LocalExcludeUserProvider,
    LocalUserRolesProvider,
    RoleExcludeConfig,
    RoleMappingConfig,
)
from boba.chainlit.domain.session import UserMetadataField
from boba.krb import (
    AcceptConfig,
    CcacheRegistry,
    CredentialsExpiredError,
    DelegationConfig,
    DelegationNotPermittedError,
    InvalidTokenError,
    KerberosDelegation,
    KerberosError,
    KeytabError,
    SpnegoAcceptor,
    SpnegoIdentity,
)
from chainlit.config import config as cl_config


class KerberosRolesInLdapMappingConfig(LdapRolesConfig):
    """Мапинг ролей/исключений по атрибутам AD; поля наследуются от LdapRolesConfig."""


class KerberosRolesInLdapConfig(BaseModel):
    server: str = Field(
        description="URI контроллера домена, напр. ldaps://dc.corp.example.com:636.",
    )
    base_dn: str = Field(
        description="База поиска пользователя, напр. DC=corp,DC=example,DC=com.",
    )
    bind_dn: str
    bind_password: str
    mapping: KerberosRolesInLdapMappingConfig = Field(
        default=KerberosRolesInLdapMappingConfig(),
    )


class KerberosRolesConfig(BaseModel):
    principal: RoleMappingConfig | None = None
    principal_ex: RoleExcludeConfig | None = None
    sid: RoleMappingConfig | None = Field(
        default=None,
        description="Мапер SID группы из PAC kerberos-тикета - роли.",
    )
    sid_ex: RoleExcludeConfig | None = Field(
        default=None,
        description="SID групп из PAC, членам которых запрещён вход (403).",
    )


class KerberosAuthConfig(BaseModel):
    """SSO через Kerberos/SPNEGO: тикет валидирует middleware, роль — из групп AD."""

    type: Literal["kerberos"] = "kerberos"

    accept: AcceptConfig = Field(
        description=(
            "SPN и keytab сервиса для SPNEGO-accept; "
            "в конфиге подключается ссылкой ${kerberos.<name>}."
        ),
    )
    principal_format: str
    sso_path: str = Field(default="/auth/sso")
    header: str = Field(
        default="X-Remote-User",
        description="Заголовок, куда кладётся принципал для header_auth_callback.",
    )
    delegation: DelegationConfig = Field(
        default_factory=DelegationConfig,
        description="Параметры ccache для unconstrained режима делегирования",
    )
    roles: KerberosRolesConfig | None = None
    ldap_roles: KerberosRolesInLdapConfig | None = None
    require_roles: bool = Field(
        default=True,
        description=(
            "403 после успешной аутентификации, "
            "если пользователю не замапилась ни одна роль."
        ),
    )

    @property
    def sids_header(self) -> str:
        "Заголовок с SID-ами групп из PAC; ставит SpnegoMiddleware."
        return f"{self.header}-Sids"


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


class SidsHeader:
    "Формат заголовка со списком SID: сериализация и разбор в одном месте."

    @staticmethod
    def render(sids: Iterable[str]) -> str:
        return ",".join(sids)

    @staticmethod
    def parse(raw: str) -> list[str]:
        return list(SidsHeader._parts(raw))

    @staticmethod
    def _parts(raw: str) -> Iterator[str]:
        for part in raw.split(","):
            if not part:
                continue
            yield part


class SpnegoMiddleware:
    "SPNEGO-accept на /auth/sso"

    def __init__(
        self,
        app: ASGIApp,
        *,
        auth_path: str,
        config: KerberosAuthConfig,
        acceptor: SpnegoAcceptor,
        delegation: KerberosDelegation,
    ) -> None:
        self._app = app
        self._auth_path = auth_path
        self._config = config
        self._acceptor = acceptor
        self._delegation = delegation
        self._negotiate = {"WWW-Authenticate": "Negotiate"}
        self._logger = logging.getLogger(SpnegoMiddleware.__name__)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):  # noqa: PLR0911
        if scope["type"] != "http":
            return await self._app(scope, receive, send)

        path = scope.get("path")
        if not isinstance(path, str):
            return await self._app(scope, receive, send)

        path = path.removesuffix("/").lower()

        if path != self._auth_path:
            return await self._app(scope, receive, send)

        headers = Headers(scope=scope).mutablecopy()
        client = self._client(headers, scope)

        auth = headers.get("authorization")
        if not auth:
            # обычное начало handshake: токена ещё нет — это не ошибка
            return await self._challenge(
                scope, receive, send, "no Authorization header", logging.INFO
            )

        scheme, _, value = auth.partition(" ")
        if scheme.lower() != "negotiate":
            return await self._challenge(
                scope, receive, send, f"unexpected auth scheme {scheme!r}"
            )

        if not value:
            return await self._challenge(
                scope, receive, send, f"unexpected auth scheme {scheme!r}"
            )

        try:
            token = base64.b64decode(value)
        except ValueError as e:
            return await self._challenge(
                scope, receive, send, f"invalid base64 token: {e}"
            )

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

        self._delegation.on_success_authenticated(identity)

        header = self._config.header.lower()
        headers[header] = identity.principal
        # заголовок с SID-ами перетираем всегда — клиентское значение не пройдёт
        headers[self._config.sids_header.lower()] = SidsHeader.render(
            identity.group_sids
        )
        scope["headers"] = headers.raw

        return await self._app(scope, receive, send)

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

        response = Response(status_code=401, headers=self._negotiate)
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


class KerberosAuth:
    """SSO через Kerberos/SPNEGO: кнопка на /login ведёт на /auth/sso (SpnegoMiddleware)

    Собран на FastAPI без chainlit header-auth: вход по явной кнопке, не автоматом.
    """

    _BUTTON_JS: ClassVar[Path] = Path(__file__).parent / "sso_button.js"
    """JS кнопки едет в wheel как package-data — см. pyproject boba-chainlit."""

    _SSO_URL_VAR: ClassVar[str] = "__SSO_URL__"

    def __init__(self, url_prefix: str, config: KerberosAuthConfig):
        self._config = config
        # роуты без префикса (root_path учтёт роутер), middleware и кнопка — с полным
        self._sso_path = config.sso_path
        self._sso_url = f"{url_prefix}{config.sso_path}"
        self._js_path = f"{self._sso_url}.js"
        self._app_url = f"{url_prefix}/"
        self._login_url = f"{url_prefix}/login"
        self._provider = "kerberos"
        self._ad = ADDirectory
        self.acceptor = SpnegoAcceptor(config.accept)
        self.registry = CcacheRegistry(renew=config.delegation.renew)
        self.delegation = KerberosDelegation(
            registry=self.registry,
            config=config.accept,
            delegation=config.delegation,
        )
        self._logger = logging.getLogger(KerberosAuth.__name__)

        self._init_mapping()

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
        placeholder = "{username}"
        if placeholder not in principal_format:
            raise InternalServiceError(
                internal_detail=(
                    f"principal_format {principal_format!r} не содержит {placeholder}"
                ),
                user_detail=None,
            )

        head, tail = principal_format.split(placeholder, 1)
        pattern = re.compile(f"^{re.escape(head)}(?P<username>.+?){re.escape(tail)}$")
        match = pattern.match(principal)
        if not match:
            raise InternalServiceError(
                internal_detail=(
                    f"principal {principal!r} не соответствует формату "
                    f"{principal_format!r}"
                ),
                user_detail=None,
            )

        return match.group("username")

    def install(self, chainlit_app: FastAPI) -> None:
        chainlit_app.add_middleware(
            SpnegoMiddleware,
            auth_path=self._sso_url,
            config=self._config,
            acceptor=self.acceptor,
            delegation=self.delegation,
        )
        self._install_routes(chainlit_app)
        self._install_button_js()

    async def _build_user(self, headers: Headers) -> cl.User | None:
        """По X-Remote-User строит cl.User: username из принципала + роли из AD."""
        principal = headers.get(self._config.header)
        if not principal:
            return None

        metadata: dict[str, Any] = {
            UserMetadataField.PROVIDER: KerberosAuth.__name__,
        }

        roles: list[str] = []
        excluded = False

        if self._principal_roles:
            roles.extend(self._principal_roles.roles_of(principal))

        if self._principal_roles_ex:
            excluded = any(self._principal_roles_ex.exclude_of(principal))

        sid_roles, sid_excluded = self._sid_mapping(headers)
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

        roles = sorted(set(roles))

        requires_roles = self._config.require_roles
        if requires_roles and not roles:
            self._logger.warning("access denied for %s (no roles mapped)", principal)
            raise AuthorizationError("Access denied")

        if roles:
            metadata[UserMetadataField.ROLES] = roles

        username = self._username_from_principal(
            self._config.principal_format,
            principal,
        )

        return cl.User(identifier=username, metadata=metadata)

    def _sid_mapping(self, headers: Headers) -> tuple[list[str], bool]:
        "Роли и исключение по SID группам из PAC; заголовок ставит middleware."
        has_roles = self._sid_roles is not None
        has_exclusions = self._sid_roles_ex is not None

        if not has_roles and not has_exclusions:
            return [], False

        raw_sids = headers.get(self._config.sids_header)
        if raw_sids is None:
            raw_sids = ""

        sids = SidsHeader.parse(raw_sids)

        roles: list[str] = []
        if self._sid_roles:
            roles = list(self._sid_roles.roles_of(sids))

        excluded = False
        if self._sid_roles_ex:
            excluded = any(self._sid_roles_ex.exclude_of(sids))

        return roles, excluded

    def _install_routes(self, chainlit_app: FastAPI) -> None:
        """Регистрирует /auth/sso (вход после SPNEGO) и /sso.js (кнопка)."""
        js = self._get_static_button()

        async def auth_sso(request: Request) -> RedirectResponse:
            # сюда долетаем после успешного SPNEGO: заводим сессию chainlit (JWT-cookie)
            from chainlit.auth import create_jwt, set_auth_cookie  # noqa: PLC0415
            from chainlit.data import get_data_layer  # noqa: PLC0415

            user = await self._build_user(request.headers)
            if user is None:
                return RedirectResponse(url=self._login_url, status_code=303)

            if data_layer := get_data_layer():
                try:
                    await data_layer.create_user(user)
                except Exception as exc:
                    raise InternalServiceError(
                        internal_detail=f"failed to persist SSO user: {exc}",
                        user_detail=None,
                    ) from exc

            resp = RedirectResponse(url=self._app_url, status_code=303)
            set_auth_cookie(request, resp, create_jwt(user))
            return resp

        async def sso_js() -> Response:
            return Response(content=js, media_type="application/javascript")

        self._prepend_route(chainlit_app, self._sso_path, auth_sso)
        self._prepend_route(chainlit_app, self._js_path, sso_js)

    def _install_button_js(self) -> None:
        """Подключает sso.js на странице логина через custom_js."""
        existing = cl_config.ui.custom_js
        if not existing:
            cl_config.ui.custom_js = self._js_path
            return

        if existing == self._js_path:
            return

        # слот один на приложение: занявший его скрипт обязан подгрузить sso.js
        self._logger.info(
            "custom_js already set (%s) — expecting it to load %s itself",
            existing,
            self._js_path,
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
        return template.replace(self._SSO_URL_VAR, self._sso_url)
