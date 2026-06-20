import asyncio
import base64
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

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
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from boba.chainlit2.chat.auth.ad import (
    ADDirectory,
    LDAPError,
    LDAPInvalidCredentialsError,
    LDAPServerUnavailableError,
    LDAPUserNotFoundError,
)
from boba.chainlit2.chat.handler import chainlit_error_handler
from boba.chainlit2.errors import (
    AuthenticationError,
    BaseError,
    ExternalServiceError,
    HttpErrorMessage,
    InternalServiceError,
)
from boba.chainlit2.infra.config import (
    KerberosAuthConfig,
)

UserCallback = Callable[..., Awaitable[cl.User | None]]


@dataclass(frozen=True)
class UserCcache:
    """Сем для tools: принципал пользователя и его ccache (значение KRB5CCNAME)."""

    principal: str
    ccache: str


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


class Delegation(Protocol):
    """
    Стратегия получения credential от имени пользователя
    """

    def on_success_authenticated(self, principal: str, ctx) -> None:
        "при успешном SPNEGO-логине вызывается этот хук"
        ...

    async def credentials_for(self, username: str, target_spn: str) -> bytes:
        "Получить SPNEGO-токен к target_spn от имени пользователя"
        ...


class SpnegoChallengeError(BaseError):
    """
    Запрос SPNEGO-токена, не является ошибкой
    Рендериться как Http ответ
    """

    status_code = 401

    def http_message(self) -> HttpErrorMessage:
        # обязательный заголовок для всех поверхностей, тело пустое
        return HttpErrorMessage(
            status_code=self.status_code,
            headers=[(b"www-authenticate", b"Negotiate")],
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


class UnconstrainedDelegation(Delegation):
    """
    Неограниченное делегирование
    при логине захватываем форварднутый TGT в ccache,
    потом когда потребуется мы ходим им в другую систему
    """

    def __init__(
        self,
        store: KerberosCredentialStore,
        ccache_template: str,
    ) -> None:
        self._store = store
        self._ccache_template = ccache_template
        self._logger = logging.getLogger(UnconstrainedDelegation.__name__)

    @staticmethod
    def sanitize_principal(principal: str):
        return re.sub(r"[^\w.@-]", "_", principal)

    def on_success_authenticated(self, principal: str, ctx) -> None:
        deleg = ctx.delegated_creds
        if deleg is None:
            # AD не форварднул TGT — делегирование запрещено для пользователя.
            # Не валим логин: это валидное состояние, а не сбой
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
            # НЕ глушим: иначе юзер войдёт, а делегирование молча не работает
            raise InternalServiceError(
                internal_detail=f"failed to store delegated ccache {ccache}: {e}",
                user_detail=None,
            ) from e

        self._store.register(principal, ccache)
        self._logger.info(
            "kerberos: захвачен delegated тикет %s → %s", principal, ccache
        )

    def captured(self, username: str) -> bool:
        "Есть ли захваченный при логине ccache пользователя (форварднутый TGT)."
        return self._store.ccache_of(username) is not None

    async def credentials_for(self, username: str, target_spn: str) -> bytes:
        ccache = self._store.ccache_of(username)
        if ccache is None:
            raise InternalServiceError(
                internal_detail=f"no delegated ccache for {username}",
                user_detail="Делегирование недоступно для пользователя",
            )
        return await asyncio.to_thread(self._init_token, ccache, target_spn)

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


class ProtocolTransitionDelegation(Delegation):
    """
    Стратегия ограниченного делегирования - S4U2Self+S4U2Proxy
    тикет по имени пользователя
    """

    def __init__(self, service_name: str, keytab: str) -> None:
        self._service_name = service_name
        self._keytab = keytab
        self._logger = logging.getLogger(ProtocolTransitionDelegation.__name__)

    def on_success_authenticated(self, principal: str, ctx) -> None:
        # ничего не храним: тикет добываем по требованию в credentials_for
        return None

    async def credentials_for(self, username: str, target_spn: str) -> bytes:
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


class KerberosDelegation(Delegation):
    """
    Выбираем стратегию согласно AD учетки
    Если в AD установлено delegated_creds, значит выбирается
    стратегия ограниченного делегирования (перечислены allowlist)
    иначе выбирается стратегия неограниченного делегирования
    """

    def __init__(
        self,
        unconstrained: UnconstrainedDelegation,
        s4u: ProtocolTransitionDelegation,
    ) -> None:
        self._unconstrained = unconstrained
        self._s4u = s4u

    def on_success_authenticated(self, principal: str, ctx) -> None:
        if ctx.delegated_creds is not None:
            self._unconstrained.on_success_authenticated(principal, ctx)

    async def credentials_for(self, username: str, target_spn: str) -> bytes:
        if self._unconstrained.captured(username):
            return await self._unconstrained.credentials_for(username, target_spn)

        return await self._s4u.credentials_for(username, target_spn)


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
        delegation: Delegation,
    ) -> None:
        self.app = app
        self.config = config
        self.delegation = delegation
        self.logger = logging.getLogger(SpnegoMiddleware.__name__)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):  # noqa: C901
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path")
        if not path:
            return await self.app(scope, receive, send)

        if not isinstance(path, str):
            return await self.app(scope, receive, send)

        path = path.removesuffix("/").lower()

        if not path.lower().endswith(self.AUTH_PATH):
            return await self.app(scope, receive, send)

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

        self.delegation.on_success_authenticated(principal, ctx)

        header = self.config.header.lower()
        headers[header] = principal
        scope["headers"] = headers

        return await self.app(scope, receive, send)

    def _get_spnego_context(self):
        """Принимает SPNEGO-token от браузера и"""
        name = Name(self.config.service_name, NameType.kerberos_principal)
        creds = Credentials(
            name=name,
            usage="accept",
            store={
                b"keytab": self.config.keytab,
            },
        )

        ctx = SecurityContext(creds=creds, usage="accept")

        return ctx


class KerberosAuthInstaller:
    """SSO через Kerberos/SPNEGO; роль — из групп AD; опц. сквозное делегирование."""

    def __init__(self, c: KerberosAuthConfig, delegation: Delegation):
        self._c = c
        self._delegation = delegation
        self._provider = "kerberos"
        self._ad = ADDirectory

    def install(self, chainlit_app: ASGIApp) -> None:
        chainlit_app.add_middleware(
            SpnegoMiddleware,
            config=self._c,
            delegation=self._delegation,
        )
        cl.header_auth_callback(self._build_callback())

    def _build_callback(self) -> UserCallback:
        header = self._c.header

        @chainlit_error_handler
        async def header_auth(headers) -> cl.User | None:
            principal = headers.get(header)
            if not principal:
                return None

            try:
                username = self._ad._username_from_principal(principal)
                search_filter = self._c.user_filter.format(username=username)

                _user_dn, member_of = await asyncio.to_thread(
                    self._ad.fetch_userdn_and_member_of,
                    server=self._c.server,
                    bind_dn=self._c.bind_dn,
                    bind_password=self._c.bind_password,
                    search_base=self._c.base_dn,
                    search_filter=search_filter,
                )

                roles = list(
                    await asyncio.to_thread(
                        self._ad.role_of,
                        group_dn_and_roles=self._c.group_role_map,
                        member_of=member_of,
                    )
                )

                return cl.User(
                    identifier=username,
                    metadata={
                        "role": roles,
                        "provider": self._provider,
                    },
                )
            except LDAPUserNotFoundError as e:
                raise AuthenticationError("User is not registered") from e
            except LDAPServerUnavailableError as e:
                raise ExternalServiceError(
                    "ldap", "LDAP service is unavailable, please try again later"
                ) from e
            except LDAPInvalidCredentialsError as e:
                # сервис-аккаунт kerberos: отклонённый bind = наша конфигурация
                raise InternalServiceError(
                    internal_detail=f"ldap service bind rejected: {e}",
                    user_detail=None,
                ) from e
            except LDAPError as e:
                raise InternalServiceError(
                    internal_detail=f"ldap error: {e}", user_detail=None
                ) from e

        return header_auth
