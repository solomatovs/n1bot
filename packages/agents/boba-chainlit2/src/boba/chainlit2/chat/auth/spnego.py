import asyncio
import base64
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import chainlit as cl
from starlette.types import ASGIApp, Receive, Scope, Send

from boba.chainlit2.infra.config import (
    KerberosAuthConfig,
)

from .ad import ADDirectory, LDAPUserNotFoundErrorError

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
        service_name: str,
        config: KerberosAuthConfig,
        store: KerberosCredentialStore,
    ) -> None:
        self.app = app
        self.config = config
        self.store = store
        self.service_name = service_name
        self.logger = logging.getLogger(SpnegoMiddleware.__name__)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http" or not scope.get("path", "").endswith(
            self.AUTH_PATH
        ):
            return await self.app(scope, receive, send)

        auth = dict(scope["headers"]).get(b"authorization", b"")
        if not auth.startswith(b"Negotiate "):
            await self._challenge(send)
            return None

        ctx = self._accept(base64.b64decode(auth.split(b" ", 1)[1]))
        if ctx is None or not ctx.complete:
            await self._challenge(send)
            return None

        principal = str(ctx.initiator_name)
        scope["username"] = principal
        # делегирование — только если задан конфиг; иначе просто аутентификация
        if self.config.delegation is not None:
            self._capture(principal, ctx)

        return await self.app(scope, receive, send)

    def _accept(self, token: bytes):
        """Принимает SPNEGO-token строго под нашим SPN (без автоподбора из keytab)."""
        from gssapi import Credentials, Name, NameType, SecurityContext  # noqa: PLC0415
        from gssapi.raw.misc import GSSError  # noqa: PLC0415

        try:
            # SPN указываем явно: берём ровно ту запись keytab, без магии
            name = Name(self.service_name, NameType.kerberos_principal)
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
            ctx.step(token)
        except GSSError as exc:
            self.logger.warning("spnego accept failed: %s", exc)
            return None
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

    @staticmethod
    async def _challenge(send: Send) -> None:
        """401 с WWW-Authenticate: Negotiate — браузер пришлёт SPNEGO-токен."""
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", b"Negotiate"),
                    (b"content-length", b"0"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})


class KerberosAuth:
    """SSO через Kerberos/SPNEGO; роль — из групп AD; опц. сквозное делегирование."""

    def __init__(self, c: KerberosAuthConfig, store: KerberosCredentialStore):
        self._c = c
        self._store = store
        self._provider = "kerberos"
        self._activedirectory = ADDirectory
        self._logger = logging.getLogger(KerberosAuth.__name__)

    def install(self, chainlit_app: ASGIApp) -> None:
        chainlit_app.add_middleware(self._PrincipalToHeader, header=self._c.header)
        chainlit_app.add_middleware(
            SpnegoMiddleware,
            service_name=self._c.service_name,
            config=self._c,
            store=self._store,
        )
        cl.header_auth_callback(self._build_callback())

    def _build_callback(self) -> UserCallback:
        header = self._c.header

        async def header_auth(headers) -> cl.User | None:
            principal = headers.get(header)
            if not principal:
                return None

            try:
                username = self._activedirectory._username_from_principal(principal)
                search_filter = self._c.user_filter.format(username=username)

                _user_dn, member_of = await asyncio.to_thread(
                    self._activedirectory.fetch_userdn_and_member_of,
                    server=self._c.server,
                    bind_dn=self._c.bind_dn,
                    bind_password=self._c.bind_password,
                    search_base=self._c.base_dn,
                    search_filter=search_filter,
                )

                roles = list(
                    await asyncio.to_thread(
                        self._activedirectory.role_of,
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
            except LDAPUserNotFoundErrorError as _e:
                return None
            except Exception:
                self._logger.exception("Couldn't perform ldap search")
                return None

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
