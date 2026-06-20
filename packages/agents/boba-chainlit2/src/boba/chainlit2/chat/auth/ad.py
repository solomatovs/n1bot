from collections.abc import Iterable, Mapping
from contextlib import contextmanager


class LDAPUserNotFoundErrorError(Exception):
    pass


class LDAPUnknownError(Exception):
    def __init__(self, e: Exception):
        self.e = e


class ADDirectory:
    """Каталог AD: поиск пользователя, его группы (memberOf), проверка пароля."""

    @staticmethod
    @contextmanager
    def _bind_with_password(
        server: str,
        bind_dn: str,
        bind_password: str,
    ):
        from ldap3 import (  # noqa: PLC0415
            Connection,
            Server,
        )

        conn: Connection | None = None
        try:
            with Connection(
                server=Server(host=server, get_info="ALL", connect_timeout=5),
                user=bind_dn,
                password=bind_password,
                auto_bind="DEFAULT",
            ) as conn:
                yield conn
        except Exception as e:
            raise LDAPUnknownError(e) from e
        finally:
            if conn:
                conn.unbind()

    @staticmethod
    def _username_from_principal(principal: str) -> str:
        """user@REALM | DOMAIN\\user -> sAMAccountName."""
        if "@" in principal:
            return principal.split("@", 1)[0]
        if "\\" in principal:
            return principal.split("\\", 1)[1]
        return principal

    @staticmethod
    def fetch_userdn_and_member_of(
        server: str,
        bind_dn: str,
        bind_password: str,
        search_base: str,
        search_filter: str,
    ) -> tuple[str, list[str]]:
        """Ищет пользователя: (DN, группы memberOf);"""
        with ADDirectory._bind_with_password(
            server,
            bind_dn,
            bind_password,
        ) as conn:
            conn.search(
                search_base=search_base,
                search_filter=search_filter,
                attributes=["memberOf"],
            )

            if not conn.entries:
                raise LDAPUserNotFoundErrorError()

            entry = conn.entries[0]

            dn = str(entry.entry_dn)
            member_of = [str(x) for x in entry.memberOf.values]

            return dn, member_of

    @staticmethod
    def role_of(
        group_dn_and_roles: Mapping[str, str], member_of: list[str]
    ) -> Iterable[str]:
        """Возвращает роли которые подключены пользователю"""
        for group_dn, role in group_dn_and_roles.items():
            if group_dn in member_of:
                yield role
