"""Поиск пользователя в AD через ldap3: bind указанной учёткой, поиск по фильтру,
перевод исключений ldap3 в ошибки каталога core.

Ошибки:
LDAPServerUnavailableError — сеть, TLS, пул серверов, таймаут.
LDAPInvalidCredentialsError — bind отклонён.
LDAPAccessDeniedError — недостаточно прав.
LDAPConfigError — база поиска, DN, фильтр, сервер или TLS-политика непригодны.
LDAPUserNotFoundError — записи нет.
LDAPError — прочие исключения ldap3.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from enum import StrEnum
from typing import ClassVar

from ldap3 import AUTO_BIND_DEFAULT, NONE, Connection, Server
from ldap3.core.exceptions import (
    LDAPBindError,
    LDAPCommunicationError,
    LDAPException,
    LDAPInsufficientAccessRightsResult,
    LDAPInvalidCredentialsResult,
    LDAPInvalidDNSyntaxResult,
    LDAPInvalidFilterError,
    LDAPInvalidServerError,
    LDAPNoSuchObjectResult,
    LDAPServerPoolError,
    LDAPStartTLSError,
    LDAPStrongerAuthRequiredResult,
)

from boba.identity.directory import (
    ADUserEntry,
    DirectoryBinding,
    DirectorySearch,
    LDAPAccessDeniedError,
    LDAPConfigError,
    LDAPError,
    LDAPInvalidCredentialsError,
    LDAPServerUnavailableError,
    LDAPUserNotFoundError,
    UserDirectory,
)

__all__ = ["Ldap3Directory"]


class UserAttribute(StrEnum):
    """Атрибуты записи пользователя, которые читает каталог."""

    SAM_ACCOUNT_NAME = "sAMAccountName"
    MEMBER_OF = "memberOf"


class Ldap3Directory(UserDirectory):
    """Каталог AD: каждое обращение — свой bind и поиск в потоке."""

    CONNECT_TIMEOUT_SEC: ClassVar[int] = 5

    async def find(
        self, binding: DirectoryBinding, search: DirectorySearch
    ) -> ADUserEntry:
        return await asyncio.to_thread(self._find, binding, search)

    def _find(self, binding: DirectoryBinding, search: DirectorySearch) -> ADUserEntry:
        with self._bound(binding) as connection:
            connection.search(
                search_base=search.base_dn,
                search_filter=search.filter,
                attributes=[UserAttribute.SAM_ACCOUNT_NAME, UserAttribute.MEMBER_OF],
            )

            if not connection.entries:
                raise LDAPUserNotFoundError()

            entry = connection.entries[0]
            member_of: list[str] = []
            for group in entry.memberOf.values:
                member_of.append(str(group))

            return ADUserEntry(
                dn=str(entry.entry_dn),
                samaccountname=str(entry.sAMAccountName.value),
                member_of=member_of,
            )

    @contextmanager
    def _bound(self, binding: DirectoryBinding) -> Generator[Connection, None, None]:
        connection: Connection | None = None
        try:
            server = Server(
                host=binding.server,
                get_info=NONE,
                connect_timeout=self.CONNECT_TIMEOUT_SEC,
            )
            with Connection(
                server=server,
                user=binding.bind_dn,
                password=binding.bind_password.get_secret_value(),
                auto_bind=AUTO_BIND_DEFAULT,
                # без raise_exceptions ошибки search'а молча дают пустой результат
                raise_exceptions=True,
            ) as connection:
                yield connection
        except LDAPError:
            # ошибки каталога core (например, LDAPUserNotFoundError из тела with)
            # идут как есть
            raise
        except (
            LDAPCommunicationError,
            LDAPInvalidServerError,
            LDAPServerPoolError,
            LDAPStartTLSError,
        ) as exc:
            raise LDAPServerUnavailableError(str(exc)) from exc
        except (LDAPBindError, LDAPInvalidCredentialsResult) as exc:
            raise LDAPInvalidCredentialsError(str(exc)) from exc
        except LDAPInsufficientAccessRightsResult as exc:
            raise LDAPAccessDeniedError(str(exc)) from exc
        except (
            LDAPNoSuchObjectResult,
            LDAPInvalidDNSyntaxResult,
            LDAPInvalidFilterError,
            LDAPStrongerAuthRequiredResult,
        ) as exc:
            raise LDAPConfigError(str(exc)) from exc
        except LDAPException as exc:
            raise LDAPError(str(exc)) from exc
        finally:
            if connection is not None:
                connection.unbind()
