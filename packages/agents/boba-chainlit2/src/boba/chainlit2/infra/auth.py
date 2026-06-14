import logging
from collections.abc import Awaitable, Callable

import chainlit as cl
from starlette.types import ASGIApp, Receive, Scope, Send

from boba.chainlit2.infra.config import (
    AuthConfig,
    CredentialsAuthConfig,
    KerberosAuthConfig,
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


class KerberosAuth(GroupRoleAuth):
    """SSO через Kerberos/SPNEGO; роль — из групп AD."""

    provider = "kerberos"

    def __init__(self, c: KerberosAuthConfig) -> None:
        super().__init__(c)
        self._c = c

    def install(self, chainlit_app: ASGIApp) -> None:
        from fastapi_gssapi import GSSAPIMiddleware  # noqa: PLC0415

        # порядок add_middleware: последний — внешний, отрабатывает первым.
        # GSSAPI терминирует Negotiate и кладёт принципала в scope['username'],
        # затем PrincipalToHeader перекладывает его в заголовок для chainlit.
        chainlit_app.add_middleware(self._PrincipalToHeader, header=self._c.header)
        chainlit_app.add_middleware(GSSAPIMiddleware, spn=self._c.service_name)
        cl.header_auth_callback(self._build_callback())

    def _build_callback(self) -> UserCallback:
        header = self._c.header

        async def header_auth(headers) -> cl.User | None:
            principal = headers.get(header)
            if not principal:
                return None

            # личность подтверждена тикетом; группы берём из AD по принципалу
            found = self._ad.lookup(principal)
            groups = found[1] if found else []
            return self._user(principal, groups)

        return header_auth

    class _PrincipalToHeader:
        """scope['username'] от GSSAPIMiddleware → заголовок для chainlit."""

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
            found = self._ad.lookup(username)
            if found is None:
                return None

            user_dn, groups = found
            if not self._ad.verify_password(user_dn, password):
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
