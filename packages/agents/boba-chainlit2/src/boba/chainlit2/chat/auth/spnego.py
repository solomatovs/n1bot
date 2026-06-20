import asyncio
import base64
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import chainlit as cl
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from boba.chainlit2.chat.auth.ad import (
    ADDirectory,
    LDAPUnknownError,
    LDAPUserNotFoundErrorError,
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
            import krb5  # type: ignore[import-not-found]  # noqa: PLC0415
        except ModuleNotFoundError:
            return False

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
        store: KerberosCredentialStore,
    ) -> None:
        self.app = app
        self.config = config
        self.store = store
        self.logger = logging.getLogger(SpnegoMiddleware.__name__)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        from gssapi.raw.misc import GSSError  # noqa: PLC0415

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

        auth = Headers(scope=scope).get("authorization")
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
        scope["username"] = principal

        return await self.app(scope, receive, send)

    def _get_spnego_context(self):
        """Принимает SPNEGO-token от браузера и"""
        from gssapi import Credentials, Name, NameType, SecurityContext  # noqa: PLC0415

        # SPN указываем явно: берём ровно ту запись keytab, без магии
        name = Name(self.config.service_name, NameType.kerberos_principal)
        creds = Credentials(
            name=name,
            usage="accept",
            store={
                # Файл с долгосрочными ключами сервиса (ключи SPN)
                # Им сервер расшифровывает входящий тикет
                b"keytab": self.config.keytab,
            },
        )

        ctx = SecurityContext(creds=creds, usage="accept")

        return ctx

    def _capture(self, principal: str, ctx) -> None:
        """Сохраняет delegated credential в per-user ccache и регистрирует в store."""
        from gssapi.raw.misc import GSSError  # noqa: PLC0415

        deleg = ctx.delegated_creds
        if deleg is None:
            self.logger.warning(
                "нет delegated_credentials в kerberos для %s "
                "(делегирование запрещено в AD)",
                principal,
            )
            return

        assert self.config.delegation is not None  # noqa: S101
        safe = re.sub(r"[^\w.@-]", "_", principal)
        ccache = self.config.delegation.ccache_template.format(principal=safe)
        try:
            deleg.store(
                store={b"ccache": ccache.encode()},
                usage="initiate",
                overwrite=True,
            )
        except GSSError as exc:  # type: ignore[attr-defined]
            self.logger.error(
                "kerberos: не сохранить delegated cred %s: %s", ccache, exc
            )
            return

        self.store.register(principal, ccache)
        self.logger.info(
            "kerberos: захвачен delegated тикет %s → %s", principal, ccache
        )


class KerberosAuthInstaller:
    """SSO через Kerberos/SPNEGO; роль — из групп AD; опц. сквозное делегирование."""

    def __init__(self, c: KerberosAuthConfig, store: KerberosCredentialStore):
        self._c = c
        self._store = store
        self._provider = "kerberos"
        self._ad = ADDirectory

    def install(self, chainlit_app: ASGIApp) -> None:
        chainlit_app.add_middleware(self._PrincipalToHeader, header=self._c.header)
        chainlit_app.add_middleware(
            SpnegoMiddleware,
            config=self._c,
            store=self._store,
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
            except LDAPUserNotFoundErrorError as e:
                raise AuthenticationError("AuthenticationError") from e
            except LDAPUnknownError as e:
                raise ExternalServiceError(
                    "ldap", "Couldn't perform ldap search"
                ) from e

        return header_auth

    class _PrincipalToHeader:
        """scope['username'] от SpnegoMiddleware → заголовок chainlit."""

        def __init__(self, app: ASGIApp, header: str) -> None:
            self.app = app
            self.header = header.lower().encode()

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            username = scope.get("username")
            if username and scope["type"] in ("http", "websocket"):
                headers = [(k, v) for k, v in scope["headers"] if k != self.header]
                headers.append((self.header, str(username).encode()))
                scope = {**scope, "headers": headers}
            await self.app(scope, receive, send)
