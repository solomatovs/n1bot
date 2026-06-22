import asyncio
import base64
import logging
import re
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import chainlit as cl
import krb5
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
from starlette.types import ASGIApp, Receive, Scope, Send

from boba.chainlit2.chat.auth.fix import FixUserRolesProvider, RolesMappingConfig
from boba.chainlit2.chat.auth.ldap import (
    ADDirectory,
    DnUserRolesProvider,
    LDAPError,
    LDAPInvalidCredentialsError,
    LDAPServerUnavailableError,
    LDAPUserNotFoundError,
    MemberOfUserRolesProvider,
)
from boba.chainlit2.chat.handler import chainlit_error_handler
from boba.chainlit2.errors import (
    AuthenticationError,
    BaseError,
    ExternalServiceError,
    HttpErrorMessage,
    InternalServiceError,
)

UserCallback = Callable[..., Awaitable[cl.User | None]]


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
    member_of: RolesMappingConfig | None = Field(default=None, description="")
    dn: RolesMappingConfig | None = Field(default=None, description="")


class KerberosRolesConfig(BaseModel):
    fix: RolesMappingConfig | None = Field(
        default=None,
        description="",
    )
    ldap: KerberosRolesInLdapConfig | None = Field(
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
    header: str = Field(
        default="X-Remote-User",
        description="Заголовок, куда кладётся принципал для header_auth_callback.",
    )
    delegation: KerberosDelegationConfig = Field(
        default_factory=KerberosDelegationConfig,
        description="Параметры ccache для unconstrained режима делегирования",
    )
    roles: KerberosRolesConfig = Field(
        default=KerberosRolesConfig(),
        description="Мапперы учеток и ролей",
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
        except Exception as _exc:
            return False

        return True


class SpnegoChallengeError(BaseError):
    """
    Запрос SPNEGO-токена, не является ошибкой
    Рендериться как Http ответ
    """

    status_code = 401

    def http_message(self) -> HttpErrorMessage | None:
        # обязательный заголовок для всех поверхностей, тело пустое
        return HttpErrorMessage(
            status_code=self.status_code,
            headers=[
                (b"www-authenticate", b"Negotiate"),
                (b"content-type", b"text/plain; charset=utf-8"),
            ],
            content="",
        )


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
                "нет delegated_credentials для %s (делегирование запрещено в AD)",
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
            "kerberos: захвачен delegated тикет %s → %s", principal, ccache
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
    SPNEGO-accept на gssapi только на /auth/header: principal + захват delegated.

    chainlit спрашивает header_auth_callback ровно на POST /auth/header,
    дальше сессия живёт по cookie-JWT.
    Поэтому SPNEGO-челлендж и захват делегирования
    делаем только на этом пути — прочие запросы проходят насквозь.
    """

    # путь логина chainlit (внутри смонтированного chainlit_app, без root_path)
    AUTH_PATH = "/auth/header"

    def __init__(
        self,
        app: ASGIApp,
        *,
        config: KerberosAuthConfig,
        delegation: KerberosDelegation,
    ) -> None:
        self._app = app
        self._config = config
        self._delegation = delegation
        self.logger = logging.getLogger(SpnegoMiddleware.__name__)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self._app(scope, receive, send)

        path = scope.get("path")
        if not isinstance(path, str):
            return await self._app(scope, receive, send)

        path = path.removesuffix("/").lower()

        if not path.endswith(self.AUTH_PATH):
            return await self._app(scope, receive, send)

        headers = Headers(scope=scope).mutablecopy()
        auth = headers.get("authorization")
        if not auth:
            raise SpnegoChallengeError()

        scheme, _, value = auth.partition(" ")
        if scheme.lower() != "negotiate" or not value:
            raise SpnegoChallengeError()

        try:
            token = base64.b64decode(value)
        except Exception as e:
            raise SpnegoChallengeError() from e

        try:
            # сбой здесь это проблема сервера
            ctx = self._get_spnego_context()
        except GSSError as e:
            raise InternalServiceError(
                internal_detail=f"gss error: {e}, file: {__file__}",
                user_detail="Spnego authentication failed",
            ) from e

        try:
            # их токен — сбой здесь это проблема клиента
            ctx.step(token)
        except GSSError as e:
            raise SpnegoChallengeError() from e

        if not ctx.complete:
            raise SpnegoChallengeError()

        principal = str(ctx.initiator_name)

        self._delegation.on_success_authenticated(principal, ctx)

        header = self._config.header.lower()
        headers[header] = principal
        scope["headers"] = headers.raw

        return await self._app(scope, receive, send)

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
        self._member_of_roles: MemberOfUserRolesProvider | None = None
        self._dn_roles: DnUserRolesProvider | None = None

        if roles := self._config.member_of:
            self._member_of_roles = MemberOfUserRolesProvider(roles)

        if roles := self._config.dn:
            self._dn_roles = DnUserRolesProvider(roles)

    async def roles_of(self, username: str) -> AsyncIterable[str]:
        search_filter = f"(sAMAccountName={username})"

        try:
            user_dn, member_of = await asyncio.to_thread(
                self._ad.fetch_userdn_and_member_of,
                server=self._config.server,
                bind_dn=self._config.bind_dn,
                bind_password=self._config.bind_password,
                search_base=self._config.base_dn,
                search_filter=search_filter,
            )
        except LDAPUserNotFoundError as e:
            raise AuthenticationError("User is not registered") from e
        except LDAPServerUnavailableError as e:
            raise ExternalServiceError(
                "ldap", "LDAP service is unavailable, please try again later"
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

        if self._member_of_roles:
            for x in self._member_of_roles.roles_of(member_of):
                yield x

        if self._dn_roles:
            for x in self._dn_roles.roles_of(user_dn):
                yield x


class KerberosAuth:
    """SSO через Kerberos/SPNEGO; роль — из групп AD; опц. сквозное делегирование."""

    def __init__(self, config: KerberosAuthConfig):
        self._config = config
        self._provider = "kerberos"
        self._ad = ADDirectory
        self.delegation = KerberosDelegation(
            store=KerberosCredentialStore(),
            keytab=config.keytab,
            ccache_template=config.delegation.ccache_template,
            service_name=config.service_name,
        )

        self._init_mapping()

    def _init_mapping(self):
        self._fixed_roles: FixUserRolesProvider | None = None
        self._kerberos_roles_in_ldap: KerberosRolesInLdapProvider | None = None

        if roles := self._config.roles.fix:
            self._fixed_roles = FixUserRolesProvider(roles)

        if ldap := self._config.roles.ldap:
            self._kerberos_roles_in_ldap = KerberosRolesInLdapProvider(ldap)

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

    def install(self, chainlit_app: ASGIApp) -> None:
        chainlit_app.add_middleware(
            SpnegoMiddleware,
            config=self._config,
            delegation=self.delegation,
        )
        cl.header_auth_callback(self._build_callback())

    def _build_callback(self) -> UserCallback:
        header = self._config.header

        @chainlit_error_handler
        async def header_auth(headers) -> cl.User | None:
            principal = headers.get(header)
            if not principal:
                return None

            username = self._username_from_principal(
                self._config.principal_format,
                principal,
            )

            metadata: dict[str, Any] = {"provider": KerberosAuth.__name__}

            roles: list[str] = []
            if self._fixed_roles:
                roles.extend(self._fixed_roles.roles_of(username))

            if self._kerberos_roles_in_ldap:
                async for x in self._kerberos_roles_in_ldap.roles_of(username):
                    roles.append(x)

            roles = list(set(roles))

            if roles:
                metadata.update(roles=roles)

            return cl.User(
                identifier=username,
                metadata=metadata,
            )

        return header_auth
