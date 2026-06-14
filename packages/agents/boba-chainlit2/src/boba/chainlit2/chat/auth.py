import asyncio
import base64
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import chainlit as cl
from starlette.types import ASGIApp, Receive, Scope, Send

from boba.chainlit2.infra.config import (
    CredentialsAuthConfig,
    KerberosAuthConfig,
    KerberosDelegationConfig,
    LdapDirectoryConfig,
)

UserCallback = Callable[..., Awaitable[cl.User | None]]


class ADDirectory:
    """Каталог AD: поиск пользователя, его группы (memberOf), проверка пароля."""

    def __init__(self, c: LdapDirectoryConfig) -> None:
        self._c = c

    @staticmethod
    def _username_from_principal(principal: str) -> str:
        """user@REALM | DOMAIN\\user -> sAMAccountName."""
        if "@" in principal:
            return principal.split("@", 1)[0]
        if "\\" in principal:
            return principal.split("\\", 1)[1]
        return principal

    def lookup(self, principal: str) -> tuple[str, list[str]] | None:
        """Ищет пользователя: (DN, группы memberOf);"""
        from ldap3 import ALL, Connection, Server  # noqa: PLC0415
        from ldap3.core.exceptions import LDAPException  # noqa: PLC0415

        server = Server(self._c.server, get_info=ALL)
        try:
            with Connection(
                server, self._c.bind_dn, self._c.bind_password, auto_bind=True
            ) as conn:
                conn.search(
                    self._c.base_dn,
                    self._c.user_filter.format(username=self._username_from_principal(principal)),
                    attributes=["memberOf"],
                )
                if not conn.entries:
                    return None

                entry = conn.entries[0]

                return str(entry.entry_dn), [str(g) for g in entry.memberOf.values]
        except LDAPException:
            return None

    def verify_password(self, user_dn: str, password: str) -> bool:
        """Проверка пароля bind'ом под DN пользователя; пустой пароль — отказ."""
        from ldap3 import Connection, Server  # noqa: PLC0415
        from ldap3.core.exceptions import LDAPException  # noqa: PLC0415

        if not password:
            return False

        try:
            with Connection(
                Server(self._c.server), user_dn, password, auto_bind=True
            ) as conn:
                conn.unbind()
                return True
        except LDAPException:
            return False

    def role_of(self, groups: list[str]) -> str | None:
        """Первая совпавшая роль по порядку group_role_map; None — доступ запрещён."""
        for group_dn, role in self._c.group_role_map.items():
            if group_dn in groups:
                return role

        return None


class CredentialsAuth:
    """Авторизация по статической таблице логин/пароль из конфига."""

    def __init__(self, c: CredentialsAuthConfig) -> None:
        self._users = dict(c.users)

    def install(self, chainlit_app: ASGIApp) -> None:
        cl.password_auth_callback(self._build_callback())

    def _build_callback(self) -> UserCallback:
        users = self._users

        async def password_auth(username: str, password: str) -> cl.User | None:
            if users.get(username) == password:
                return cl.User(
                    identifier=username,
                    metadata={"role": "admin", "provider": "credentials"},
                )

            return None

        return password_auth


class GroupRoleAuth:
    """База kerberos/ldap: cl.User строится из групп AD"""

    # provider кладётся в metadata пользователя; задаётся наследником
    provider: str = ""

    def __init__(self, c: LdapDirectoryConfig) -> None:
        self._ad = ADDirectory(c)

    def _user(self, identifier: str, groups: list[str]) -> cl.User | None:
        """Единая сборка пользователя из групп: роль по карте или отказ."""
        if (role := self._ad.role_of(groups)) is None:
            return None

        return cl.User(
            identifier=identifier,
            metadata={
                "role": role,
                "groups": groups,
                "provider": self.provider,
            },
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

    class _Entry:
        __slots__ = ("ccache", "lock")

        def __init__(self, ccache: str) -> None:
            self.ccache = ccache
            self.lock = asyncio.Lock()

    def __init__(self, *, renew: bool) -> None:
        self._entries: dict[str, KerberosCredentialStore._Entry] = {}
        self._renew_enabled = renew

    def register(self, principal: str, ccache: str) -> None:
        """Сохраняет/обновляет делегированный тикет принципала.

        Существующую запись обновляет на месте — lock принципала сохраняется.
        """
        entry = self._entries.get(principal)
        if entry is None:
            self._entries[principal] = self._Entry(ccache)
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
        if not self._renew_enabled:
            return False

        entry = self._entries.get(principal)
        if entry is None:
            return False

        async with entry.lock:
            # _renew синхронно ходит в KDC — в поток, чтобы не блокировать loop
            ok = await asyncio.to_thread(self._renew, principal, entry.ccache)
            if not ok:
                self._entries.pop(principal, None)
            return ok

    def drop(self, principal: str) -> None:
        """Убирает тикет принципала."""
        self._entries.pop(principal, None)

    @staticmethod
    def _renew(principal: str, ccache: str) -> bool:
        """Продлевает TGT в ccache через krb5; True — успех, False — нельзя/не вышло.

        Ленивый импорт: без krb5 продление недоступно (degradation, не падение).
        Реальный путь проверяется только на живом KDC.
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
    """SPNEGO-accept на gssapi только на /auth/header: principal + захват delegated.

    chainlit спрашивает header_auth_callback ровно на POST /auth/header, дальше
    сессия живёт по cookie-JWT. Поэтому SPNEGO-челлендж и захват делегирования
    делаем только на этом пути — прочие запросы проходят насквозь.
    """

    # путь логина chainlit (внутри смонтированного chainlit_app, без root_path)
    AUTH_PATH = "/auth/header"

    def __init__(
        self,
        app: ASGIApp,
        *,
        service_name: str,
        delegation: KerberosDelegationConfig | None,
        store: KerberosCredentialStore,
    ) -> None:
        self.app = app
        self.service_name = service_name
        self.delegation = delegation
        self.store = store
        self.logger = logging.getLogger(SpnegoMiddleware.__name__)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # SPNEGO нужен только на логине; всё прочее (включая cookie-JWT) — мимо
        if scope["type"] != "http" or scope["path"] != self.AUTH_PATH:
            await self.app(scope, receive, send)
            return

        auth = dict(scope["headers"]).get(b"authorization", b"")
        if not auth.startswith(b"Negotiate "):
            await self._challenge(send)
            return

        ctx = self._accept(base64.b64decode(auth.split(b" ", 1)[1]))
        if ctx is None or not ctx.complete:
            await self._challenge(send)
            return

        principal = str(ctx.initiator_name)
        scope["username"] = principal
        # делегирование — только если задан конфиг; иначе просто аутентификация
        if self.delegation is not None:
            self._capture(principal, ctx)

        await self.app(scope, receive, send)

    def _accept(self, token: bytes):
        """Принимает SPNEGO-token строго под нашим SPN (без автоподбора из keytab)."""
        import gssapi  # noqa: PLC0415

        try:
            # SPN указываем явно: берём ровно ту запись keytab, без магии
            name = gssapi.Name(self.service_name, gssapi.NameType.kerberos_principal)
            creds = gssapi.Credentials(name=name, usage="accept")
            ctx = gssapi.SecurityContext(creds=creds, usage="accept")
            ctx.step(token)
        except gssapi.exceptions.GSSError as exc:  # type: ignore[attr-defined]
            self.logger.warning("spnego accept failed: %s", exc)
            return None
        return ctx

    def _capture(self, principal: str, ctx) -> None:
        """Сохраняет delegated credential в per-user ccache и регистрирует в store."""
        import gssapi  # noqa: PLC0415

        deleg = ctx.delegated_credentials
        if deleg is None:
            self.logger.warning(
                "нет delegated_credentials в kerberos для %s "
                "(делегирование запрещено в AD)",
                principal,
            )
            return

        assert self.delegation is not None  # noqa: S101
        safe = re.sub(r"[^\w.@-]", "_", principal)
        ccache = self.delegation.ccache_template.format(principal=safe)
        try:
            deleg.store(
                store={b"ccache": ccache.encode()}, usage="initiate", overwrite=True
            )
        except gssapi.exceptions.GSSError as exc:  # type: ignore[attr-defined]
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


class KerberosAuth(GroupRoleAuth):
    """SSO через Kerberos/SPNEGO; роль — из групп AD; опц. сквозное делегирование."""

    provider = "kerberos"

    def __init__(self, c: KerberosAuthConfig, store: KerberosCredentialStore) -> None:
        super().__init__(c)
        self._c = c
        self._store = store

    def install(self, chainlit_app: ASGIApp) -> None:
        # порядок add_middleware: последний — внешний, отрабатывает первым.
        # SPNEGO (только на /auth/header) терминирует Negotiate, кладёт принципала
        # в scope['username'] и захватывает delegated cred; затем PrincipalToHeader
        # переносит его в заголовок, который читает header_auth_callback.
        chainlit_app.add_middleware(self._PrincipalToHeader, header=self._c.header)
        chainlit_app.add_middleware(
            SpnegoMiddleware,
            service_name=self._c.service_name,
            delegation=self._c.delegation,
            store=self._store,
        )
        cl.header_auth_callback(self._build_callback())

    def _build_callback(self) -> UserCallback:
        header = self._c.header

        async def header_auth(headers) -> cl.User | None:
            principal = headers.get(header)
            if not principal:
                return None

            # личность подтверждена тикетом; группы берём из AD по принципалу
            # (LDAP синхронный — в поток, чтобы не блокировать event loop)
            found = await asyncio.to_thread(self._ad.lookup, principal)
            groups = found[1] if found else []
            return self._user(principal, groups)

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


class LdapAuth(GroupRoleAuth):
    """Логин/пароль с проверкой bind'ом в AD; роль — из групп AD (как kerberos)."""

    provider = "ldap"

    def install(self, chainlit_app: ASGIApp) -> None:
        cl.password_auth_callback(self._build_callback())

    def _build_callback(self) -> UserCallback:
        async def password_auth(username: str, password: str) -> cl.User | None:
            # личность подтверждаем bind'ом под пользователем, затем те же группы
            # (LDAP синхронный — в поток, чтобы не блокировать event loop)
            found = await asyncio.to_thread(self._ad.lookup, username)
            if found is None:
                return None

            user_dn, groups = found
            if not await asyncio.to_thread(self._ad.verify_password, user_dn, password):
                return None

            return self._user(username, groups)

        return password_auth
