import asyncio
import base64
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from itertools import chain
from typing import Any, Literal

import chainlit as cl
import krb5
from chainlit.config import config as cl_config
from fastapi import FastAPI
from gssapi import Credentials, Name, NameType, SecurityContext
from gssapi.exceptions import (
    ExpiredContextError,
    ExpiredCredentialsError,
    InvalidCredentialsError,
    MissingCredentialsError,
    UnauthorizedError,
)
from gssapi.raw.misc import GSSError
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from boba.chainlit2.chat.auth.fix import (
    FixExcludeUserProvider,
    FixUserRolesProvider,
    RoleExcludeConfig,
    RoleMappingConfig,
)
from boba.chainlit2.chat.auth.ldap import (
    ADDirectory,
    DnExcludeUserProvider,
    DnUserRolesProvider,
    LDAPError,
    LDAPInvalidCredentialsError,
    LDAPServerUnavailableError,
    LDAPUserNotFoundError,
    MemberOfExcludeUserProvider,
    MemberOfUserRolesProvider,
    SAMAccountNameExcludeUserProvider,
    SAMAccountNameUserRolesProvider,
)
from boba.chainlit2.errors import (
    AuthenticationError,
    BaseError,
    ExternalServiceError,
    InternalServiceError,
)


@dataclass(frozen=True)
class UserCcache:
    """Сем для tools: принципал пользователя и его ccache (значение KRB5CCNAME)."""

    principal: str
    ccache: str


class KerberosDelegationConfig(BaseModel):
    """
    Куда класть ccache и продлевать ли токен
    """

    model_config = ConfigDict(extra="ignore")

    ccache_template: str = Field(
        default="MEMORY:agent-{principal}",
        description="Шаблон имени ccache на пользователя {principal} подставляется",
    )
    renew: bool = Field(
        default=True,
        description="Продлевать renewable-тикет по запросу при ошибке истечения.",
    )


class KerberosRolesInLdapMappingConfig(BaseModel):
    samaccountname: RoleMappingConfig | None = Field(default=None, description="")
    samaccountname_ex: RoleExcludeConfig | None = Field(default=None, description="")
    member_of: RoleMappingConfig | None = Field(default=None, description="")
    member_of_ex: RoleExcludeConfig | None = Field(default=None, description="")
    dn: RoleMappingConfig | None = Field(default=None, description="")
    dn_ex: RoleExcludeConfig | None = Field(default=None, description="")


class KerberosRolesInLdapConfig(BaseModel):
    server: str = Field(
        description="URI контроллера домена, напр. ldaps://dc.corp.example.com:636.",
    )
    base_dn: str = Field(
        description="База поиска пользователя, напр. DC=corp,DC=example,DC=com.",
    )
    bind_dn: str = Field(
        description="",
    )
    bind_password: str = Field(
        description="",
    )
    mapping: KerberosRolesInLdapMappingConfig = Field(
        default=KerberosRolesInLdapMappingConfig(),
        description="",
    )


class KerberosRolesConfig(BaseModel):
    principal: RoleMappingConfig | None = Field(
        default=None,
        description="",
    )
    principal_ex: RoleExcludeConfig | None = Field(
        default=None,
        description="",
    )


class KerberosAuthConfig(BaseModel):
    """SSO через Kerberos/SPNEGO: тикет валидирует middleware, роль — из групп AD."""

    type: Literal["kerberos"] = "kerberos"

    service_name: str = Field(
        description="SPN сервиса (HTTP/host@REALM)",
    )
    keytab: str = Field(
        description=(
            "Путь к keytab сервиса (ключ SPN для SPNEGO-accept); "
            "обычно /etc/krb5.keytab."
        ),
    )
    principal_format: str = Field(
        description="",
    )
    sso_path: str = Field(default="/auth/sso")
    header: str = Field(
        default="X-Remote-User",
        description="Заголовок, куда кладётся принципал для header_auth_callback.",
    )
    delegation: KerberosDelegationConfig = Field(
        default_factory=KerberosDelegationConfig,
        description="Параметры ccache для unconstrained режима делегирования",
    )
    roles: KerberosRolesConfig | None = Field(
        default=None,
        description="",
    )
    ldap_roles: KerberosRolesInLdapConfig | None = Field(
        default=None,
        description="",
    )


class KerberosCredentialStore:
    """
    Реестр делегированных тикетов (принципал - ccache) с продлением по запросу.

    Store сам срок не отслеживает и не угадывает: продление инициирует место
    использования ccache (tool), когда бэкенд вернул ошибку истечения — оно
    зовёт renew(). На каждого принципала свой lock, чтобы конкурентные продления
    не сходили в KDC и не перезаписали ccache дважды.

    Store нужен двум мирам:
        DI-провайдеру (читает)
        SpnegoMiddleware (пишет)
    """

    class _EntryCcache:
        __slots__ = ("ccache", "lock")

        def __init__(self, ccache: str) -> None:
            self.ccache = ccache
            self.lock = asyncio.Lock()

    def __init__(self) -> None:
        self._entries: dict[str, KerberosCredentialStore._EntryCcache] = {}

    def register(self, principal: str, ccache: str) -> None:
        """Сохраняет/обновляет делегированный тикет принципала.

        Существующую запись обновляет на месте — lock принципала сохраняется.
        """
        entry = self._entries.get(principal)
        if entry is None:
            self._entries[principal] = self._EntryCcache(ccache)
        else:
            entry.ccache = ccache

    def ccache_of(self, principal: str) -> str | None:
        """ccache принципала (значение KRB5CCNAME) или None."""
        entry = self._entries.get(principal)
        return entry.ccache if entry else None

    async def renew(self, principal: str) -> bool:
        """Продлевает тикет принципала; звать при ошибке истечения от бэкенда.

        Сериализовано локом принципала. False — продление недоступно/не удалось
        (renew выключен, нет записи или KDC отказал); при провале запись снимается.
        """
        entry = self._entries.get(principal)
        if entry is None:
            return False

        async with entry.lock:
            # _renew синхронно ходит в KDC — в поток, чтобы не блокировать loop
            ok = await asyncio.to_thread(self._renew, entry.ccache)
            if not ok:
                self._entries.pop(principal, None)
            return ok

    def drop(self, principal: str) -> None:
        """Убирает тикет принципала."""
        self._entries.pop(principal, None)

    @staticmethod
    def _renew(ccache: str) -> bool:
        """
        Продлевает TGT в ccache через krb5
        """
        try:
            ctx = krb5.init_context()
            cc = krb5.cc_resolve(ctx, ccache.encode())
            princ = krb5.cc_get_principal(ctx, cc)
            creds = krb5.get_renewed_creds(ctx, princ, cc)
            krb5.cc_initialize(ctx, cc, princ)
            krb5.cc_store_cred(ctx, cc, creds)
        except Exception:
            return False

        return True


class GssErrorToDomain:
    "Классификация GSSError (initiate-сторона делегирования) в доменную ошибку."

    @staticmethod
    def map(e: Exception) -> BaseError:
        # не наша вина / временное / нужен повторный логин → External
        if isinstance(e, (ExpiredCredentialsError, ExpiredContextError)):
            return ExternalServiceError(
                "kerberos", "Kerberos credentials expired, please re-login"
            )

        # делегирование запрещено политикой (msDS-AllowedToDelegateTo) — конфиг AD
        if isinstance(e, UnauthorizedError):
            return InternalServiceError(
                internal_detail=f"gss unauthorized (delegation not permitted): {e}",
                user_detail=None,
            )

        # креды отсутствуют/повреждены (ccache/keytab) — наша сторона
        if isinstance(e, (MissingCredentialsError, InvalidCredentialsError)):
            return InternalServiceError(
                internal_detail=f"gss credential problem: {e}",
                user_detail=None,
            )

        # неизвестный GSSError: чаще S4U-политика / неверный SPN / keytab —
        # трактуем как нашу конфигурацию, полный maj/min кладём в лог
        return InternalServiceError(
            internal_detail=(
                f"gss error maj={getattr(e, 'maj_code', None)} "
                f"min={getattr(e, 'min_code', None)}: {e}"
            ),
            user_detail=None,
        )


class KerberosDelegation:
    """
    Выбираем стратегию согласно AD учетки
    Если в AD установлено delegated_creds, значит выбирается
    стратегия ограниченного делегирования (перечислены allowlist)
    иначе выбирается стратегия неограниченного делегирования
    """

    def __init__(
        self,
        store: KerberosCredentialStore,
        keytab: str,
        ccache_template: str,
        service_name: str,
    ) -> None:
        self._store = store
        self._ccache_template = ccache_template
        self._service_name = service_name
        self._keytab = keytab
        self._logger = logging.getLogger(KerberosDelegation.__name__)

    @staticmethod
    def sanitize_principal(principal: str):
        return re.sub(r"[^\w.@-]", "_", principal)

    def captured(self, username: str) -> bool:
        "Есть ли захваченный при логине ccache пользователя (форварднутый TGT)."
        return self._store.ccache_of(username) is not None

    @staticmethod
    def _init_token(ccache: str, target_spn: str) -> bytes:
        try:
            creds = Credentials(usage="initiate", store={b"ccache": ccache.encode()})
            target = Name(target_spn, NameType.kerberos_principal)
            ctx = SecurityContext(name=target, creds=creds, usage="initiate")
            token = ctx.step()
        except GSSError as e:
            raise GssErrorToDomain.map(e) from e

        # initiate обязан выдать AP-REQ для бэкенда; пустой токен = слать нечего
        if not token:
            raise InternalServiceError(
                internal_detail="gss step returned empty token on initiate side",
                user_detail=None,
            )

        return token

    def on_success_authenticated(self, principal: str, ctx: SecurityContext) -> None:
        deleg = ctx.delegated_creds
        if deleg is None:
            # AD не форварднул TGT — делегирование запрещено для пользователя.
            self._logger.warning(
                "no delegated_credentials for %s (delegation not permitted in AD)",
                principal,
            )
            return

        safe_principal = self.sanitize_principal(principal)
        try:
            ccache = self._ccache_template.format(principal=safe_principal)
        except (KeyError, IndexError) as e:
            raise InternalServiceError(
                internal_detail=f"bad ccache_template {self._ccache_template!r}: {e}",
                user_detail=None,
            ) from e

        try:
            deleg.store(
                store={b"ccache": ccache.encode()},
                usage="initiate",
                overwrite=True,
            )
        except GSSError as e:
            # не смогли сохранить делегированный тикет — наша сторона.
            raise InternalServiceError(
                internal_detail=f"failed to store delegated ccache {ccache}: {e}",
                user_detail=None,
            ) from e

        self._store.register(principal, ccache)
        self._logger.info(
            "kerberos: captured delegated ticket %s -> %s", principal, ccache
        )

    async def credentials_for(self, username: str, target_spn: str) -> bytes:
        if self.captured(username):
            ccache = self._store.ccache_of(username)
            if ccache is None:
                raise InternalServiceError(
                    internal_detail=f"no delegated ccache for {username}",
                    user_detail="Делегирование недоступно для пользователя",
                )
            return await asyncio.to_thread(self._init_token, ccache, target_spn)

        # s4u
        return await asyncio.to_thread(
            self._s4u_token, self._service_name, self._keytab, username, target_spn
        )

    @staticmethod
    def _s4u_token(
        service_name: str, keytab: str, username: str, target_spn: str
    ) -> bytes:
        try:
            service = Credentials(
                name=Name(service_name, NameType.kerberos_principal),
                usage="both",
                store={b"keytab": keytab},
            )
            user = Name(username, NameType.kerberos_principal)
            user_creds = service.impersonate(user)
            target = Name(target_spn, NameType.kerberos_principal)
            # initiate этими creds → KDC делает S4U2Proxy и проверяет whitelist
            ctx = SecurityContext(name=target, creds=user_creds, usage="initiate")
            # произвести токен, который мы пошлём бэкенду
            token = ctx.step()
        except GSSError as e:
            raise GssErrorToDomain.map(e) from e

        # initiate обязан выдать AP-REQ для бэкенда; пустой токен = слать нечего
        if not token:
            raise InternalServiceError(
                internal_detail="gss step returned empty token on initiate side (S4U)",
                user_detail=None,
            )

        return token


class SpnegoMiddleware:
    """
    SPNEGO-accept на /auth/sso
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        auth_path: str,
        config: KerberosAuthConfig,
        delegation: KerberosDelegation,
    ) -> None:
        self._app = app
        self._auth_path = auth_path
        self._config = config
        self._delegation = delegation
        self._negotiate = {"WWW-Authenticate": "Negotiate"}
        self.logger = logging.getLogger(SpnegoMiddleware.__name__)

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
        if scheme.lower() != "negotiate" or not value:
            return await self._challenge(
                scope, receive, send, f"unexpected auth scheme {scheme!r}"
            )

        try:
            token = base64.b64decode(value)
        except Exception as e:
            return await self._challenge(
                scope, receive, send, f"invalid base64 token: {e}"
            )

        try:
            # сбой здесь это проблема сервера (keytab/SPN)
            ctx = self._get_spnego_context()
        except GSSError as e:
            self.logger.exception(
                "kerberos: spnego accept context failed (keytab/SPN) [client=%s]",
                client,
            )
            raise InternalServiceError(
                internal_detail=f"gss error: {e}, file: {__file__}",
                user_detail="Spnego authentication failed",
            ) from e

        try:
            # сбой здесь это проблема клиента (битый/просроченный токен)
            ctx.step(token)
        except GSSError as e:
            return await self._challenge(
                scope, receive, send, f"gss step failed: {e}"
            )

        if not ctx.complete:
            return await self._challenge(
                scope,
                receive,
                send,
                "spnego context incomplete (multi-leg not supported)",
                logging.INFO,
            )

        principal = str(ctx.initiator_name)
        self.logger.info(
            "kerberos authenticated [principal=%s] [client=%s]", principal, client
        )

        self._delegation.on_success_authenticated(principal, ctx)

        header = self._config.header.lower()
        headers[header] = principal
        scope["headers"] = headers.raw

        return await self._app(scope, receive, send)

    def _client(self, headers: Headers, scope: Scope) -> str:
        "Лучший идентификатор клиента для логов: реальный IP за прокси, иначе peer."
        if xff := headers.get("x-forwarded-for"):
            return xff.split(",")[0].strip()
        if real := headers.get("x-real-ip"):
            return real
        peer = scope.get("client")
        return peer[0] if peer else "unknown"

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
        self.logger.log(level, "kerberos challenge [client=%s]: %s", client, reason)
        await Response(status_code=401, headers=self._negotiate)(scope, receive, send)

    def _get_spnego_context(self):
        """Принимает SPNEGO-token от браузера и"""
        name = Name(self._config.service_name, NameType.kerberos_principal)
        creds = Credentials(
            name=name,
            usage="accept",
            store={
                b"keytab": self._config.keytab,
            },
        )

        ctx = SecurityContext(
            creds=creds,
            usage="accept",
        )

        return ctx


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

    async def request(self, principal: str) -> tuple[str, str, list[str]]:
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
            return user_dn, samaccountname, member_of
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

    def roles_of(
        self, user_dn: str, samaccountname: str, member_of: list[str]
    ) -> Iterable[str]:
        if self._samaccountname_roles:
            for x in self._samaccountname_roles.roles_of(samaccountname):
                yield x

        if self._member_of_roles:
            for x in self._member_of_roles.roles_of(member_of):
                yield x

        if self._dn_roles:
            for x in self._dn_roles.roles_of(user_dn):
                yield x

    def excluded_of(
        self, user_dn: str, samaccountname: str, member_of: list[str]
    ) -> bool:
        res = []
        if self._samaccountname_roles_ex:
            res.append(self._samaccountname_roles_ex.exclude_of(samaccountname))

        if self._member_of_roles_ex:
            res.append(self._member_of_roles_ex.exclude_of(member_of))

        if self._dn_roles_ex:
            res.append(self._dn_roles_ex.exclude_of(user_dn))

        return any(chain.from_iterable(res))


class KerberosAuth:
    """
    SSO через Kerberos/SPNEGO

    Собирается полностью на уровне FastAPI, без chainlit header-auth
    Так как требуется вход по sso через явное нажатие на кнопку,
    а не автоматический вход через header-auth

    Добавляет в ui кнопку /login, которая ведёт на /auth/sso
    SpnegoMiddleware перехватывает этот путь и
    выполняет 401: Authentification: Negotiate
    Далее браузер подключенный в ActiveDirectory (+Kerberos)
    отправляет токен пользователя еще раз, по которому SpnegoMiddleware выполняет accept
    и получает итоговый токен пользователя
    """

    def __init__(self, url_prefix: str, config: KerberosAuthConfig):
        self._config = config
        # роуты регистрируются без префикса (роутер учитывает root_path),
        # а middleware и кнопка работают с полным путём (с префиксом)
        self._sso_path = config.sso_path
        self._sso_url = f"{url_prefix}{config.sso_path}"
        self._js_path = f"{self._sso_url}.js"
        self._app_url = f"{url_prefix}/"
        self._login_url = f"{url_prefix}/login"
        self._provider = "kerberos"
        self._ad = ADDirectory
        self.delegation = KerberosDelegation(
            store=KerberosCredentialStore(),
            keytab=config.keytab,
            ccache_template=config.delegation.ccache_template,
            service_name=config.service_name,
        )
        self._logger = logging.getLogger(KerberosAuth.__name__)

        self._init_mapping()

    def _init_mapping(self):
        self._principal_roles: FixUserRolesProvider | None = None
        self._principal_roles_ex: FixExcludeUserProvider | None = None
        self._kerberos_roles_in_ldap: KerberosRolesInLdapProvider | None = None

        if roles := self._config.roles:
            if roles.principal:
                self._principal_roles = FixUserRolesProvider(roles.principal)

            if roles.principal_ex:
                self._principal_roles_ex = FixExcludeUserProvider(roles.principal_ex)

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
            delegation=self.delegation,
        )
        self._install_routes(chainlit_app)
        self._install_button_js()

    async def _build_user(self, headers) -> cl.User | None:
        """По X-Remote-User строит cl.User: username из принципала + роли из AD."""
        principal = headers.get(self._config.header)
        if not principal:
            return None

        metadata: dict[str, Any] = {"provider": KerberosAuth.__name__}

        roles: list[str] = []
        exclude = False

        if self._principal_roles:
            roles.extend(self._principal_roles.roles_of(principal))

        if self._kerberos_roles_in_ldap:
            # делаем request в ldap и получаем user_dn, samaccountname, member_of
            (
                user_dn,
                samaccountname,
                member_of,
            ) = await self._kerberos_roles_in_ldap.request(principal)
            # выполняю мапинг ролей через ldap
            for x in self._kerberos_roles_in_ldap.roles_of(
                user_dn, samaccountname, member_of
            ):
                roles.append(x)

            if res := self._kerberos_roles_in_ldap.excluded_of(user_dn, member_of):
                exclude = res

        roles = list(set(roles))

        if self._principal_roles_ex:
            self._principal_roles_ex.exclude_of(principal)

        if roles:
            metadata.update(roles=roles)

        username = self._username_from_principal(
            self._config.principal_format,
            principal,
        )

        return cl.User(identifier=username, metadata=metadata)

    def _install_routes(self, chainlit_app: FastAPI) -> None:
        """Регистрирует /auth/sso (вход после SPNEGO) и /sso.js (кнопка)."""
        js = self._get_static_button()

        async def auth_sso(request: Request) -> RedirectResponse:
            # сюда долетаем только после успешного SPNEGO
            # middleware положил X-Remote-User поэтому мы считаем
            # что авторизация прошла успешно
            # здесь мы собираем юзера и заводим сессию chainlit (JWT-cookie)
            from chainlit.auth import create_jwt, set_auth_cookie  # noqa: PLC0415
            from chainlit.data import get_data_layer  # noqa: PLC0415

            user = await self._build_user(request.headers)
            if user is None:
                return RedirectResponse(url=self._login_url, status_code=303)

            if data_layer := get_data_layer():
                try:
                    await data_layer.create_user(user)
                except Exception:
                    self._logger.exception("failed to persist SSO user")

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
        if existing and existing != self._js_path:
            self._logger.warning(
                "custom_js already set (%s) — skipping SSO button injection",
                existing,
            )
            return

        cl_config.ui.custom_js = self._js_path

    @staticmethod
    def _prepend_route(
        chainlit_app: FastAPI, path: str, endpoint: Callable[..., Awaitable[Any]]
    ) -> None:
        """Добавляет GET-роут в начало, иначе его перехватит chainlit"""
        chainlit_app.add_api_route(
            path, endpoint, methods=["GET"], include_in_schema=False
        )
        chainlit_app.router.routes.insert(0, chainlit_app.router.routes.pop())

    def _get_static_button(self) -> str:
        "Генерирует JS кнопки SSO: клонирует нативную кнопку формы login и ведёт на SSO"
        template = """\
(() => {
  "use strict";
  const SSO_URL = "__SSO_URL__";
  const BTN_ID = "sso-login-btn";

  const onLogin = () => /\\/login\\/?$/.test(window.location.pathname);

  function build(sample) {
    // клон нативной кнопки: классы, вёрстка и тема наследуются автоматически
    const btn = sample.cloneNode(true);
    btn.id = BTN_ID;
    btn.type = "button";
    btn.textContent = "Войти через SSO";
    btn.removeAttribute("disabled");
    btn.removeAttribute("form");
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      fetch(SSO_URL, { credentials: "same-origin" })
        .then((r) => {
          if (r.ok) {
            window.location.href = r.url;
          } else {
            window.location.href = window.location.pathname + "?error=sso";
          }
        })
        .catch(() => {
          window.location.href = window.location.pathname + "?error=sso";
        });
    });
    return btn;
  }

  function inject() {
    if (!onLogin()) {
      const stale = document.getElementById(BTN_ID);
      if (stale) stale.remove();
      return;
    }
    if (document.getElementById(BTN_ID)) return;

    const form = document.querySelector("form");
    if (!form) return;

    // образец стиля — нативная submit-кнопка формы
    const sample =
      form.querySelector('button[type="submit"]') || form.querySelector("button");
    if (!sample) return;

    sample.insertAdjacentElement("afterend", build(sample));
  }

  const obs = new MutationObserver(() => inject());
  obs.observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("DOMContentLoaded", inject);
  inject();
})();
"""
        return template.replace("__SSO_URL__", self._sso_url)
