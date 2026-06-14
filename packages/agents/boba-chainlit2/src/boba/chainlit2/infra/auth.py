import asyncio
import base64
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar

import chainlit as cl
from starlette.types import ASGIApp, Receive, Scope, Send

from boba.chainlit2.infra.config import (
    AuthConfig,
    CredentialsAuthConfig,
    KerberosAuthConfig,
    KerberosDelegationConfig,
    LdapAuthConfig,
    LdapDirectoryConfig,
)

logger = logging.getLogger("auth")

UserCallback = Callable[..., Awaitable[cl.User | None]]


class ADDirectory:
    """Каталог AD: поиск пользователя, его группы (memberOf), проверка пароля."""

    def __init__(self, c: LdapDirectoryConfig) -> None:
        self._c = c

    @staticmethod
    def _username(principal: str) -> str:
        """user@REALM | DOMAIN\\user → sAMAccountName."""
        if "@" in principal:
            return principal.split("@", 1)[0]
        if "\\" in principal:
            return principal.split("\\", 1)[1]
        return principal

    def lookup(self, principal: str) -> "tuple[str, list[str]] | None":
        """Сервисным bind'ом ищет пользователя: (DN, группы memberOf); None — нет."""
        from ldap3 import ALL, Connection, Server  # noqa: PLC0415

        server = Server(self._c.server, get_info=ALL)
        with Connection(
            server, self._c.bind_dn, self._c.bind_password, auto_bind=True
        ) as conn:
            conn.search(
                self._c.base_dn,
                self._c.user_filter.format(username=self._username(principal)),
                attributes=["memberOf"],
            )
            if not conn.entries:
                return None
            entry = conn.entries[0]
            return str(entry.entry_dn), [str(g) for g in entry.memberOf.values]

    def verify_password(self, user_dn: str, password: str) -> bool:
        """Проверка пароля bind'ом под DN пользователя; пустой пароль — отказ."""
        from ldap3 import Connection, Server  # noqa: PLC0415
        from ldap3.core.exceptions import LDAPException  # noqa: PLC0415

        if not password:
            return False

        try:
            conn = Connection(Server(self._c.server), user_dn, password, auto_bind=True)
        except LDAPException:
            return False
        conn.unbind()
        return True

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
    """Реестр делегированных тикетов (принципал - ccache) и их фоновое продление.

    Владеет всем жизненным циклом тикета: захват кладёт сюда credential и
    поднимает один рефрешер на принципала; teardown приложения гасит все.
    """

    class _Entry:
        __slots__ = ("ccache", "expiry", "task")

        def __init__(self, ccache: str, expiry: float) -> None:
            self.ccache = ccache
            self.expiry = expiry
            self.task: asyncio.Task | None = None

    _entries: ClassVar[dict[str, "KerberosCredentialStore._Entry"]] = {}

    @classmethod
    def register(
        cls,
        principal: str,
        ccache: str,
        expiry: float,
        *,
        renew: bool,
        margin_sec: int,
    ) -> None:
        """Сохраняет/обновляет тикет; при renew держит один рефрешер на принципала."""
        entry = cls._entries.get(principal)
        if entry is None:
            entry = cls._entries[principal] = cls._Entry(ccache, expiry)
        else:
            entry.ccache, entry.expiry = ccache, expiry

        if renew and (entry.task is None or entry.task.done()):
            entry.task = asyncio.create_task(cls._renew_loop(principal, margin_sec))

    @classmethod
    def ccache_of(cls, principal: str) -> str | None:
        """ccache пользователя (значение KRB5CCNAME) или None."""
        entry = cls._entries.get(principal)
        return entry.ccache if entry else None

    @classmethod
    def drop(cls, principal: str) -> None:
        """Убирает тикет и гасит его рефрешер."""
        entry = cls._entries.pop(principal, None)
        if entry and entry.task is not None:
            entry.task.cancel()

    @classmethod
    async def shutdown(cls) -> None:
        """Гасит все рефрешеры; вызывается на teardown приложения."""
        for entry in cls._entries.values():
            if entry.task is not None:
                entry.task.cancel()
        cls._entries.clear()

    @classmethod
    async def _renew_loop(cls, principal: str, margin_sec: int) -> None:
        """Спит до (expiry-margin), продлевает; провал → снимаем запись и стоп."""
        while True:
            entry = cls._entries.get(principal)
            if entry is None:
                return
            # не реже раза в минуту, иначе по таймеру тикета
            await asyncio.sleep(max(60, entry.expiry - time.time() - margin_sec))
            # _renew синхронно ходит в KDC — в поток, чтобы не блокировать loop
            new_expiry = await asyncio.to_thread(cls._renew, principal, entry.ccache)
            if new_expiry is None:
                logger.info("kerberos: продление %s прекращено", principal)
                cls._entries.pop(principal, None)
                return
            entry.expiry = new_expiry

    @staticmethod
    def _renew(principal: str, ccache: str) -> float | None:
        """Продлевает TGT в ccache через krb5; новый expiry (epoch) или None.

        Ленивый импорт: без krb5 продление недоступно (degradation, не падение).
        Реальный путь проверяется только на живом KDC.
        """
        try:
            import krb5  # type: ignore[import-not-found]  # noqa: PLC0415
        except ModuleNotFoundError:
            logger.warning("krb5 не установлен — продление тикета недоступно")
            return None

        try:
            ctx = krb5.init_context()
            cc = krb5.cc_resolve(ctx, ccache.encode())
            princ = krb5.cc_get_principal(ctx, cc)
            creds = krb5.get_renewed_creds(ctx, princ, cc)
            krb5.cc_initialize(ctx, cc, princ)
            krb5.cc_store_cred(ctx, cc, creds)
        except Exception as exc:
            logger.warning("kerberos: продление %s не удалось: %s", principal, exc)
            return None

        # endtime тикета у krb5.Creds доступен не во всех версиях единообразно;
        # берём явный, иначе консервативно перепроверим через час
        endtime = getattr(getattr(creds, "times", None), "endtime", None)
        return float(endtime) if endtime else time.time() + 3600


class SpnegoDelegationMiddleware:
    """SPNEGO-accept на gssapi: principal в scope + захват delegated cred."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        service_name: str,
        delegation: KerberosDelegationConfig | None,
    ) -> None:
        self.app = app
        self.service_name = service_name
        self.delegation = delegation

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
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
            logger.warning("spnego accept failed: %s", exc)
            return None
        return ctx

    def _capture(self, principal: str, ctx) -> None:
        """Сохраняет delegated credential в per-user ccache и регистрирует в store."""
        import gssapi  # noqa: PLC0415

        deleg = ctx.delegated_credentials
        if deleg is None:
            logger.warning(
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
            logger.error("kerberos: не сохранить delegated cred %s: %s", ccache, exc)
            return

        expiry = time.time() + (deleg.lifetime or 0)
        KerberosCredentialStore.register(
            principal,
            ccache,
            expiry,
            renew=self.delegation.renew,
            margin_sec=self.delegation.renew_margin_sec,
        )
        logger.info("kerberos: захвачен delegated тикет %s → %s", principal, ccache)

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

    def __init__(self, c: KerberosAuthConfig) -> None:
        super().__init__(c)
        self._c = c

    def install(self, chainlit_app: ASGIApp) -> None:
        # порядок add_middleware: последний — внешний, отрабатывает первым.
        # SPNEGO терминирует Negotiate, кладёт принципала в scope['username'] и
        # захватывает delegated cred; затем PrincipalToHeader — в заголовок.
        chainlit_app.add_middleware(self._PrincipalToHeader, header=self._c.header)
        chainlit_app.add_middleware(
            SpnegoDelegationMiddleware,
            service_name=self._c.service_name,
            delegation=self._c.delegation,
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
        """scope['username'] от SpnegoDelegationMiddleware → заголовок chainlit."""

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


class Auth:
    """Единая точка: выбирает и подключает стратегию авторизации по конфигу."""

    @staticmethod
    def install(chainlit_app: ASGIApp, c: AuthConfig) -> None:
        if isinstance(c, CredentialsAuthConfig):
            CredentialsAuth(c).install(chainlit_app)

        elif isinstance(c, KerberosAuthConfig):
            KerberosAuth(c).install(chainlit_app)

        elif isinstance(c, LdapAuthConfig):
            LdapAuth(c).install(chainlit_app)

        else:
            raise ValueError(f"unknown authorization type: {type(c).__name__}")
